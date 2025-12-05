import os
import json
import random
import pandas as pd
import io
import csv
import tempfile
import shutil
import zipfile
from datetime import datetime
from functools import wraps
from types import SimpleNamespace

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    session,
    send_from_directory,
    abort,
    jsonify,
    send_file,
    make_response,
    g,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from models import (
    db,
    User,
    Job,
    SurveyItem,
    PartRequirement,
    Attachment,
    Template,
    TemplateItem,
    TemplateGroup,
    RoleEnum,
    JobStatusEnum,
    DecisionEnum,
    StockStatusEnum,
    MeasurementResultEnum,
    StoreItem,
    PickList,
    PickListItem,
    PickListStatusEnum,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------------------------------------------------------------------
# Custom request class for large form submissions
# ---------------------------------------------------------------------
class LargeFormRequest(Flask.request_class):
    max_form_memory_size = 100 * 1024 * 1024  # 100MB for safety


# ---------------------------------------------------------------------
# Flask application factory
# ---------------------------------------------------------------------
app = Flask(__name__)


def create_app():
    global app
    app.request_class = LargeFormRequest

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB

    db.init_app(app)

    with app.app_context():
        db.create_all()

        # Ensure at least one admin user exists
        if not User.query.filter_by(username="admin").first():
            admin = User(
                username="admin",
                role=RoleEnum.ADMIN.value,
                password_hash=generate_password_hash("admin"),
            )
            db.session.add(admin)
            db.session.commit()

    return app


# ---------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def roles_required(*roles):
    """
    Decorator to require one of the given roles.

    Accepts either RoleEnum values or plain strings, e.g.:

        @roles_required(RoleEnum.ADMIN, RoleEnum.OFFICE)
        @roles_required("Admin", "Office")
    """

    def _role_val(r):
        # Normalise to the underlying string value
        if isinstance(r, RoleEnum):
            return r.value
        return str(r)

    allowed = {_role_val(r) for r in roles}

    def decorator(view):
        @wraps(view)
        def decorated(*args, **kwargs):
            user_id = session.get("user_id")
            if not user_id:
                return redirect(url_for("login"))

            user = User.query.get(user_id)
            if not user:
                abort(403)

            user_role_val = _role_val(user.role)
            if user_role_val not in allowed:
                abort(403)

            return view(*args, **kwargs)

        return decorated

    return decorator


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

def get_current_user():
    """Compatibility shim so views can call get_current_user()."""
    return current_user()

# ---------------------------------------------------------------------
# Survey seeding from template
# ---------------------------------------------------------------------
def ensure_survey_from_template(job: Job) -> None:
    """
    Ensure this job has SurveyItem rows, seeded from its Template items.

    - If survey items already exist for the job, do nothing.
    - Otherwise, copy all TemplateItem rows over, including behaviour flags.
    """
    existing = SurveyItem.query.filter_by(job_id=job.id).first()
    if existing:
        return

    if not job.template_id:
        return

    template = Template.query.get(job.template_id)
    if not template:
        return

    template_items = (
        TemplateItem.query
        .filter_by(template_id=template.id)
        .order_by(TemplateItem.section, TemplateItem.pos)
        .all()
    )

    for t in template_items:
        # Optional: derive kit info from groups (if you’re using TemplateGroup for kits)
        kit_name = None
        kit_master = False
        if getattr(t, "groups", None):
            for g in t.groups:
                if getattr(g, "type", None) == "kit":
                    kit_name = g.label
                    # simple assumption: the kit “master” line is the one flagged in_kit
                    kit_master = bool(t.in_kit)
                    break

        s = SurveyItem(
            job_id=job.id,
            section=t.section,
            pos=t.pos,
            part_no=t.part_no,
            name=t.name,
            qty=t.qty or 1,

            # behaviour flags copied from TemplateItem
            always_replace=t.always_replace,
            in_kit=t.in_kit,
            big_ticket=getattr(t, "big_ticket", False),
            requires_test=getattr(t, "requires_test", False),
            requires_measurement=t.requires_measurement,

            # kit info used later for BOM logic
            kit_name=kit_name,
            kit_master=kit_master,
        )
        db.session.add(s)

    db.session.commit()

# ---------------------------------------------------------------------
# Helper: overlay Stores info onto BOM lines
#   Returns: dict[part_id] -> {
#       "store": StoreItem or None,
#       "stock": int,
#       "required": int,
#       "enough": bool,
#   }
# ---------------------------------------------------------------------
def build_store_overlay(job, parts):
    overlay = {}

    # If this job isn't tied to a template, we can't match into StoreItem rows
    if not job.template_id:
        return overlay

    # All template items for this engine template
    template_items = TemplateItem.query.filter_by(
        template_id=job.template_id
    ).all()

    # Build lookup: (part_no, section, pos) -> StoreItem
    template_store_lookup = {}
    for ti in template_items:
        store = ti.store_item
        if not store:
            continue

        key = (
            (ti.part_no or "").strip(),
            (ti.section or "").strip(),
            ti.pos or 0,
        )
        template_store_lookup[key] = store

    # Now attach store info per BOM part
    for p in parts:
        key = (
            (p.part_no or "").strip(),
            (p.section or "").strip(),
            p.pos or 0,
        )
        store = template_store_lookup.get(key)
        if not store:
            continue

        stock = store.qty_in_stock or 0
        required = p.qty_required or 0

        overlay[p.id] = {
            "store": store,
            "stock": stock,
            "required": required,
            "enough": stock >= required if required > 0 else stock > 0,
        }

    return overlay

# ---------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------
def job_age_days(job):
    """Return whole days since job was created (or None)."""
    if not job.created_at:
        return None
    delta = datetime.utcnow() - job.created_at
    return delta.days


def next_action_for_job(job, user):
    """
    Very simple 'next action' text based on job status and user role.
    This is just for the dashboard; tweak wordings any time.
    """
    role = getattr(user, "role", None)

    if job.status == JobStatusEnum.PRE_SURVEY:
        if role == RoleEnum.TECHNICIAN:
            return "Start strip survey"
        else:
            return "Assign to technician" if not job.assigned_to else "Start strip survey"

    if job.status == JobStatusEnum.SURVEY:
        if role == RoleEnum.TECHNICIAN:
            return "Complete strip survey"
        else:
            return "Review survey & generate BOM"

    if job.status == JobStatusEnum.OFFICE:
        return "Complete parts list & order parts"

    if job.status == JobStatusEnum.BUILD:
        if role == RoleEnum.TECHNICIAN:
            return "Build & test engine"
        else:
            return "Monitor build progress"

    if job.status == JobStatusEnum.COMPLETE:
        return "Job complete"

    return "View job"



# ---------------------------------------------------------------------
# Simple helpers
# ---------------------------------------------------------------------
def save_uploaded_file(file_obj, subdir=""):
    if not file_obj or file_obj.filename == "":
        return None

    filename = secure_filename(file_obj.filename)
    folder = app.config["UPLOAD_FOLDER"]
    if subdir:
        folder = os.path.join(folder, subdir)
        os.makedirs(folder, exist_ok=True)

    path = os.path.join(folder, filename)
    file_obj.save(path)
    return os.path.relpath(path, app.config["UPLOAD_FOLDER"])


# ---------------------------------------------------------------------
# Template globals
# ---------------------------------------------------------------------
@app.context_processor
def inject_globals():
    return {
        "JobStatusEnum": JobStatusEnum,
        "DecisionEnum": DecisionEnum,
        "StockStatusEnum": StockStatusEnum,
        "MeasurementResultEnum": MeasurementResultEnum,
        "RoleEnum": RoleEnum,
        "current_user": current_user(),
        "job_age_days": job_age_days,
        "next_action_for_job": next_action_for_job,
    }
@app.route("/")
def index():
    # If already logged in, go straight to the dashboard
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    # Otherwise, send to login screen
    return redirect(url_for("login"))

# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username or password.", "danger")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user.id
        flash("Logged in successfully.", "success")

        # --- Role-based landing page ---
        if user.role == RoleEnum.STORES:
            # Stores go straight to Stores hub
            return redirect(url_for("stores_hub", view="master"))
        else:
            # Everyone else keeps the normal dashboard
            return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# Dashboard – role-aware job overview
# ---------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    # Get the logged-in user
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    # Stores users don't use the job dashboard
    if user.role == RoleEnum.STORES:
        return redirect(url_for("stores_hub", view="master"))

    # Base query – newest jobs first
    q = Job.query.order_by(Job.created_at.desc())

    # Technicians only see jobs assigned to them
    if user.role == RoleEnum.TECHNICIAN:
        q = q.filter(Job.assigned_to_id == user.id)

    all_jobs = q.all()

    # Group jobs by status
    pre_survey_jobs = []
    survey_jobs = []
    office_jobs = []
    build_jobs = []
    complete_jobs = []

    for job in all_jobs:
        if job.status == JobStatusEnum.PRE_SURVEY:
            pre_survey_jobs.append(job)
        elif job.status == JobStatusEnum.SURVEY:
            survey_jobs.append(job)
        elif job.status == JobStatusEnum.OFFICE:
            office_jobs.append(job)
        elif job.status == JobStatusEnum.BUILD:
            build_jobs.append(job)
        elif job.status == JobStatusEnum.COMPLETE:
            complete_jobs.append(job)

    # For the Admin view: simple dict of lists
    jobs_by_status = {
        "Pre-survey": pre_survey_jobs,
        "Survey": survey_jobs,
        "Office": office_jobs,
        "Build": build_jobs,
        "Complete": complete_jobs,
    }

    # Template switches on this
    user_role = user.role.value  # "Admin", "Office", "Technician", "Stores"

    return render_template(
        "dashboard.html",
        user_role=user_role,
        pre_survey_jobs=pre_survey_jobs,
        survey_jobs=survey_jobs,
        office_jobs=office_jobs,
        build_jobs=build_jobs,
        complete_jobs=complete_jobs,
        jobs_by_status=jobs_by_status,
    )


# ---------------------------------------------------------------------
# Jobs – basic CRUD
# ---------------------------------------------------------------------
@app.route("/jobs")
@login_required
def jobs_list():
    jobs = Job.query.order_by(Job.created_at.desc()).all()
    return render_template("jobs_list.html", jobs=jobs)


@app.route("/jobs/create", methods=["GET", "POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value, RoleEnum.OFFICE.value)
def job_create():
    user = current_user()

    if request.method == "POST":
        job_number = (request.form.get("job_number") or "").strip()
        customer_name = (request.form.get("customer_name") or "").strip()
        engine_type = (request.form.get("engine_type") or "").strip()
        serial_number = (request.form.get("serial_number") or "").strip()
        template_id = request.form.get("template_id") or ""
        assigned_to_id = request.form.get("assigned_to_id") or ""

        if not job_number:
            flash("Job number is required.", "danger")
            return redirect(url_for("job_create"))

        # 🔹 NEW: guard against duplicate job numbers
        existing = Job.query.filter_by(job_number=job_number).first()
        if existing:
            flash(
                f"Job number {job_number} already exists. "
                "Use a different number or open the existing job.",
                "danger",
            )
            return redirect(url_for("job_create"))

        job = Job(
            job_number=job_number,
            customer_name=customer_name or None,
            engine_type=engine_type or None,
            serial_number=serial_number or None,
            status=JobStatusEnum.PRE_SURVEY,
            template_id=int(template_id) if template_id else None,
            assigned_to_id=int(assigned_to_id) if assigned_to_id else None,
            created_by_id=user.id if user else None,
        )

        db.session.add(job)
        db.session.commit()
        flash("Job created.", "success")
        return redirect(url_for("jobs_list"))

    technicians = (
        User.query.filter(User.role.in_([RoleEnum.TECHNICIAN, RoleEnum.ADMIN]))
        .order_by(User.username)
        .all()
    )
    templates = Template.query.order_by(Template.name).all()

    return render_template(
        "create_job.html",
        technicians=technicians,
        templates=templates,
    )


@app.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    survey_items = (
        SurveyItem.query.filter_by(job_id=job.id)
        .order_by(SurveyItem.section, SurveyItem.pos, SurveyItem.id)
        .all()
    )
    parts = PartRequirement.query.filter_by(job_id=job.id).all()
    attachments = Attachment.query.filter_by(job_id=job.id).all()
    return render_template(
        "job_detail.html",
        job=job,
        survey_items=survey_items,
        parts=parts,
        attachments=attachments,
    )
@app.route("/jobs/<int:job_id>/pack")
@login_required
def job_pack(job_id):
    job = Job.query.get_or_404(job_id)

    # Full data for this job
    survey_items = (
        SurveyItem.query.filter_by(job_id=job.id)
        .order_by(SurveyItem.section, SurveyItem.pos, SurveyItem.id)
        .all()
    )
    parts = (
        PartRequirement.query.filter_by(job_id=job.id)
        .order_by(PartRequirement.section, PartRequirement.pos, PartRequirement.id)
        .all()
    )
    attachments = Attachment.query.filter_by(job_id=job.id).all()

    # Simple survey stats
    total_lines = len(survey_items)
    always_replace_count = sum(1 for s in survey_items if s.always_replace)
    big_ticket_count = sum(1 for s in survey_items if getattr(s, "big_ticket", False))
    test_count = sum(1 for s in survey_items if getattr(s, "requires_test", False))

    good_count = sum(1 for s in survey_items if s.decision == DecisionEnum.REUSE)
    replace_count = sum(1 for s in survey_items if s.decision == DecisionEnum.REPLACE)
    scrap_count = sum(1 for s in survey_items if s.decision == DecisionEnum.SCRAP)
    undecided_count = sum(1 for s in survey_items if s.decision is None)

    survey_counts = {
        "total": total_lines,
        "always": always_replace_count,
        "big_ticket": big_ticket_count,
        "test": test_count,
        "good": good_count,
        "replace": replace_count,
        "scrap": scrap_count,
        "undecided": undecided_count,
    }

    return render_template(
        "job_pack.html",
        job=job,
        survey_items=survey_items,
        parts=parts,
        attachments=attachments,
        survey_counts=survey_counts,
    )
# ---------------------------------------------------------------------
# Admin test helpers – autofill survey / mark all good
# ---------------------------------------------------------------------
@app.route("/admin/jobs/<int:job_id>/autofill_survey", methods=["POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def admin_autofill_survey(job_id):
    """Quickly auto-fill a survey for testing/demo."""
    job = Job.query.get_or_404(job_id)

    # Make sure survey exists
    ensure_survey_from_template(job)

    survey_items = SurveyItem.query.filter_by(job_id=job.id).all()

    for item in survey_items:
        if item.always_replace:
            # 100% items → always replace
            item.decision = DecisionEnum.REPLACE
        else:
            # Random-ish decisions for demo purposes
            item.decision = random.choice(
                [DecisionEnum.REUSE, DecisionEnum.REPLACE, DecisionEnum.SCRAP]
            )

    db.session.commit()
    flash("Survey auto-filled for testing.", "success")
    return redirect(url_for("job_survey", job_id=job.id))


@app.route("/admin/jobs/<int:job_id>/mark_all_good", methods=["POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def admin_mark_all_good(job_id):
    """Mark all survey lines as Good (100% items stay Replace)."""
    job = Job.query.get_or_404(job_id)

    ensure_survey_from_template(job)

    survey_items = SurveyItem.query.filter_by(job_id=job.id).all()

    for item in survey_items:
        if item.always_replace:
            item.decision = DecisionEnum.REPLACE
        else:
            item.decision = DecisionEnum.REUSE

    db.session.commit()
    flash("All survey lines marked Good (100% items kept as Replace).", "success")
    return redirect(url_for("job_survey", job_id=job.id))

# ---------------------------------------------------------------------
# Helper: rebuild BOM from survey
# ---------------------------------------------------------------------
def regenerate_bom_for_job(job, survey_items):
    """
    Rebuild PartRequirement rows for a job from the current survey.

    - Clears existing BOM lines for this job.
    - Creates a line for each SurveyItem that is 100% (always_replace)
      or has decision REPLACE / SCRAP.
    - Uses amount_required_X from the form if present, otherwise falls
      back to the template quantity.

    Returns:
        int: number of PartRequirement lines created.
    """
    # Remove existing parts list for this job
    PartRequirement.query.filter_by(job_id=job.id).delete()
    db.session.flush()

    created = 0

    for item in survey_items:
        decision = item.decision
        always = bool(item.always_replace)

        # Only create BOM line when we actually need parts
        if not (always or decision in (DecisionEnum.REPLACE, DecisionEnum.SCRAP)):
            continue

        # Tech-entered amount (may be blank)
        raw_qty = (request.form.get(f"amount_required_{item.id}") or "").strip()
        try:
            qty = int(raw_qty) if raw_qty != "" else 0
        except ValueError:
            qty = 0

        # Fallback to template quantity
        if qty <= 0:
            qty = item.qty or 1

        bom_line = PartRequirement(
            job_id=job.id,
            survey_item_id=item.id,
            section=item.section,
            pos=item.pos,
            part_no=item.part_no,
            name=item.name,
            qty_required=qty,
            stock_status=StockStatusEnum.TO_ORDER,
        )
        db.session.add(bom_line)
        created += 1

    return created


# ---------------------------------------------------------------------
# Strip survey
# ---------------------------------------------------------------------
@app.route("/jobs/<int:job_id>/survey", methods=["GET", "POST"])
@login_required
def job_survey(job_id):
    job = Job.query.get_or_404(job_id)

    # 1) Make sure survey rows exist for this job (clone from template if needed)
    ensure_survey_from_template(job)

    # 2) Layout flag from query string (?layout=classic or ?layout=split)
    layout = request.args.get("layout") or "classic"

    # 3) Load survey items in a nice order
    survey_items = (
        SurveyItem.query.filter_by(job_id=job.id)
        .order_by(SurveyItem.section, SurveyItem.pos, SurveyItem.id)
        .all()
    )

    # 4) Section → image URL mapping (per template / engine type)
    #
    # We look for images under:
    #   uploads/section_images/<engine_type>/...
    # falling back to:
    #   uploads/section_images/...
    #
    # Filenames like "TA0100_Crankcase.png" will be mapped to keys:
    #   "TA0100", "TA 0100", "TA0100_Crankcase", "TA 0100 Crankcase"
    section_images = {}

    template = job.template
    engine_type = template.engine_type if template and template.engine_type else None

    img_root = os.path.join(UPLOAD_FOLDER, "section_images")
    search_dirs = []

    # Prefer per-engine folder if it exists (e.g. uploads/section_images/steyr M160036-0)
    if engine_type:
        engine_dir = os.path.join(img_root, engine_type)
        search_dirs.append(engine_dir)

    # Fallback: generic section_images folder
    search_dirs.append(img_root)

    allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    for directory in search_dirs:
        if not os.path.isdir(directory):
            continue

        for fname in os.listdir(directory):
            base, ext = os.path.splitext(fname)
            if ext.lower() not in allowed_exts:
                continue

            clean = base.strip()  # e.g. "TA0100_Crankcase"
            # First chunk before underscore is the section code, e.g. "TA0100"
            core = clean.split("_", 1)[0].strip()

            # Build variants with and without spaces
            code_no_space = core.replace(" ", "").upper()          # "TA0100"
            code_with_space = (
                f"{code_no_space[:2]} {code_no_space[2:]}"
                if len(code_no_space) > 3
                else code_no_space
            )                                                      # "TA 0100"

            # Nice label variant (for completeness)
            pretty = clean.replace("_", " ")

            # Relative path under UPLOAD_FOLDER for url_for('uploaded_file')
            rel_path = os.path.relpath(os.path.join(directory, fname), UPLOAD_FOLDER)
            rel_path = rel_path.replace("\\", "/")
            img_url = url_for("uploaded_file", filename=rel_path)

            for key in {clean, core, code_no_space, code_with_space, pretty}:
                if key and key not in section_images:
                    section_images[key] = img_url

    if request.method == "POST":
        action = request.form.get("action", "save")

        # 4a) Update decisions + notes for each existing row
        for item in survey_items:
            # Decision: reuse / replace / scrap / ""
            decision_raw = request.form.get(f"decision_{item.id}", "").strip()
            if decision_raw:
                try:
                    item.decision = DecisionEnum(decision_raw)
                except ValueError:
                    item.decision = None
            else:
                item.decision = None

            # Notes (from hidden input that the notes popup updates)
            notes_val = request.form.get(f"notes_{item.id}")
            if notes_val is not None:
                item.notes = notes_val.strip() or None

            # Measurements (if this line requires a measurement)
            if item.requires_measurement:
                val_raw = (request.form.get(f"measurement_value_{item.id}") or "").strip()
                min_raw = (request.form.get(f"spec_min_{item.id}") or "").strip()
                max_raw = (request.form.get(f"spec_max_{item.id}") or "").strip()
                unit_raw = (request.form.get(f"measurement_unit_{item.id}") or "").strip()

                item.measurement_value = val_raw or None
                item.spec_min = min_raw or None
                item.spec_max = max_raw or None
                item.measurement_unit = unit_raw or None

                # Decide measurement result
                try:
                    v = float(val_raw)
                    lo = float(min_raw)
                    hi = float(max_raw)
                except (TypeError, ValueError):
                    item.measurement_result = MeasurementResultEnum.NOT_MEASURED
                else:
                    if lo <= v <= hi:
                        item.measurement_result = MeasurementResultEnum.WITHIN_SPEC
                    else:
                        item.measurement_result = MeasurementResultEnum.OUT_OF_SPEC

        # 4b) Add a new survey row if requested
        new_name = (request.form.get("new_name") or "").strip()
        if new_name:
            new_section = (request.form.get("new_section") or "").strip() or None
            new_pos_raw = (request.form.get("new_pos") or "").strip()
            new_pos = int(new_pos_raw) if new_pos_raw else None

            new_part_no = (request.form.get("new_part_no") or "").strip() or None
            new_qty_raw = (request.form.get("new_qty") or "").strip()
            try:
                new_qty = int(new_qty_raw) if new_qty_raw else 1
            except ValueError:
                new_qty = 1

            db.session.add(
                SurveyItem(
                    job_id=job.id,
                    section=new_section,
                    pos=new_pos,
                    part_no=new_part_no,
                    name=new_name,
                    qty=new_qty,
                )
            )

        # 4c) If the tech hits "Complete", HARD-GATE completion until all
        # non-locked lines have a decision.
        if action == "complete":
            undecided = [
                si
                for si in survey_items
                if not si.always_replace
                and (si.decision is None or si.decision == "")
            ]

            if undecided:
                db.session.commit()
                flash(
                    f"Cannot complete survey – {len(undecided)} line(s) still undecided.",
                    "error",
                )
                return redirect(
                    url_for("job_survey", job_id=job.id, layout=layout)
                )

            # Everything decided → rebuild BOM + move to Office
            created = regenerate_bom_for_job(job, survey_items)
            job.status = JobStatusEnum.OFFICE

            db.session.commit()

            db.session.commit()

            if created > 0:
                flash(
                    f"Survey completed – {created} part line(s) added to the BOM.",
                    "success",
                )
            else:
                flash(
                    "Survey completed – no parts required based on your decisions "
                    "(all items marked Good).",
                    "info",
                )

            return redirect(url_for("job_parts", job_id=job.id))

        # Normal save (non-complete)
        db.session.commit()
        flash("Strip survey updated.", "success")

        # PRG pattern – redirect to avoid resubmission
        return redirect(url_for("job_survey", job_id=job.id, layout=layout))
    
    # GET → render your big strip survey template
    return render_template(
        "job_survey.html",
        job=job,
        survey_items=survey_items,
        DecisionEnum=DecisionEnum,
        layout=layout,
        section_images=section_images,
    )


# ---------------------------------------------------------------------
# Parts / Office view
# ---------------------------------------------------------------------
@app.route("/jobs/<int:job_id>/parts", methods=["GET", "POST"])
@login_required
def job_parts(job_id):
    job = Job.query.get_or_404(job_id)

    # -----------------------------
    # POST: update parts + status
    # -----------------------------
    if request.method == "POST":
        action = request.form.get("action", "save")

        parts_for_update = (
            PartRequirement.query
            .filter_by(job_id=job.id)
            .order_by(
                PartRequirement.section,
                PartRequirement.pos,
                PartRequirement.id,
            )
            .all()
        )

        # 1) Apply form fields (qty, manual status, supplier, PO, notes)
        for part in parts_for_update:
            qty_raw = (request.form.get(f"qty_{part.id}") or "").strip()
            stock_status_raw = request.form.get(f"stock_status_{part.id}")
            supplier = (request.form.get(f"supplier_{part.id}") or "").strip()
            po_number = (request.form.get(f"po_number_{part.id}") or "").strip()
            notes = (request.form.get(f"notes_{part.id}") or "").strip()

            # Qty
            if qty_raw:
                try:
                    part.qty_required = int(qty_raw)
                except ValueError:
                    pass  # leave existing qty

            # Manual stock status (will be overridden by auto logic below if needed)
            if stock_status_raw in [s.value for s in StockStatusEnum]:
                part.stock_status = StockStatusEnum(stock_status_raw)

            part.supplier = supplier or None
            part.po_number = po_number or None
            part.notes = notes or None

        # 2) Rebuild stores overlay with updated quantities
        store_info = build_store_overlay(job, parts_for_update)

        # 3) Auto-set stock_status from stores availability
        for part in parts_for_update:
            info = store_info.get(part.id)
            if not info:
                continue

            required = part.qty_required or 0
            stock = info.get("stock", 0)

            # don't auto-touch if explicitly marked as not required or fitted
            if part.stock_status in (
                StockStatusEnum.NOT_REQUIRED,
                StockStatusEnum.FITTED,
            ):
                continue

            if required > 0 and stock >= required:
                # enough on shelf → mark as in_stock
                part.stock_status = StockStatusEnum.IN_STOCK
            else:
                # not enough → default to to_order
                part.stock_status = StockStatusEnum.TO_ORDER

        # 4) Optional: move job to BUILD
        if action == "move_to_build":
            job.status = JobStatusEnum.BUILD

        db.session.commit()

        if action == "move_to_build":
            flash("Parts list saved. Job moved to Build.", "success")
            return redirect(url_for("job_build", job_id=job.id))

        flash("Parts list updated.", "success")
        return redirect(url_for("job_parts", job_id=job.id))

    # -----------------------------
    # GET: build view model
    # -----------------------------
    search_query = (request.args.get("q") or "").strip().lower()

    all_parts = (
        PartRequirement.query
        .filter_by(job_id=job.id)
        .order_by(
            PartRequirement.section,
            PartRequirement.pos,
            PartRequirement.id,
        )
        .all()
    )

    # Simple text search on part no / name / section
    if search_query:
        parts = []
        for p in all_parts:
            haystack = " ".join([
                p.part_no or "",
                p.name or "",
                p.section or "",
            ]).lower()
            if search_query in haystack:
                parts.append(p)
    else:
        parts = list(all_parts)

    # Derive flags from SurveyItem (if linked)
    always_parts = []
    test_parts = []
    big_ticket_parts = []
    kit_parts_by_name = {}
    standard_parts = []

    for p in parts:
        si = getattr(p, "survey_item", None)

        if not si:
            standard_parts.append(p)
            continue

        if getattr(si, "always_replace", False):
            always_parts.append(p)
        elif getattr(si, "requires_test", False):
            test_parts.append(p)
        elif getattr(si, "big_ticket", False):
            big_ticket_parts.append(p)
        elif getattr(si, "kit_name", None):
            kit_name = si.kit_name
            kit_parts_by_name.setdefault(kit_name, []).append(p)
        else:
            standard_parts.append(p)

    # Stores overlay for the GET view
    store_info = build_store_overlay(job, parts)

    return render_template(
        "parts.html",
        job=job,
        search_query=search_query,
        standard_parts=standard_parts,
        always_parts=always_parts,
        test_parts=test_parts,
        big_ticket_parts=big_ticket_parts,
        kit_parts_by_name=kit_parts_by_name,
        store_info=store_info,
        StockStatusEnum=StockStatusEnum,
    )


# ---------------------------------------------------------------------
# Admin: seed StoreItem rows from a template (one-off per template)
# Triggered from the Templates admin UI, NOT from Stores.
# ---------------------------------------------------------------------
@app.route("/admin/templates/<int:template_id>/stores/seed", methods=["POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def stores_seed(template_id):
    template = Template.query.get_or_404(template_id)

    # Get all template items for this template
    template_items = (
        TemplateItem.query
        .filter_by(template_id=template.id)
        .order_by(TemplateItem.section, TemplateItem.pos)
        .all()
    )

    created = 0

    for ti in template_items:
        # Skip if this template item already has a StoreItem row
        if ti.store_item:
            continue

        # Simple starter stock: you can tweak/remove later
        dummy_stock = 10 if getattr(ti, "always_replace", False) else 4
        dummy_min = 2

        store = StoreItem(
            template_item=ti,             # link to the template item
            engine_type=template.engine_type,
            bin_location="",              # tweak later
            qty_in_stock=dummy_stock,
            min_stock=dummy_min,
            notes="",
        )

        db.session.add(store)
        created += 1

    db.session.commit()
    flash(f"Seeded {created} store lines from template «{template.name}».", "success")

    # After seeding, go to Stores hub (Office / Stores users don't see templates)
    return redirect(url_for("stores_hub", view="engine"))


# ---------------------------------------------------------------------
# Stores hub – By engine / Master list / Consumables
# ---------------------------------------------------------------------
@app.route("/stores", methods=["GET"])
@login_required
@roles_required(RoleEnum.ADMIN.value, RoleEnum.OFFICE.value,RoleEnum.STORES.value)
def stores_hub():
    # view can be: engine (or per_engine), master, consumables
    view = request.args.get("view") or "engine"
    if view == "per_engine":
        view = "engine"

    # ----- BY ENGINE: one card per template/engine -----
    if view == "engine":
        engine_summaries = []

        # all templates (each engine type)
        templates = Template.query.order_by(Template.engine_type, Template.name).all()

        for t in templates:
            # all template lines for this template
            template_items = TemplateItem.query.filter_by(template_id=t.id).all()

            # related store rows via TemplateItem.store_item
            linked_store_items = [
                ti.store_item
                for ti in template_items
                if getattr(ti, "store_item", None) is not None
            ]

            total_parts = len(template_items)
            total_stock = sum((si.qty_in_stock or 0) for si in linked_store_items)
            below_min = sum(
                1
                for si in linked_store_items
                if (si.min_stock or 0) > 0
                and (si.qty_in_stock or 0) < (si.min_stock or 0)
            )

            engine_summaries.append(
                {
                    "template_id": t.id,
                    "template_name": t.name,
                    "engine_type": t.engine_type,
                    "total_parts": total_parts,
                    "total_stock": total_stock,
                    "below_min": below_min,
                }
            )

        # optional filter by engine_type
        selected_engine_type = request.args.get("engine_type") or None
        if selected_engine_type:
            engine_summaries = [
                e
                for e in engine_summaries
                if (e["engine_type"] or "") == selected_engine_type
            ]

        engine_types = sorted({t.engine_type for t in templates if t.engine_type})

        return render_template(
            "stores.html",
            view_mode=view,
            engine_summaries=engine_summaries,
            engine_types=engine_types,
            selected_engine_type=selected_engine_type,
        )

    # ----- MASTER / CONSUMABLES TABLE -----
    q = StoreItem.query
    if view == "consumables" and hasattr(StoreItem, "is_consumable"):
        q = q.filter_by(is_consumable=True)

    store_items = q.order_by(StoreItem.id).all()

    # Attach a simple engine_types list to each store item using the backref
    for si in store_items:
        et = None
        tpl_item = getattr(si, "template_item", None)
        if tpl_item is not None and getattr(tpl_item, "template", None):
            et = tpl_item.template.engine_type
        si.engine_types = [et] if et else []

    return render_template(
        "stores.html",
        view_mode=view,
        store_items=store_items,
    )

# ---------------------------------------------------------------------
# Stores – view (and optionally edit) stock for a single template/engine
# ---------------------------------------------------------------------
@app.route("/stores/template/<int:template_id>", methods=["GET", "POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value, RoleEnum.OFFICE.value,RoleEnum.STORES.value)
def stores_for_template(template_id):
    template = Template.query.get_or_404(template_id)

    # All template lines for this template
    items = (
        TemplateItem.query
        .filter_by(template_id=template.id)
        .order_by(TemplateItem.section, TemplateItem.pos, TemplateItem.id)
        .all()
    )

    # If the template view's "Save stores" button is used, update StoreItem rows
    if request.method == "POST":
        for key, value in request.form.items():
            if "_" not in key:
                continue

            prefix, raw_id = key.split("_", 1)
            if prefix not in {"bin", "stock", "min_stock", "notes"}:
                continue

            try:
                ti_id = int(raw_id)
            except ValueError:
                continue

            ti = TemplateItem.query.get(ti_id)
            if not ti or not ti.store_item:
                continue

            store = ti.store_item

            if prefix == "bin":
                store.bin_location = (value or "").strip()
            elif prefix == "stock":
                try:
                    store.qty_in_stock = int(value or 0)
                except ValueError:
                    store.qty_in_stock = 0
            elif prefix == "min_stock":
                try:
                    store.min_stock = int(value or 0)
                except ValueError:
                    store.min_stock = 0
            elif prefix == "notes":
                store.notes = (value or "").strip()

        db.session.commit()
        flash("Stores updated for this engine template.", "success")
        return redirect(url_for("stores_for_template", template_id=template.id))

    # GET render – use the existing template-specific branch in stores.html
    return render_template(
        "stores.html",
        view_mode="template",
        template=template,
        items=items,
    )


# ---------------------------------------------------------------------
# Stores – save updates from master / consumables table
# ---------------------------------------------------------------------
@app.route("/admin/stores/update", methods=["POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value,RoleEnum.STORES.value)
def stores_update():
    view = request.form.get("view", "master")

    # ---- update fields for each StoreItem ----
    for key, value in request.form.items():
        # skip non-field keys (like 'view')
        if "_" not in key:
            continue

        prefix, raw_id = key.split("_", 1)
        if prefix not in {"bin", "stock", "min_stock", "notes"}:
            continue

        try:
            store_id = int(raw_id)
        except ValueError:
            continue

        store = StoreItem.query.get(store_id)
        if not store:
            continue

        if prefix == "bin":
            store.bin_location = (value or "").strip()
        elif prefix == "stock":
            try:
                store.qty_in_stock = int(value or 0)
            except ValueError:
                store.qty_in_stock = 0
        elif prefix == "min_stock":
            try:
                store.min_stock = int(value or 0)
            except ValueError:
                store.min_stock = 0
        elif prefix == "notes":
            store.notes = (value or "").strip()

        # If you later add an is_consumable column, you can manage it here:
        if hasattr(StoreItem, "is_consumable"):
            store.is_consumable = f"consumable_{store_id}" in request.form

    db.session.commit()
    flash("Stores updated.", "success")
    return redirect(url_for("stores_hub", view=view))

# ---------------------------------------------------------------------
# Parts / BOM export (per job)
# ---------------------------------------------------------------------
@app.route("/jobs/<int:job_id>/parts/export", methods=["GET"])
@login_required
def job_parts_export(job_id):
    job = Job.query.get_or_404(job_id)

    parts = (
        PartRequirement.query
        .filter_by(job_id=job.id)
        .order_by(
            PartRequirement.section,
            PartRequirement.pos,
            PartRequirement.id,
        )
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Job number",
        "Customer",
        "Engine type",
        "Status",
        "Section",
        "Pos",
        "Part no",
        "Name",
        "Qty required",
        "Stock status",
        "Supplier",
        "PO number",
        "Notes",
    ])

    for p in parts:
        writer.writerow([
            job.job_number,
            job.customer_name or "",
            job.engine_type or "",
            job.status.value if job.status else "",
            p.section or "",
            p.pos or "",
            p.part_no or "",
            p.name or "",
            p.qty_required or "",
            p.stock_status.value if p.stock_status else "",
            p.supplier or "",
            p.po_number or "",
            (p.notes or "").replace("\n", " "),
        ])

    csv_data = output.getvalue()
    filename = f"bom_job_{job.job_number}.csv"

    from flask import Response

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        },
    )
def build_job_pack_pdf(job, survey_items, parts, attachments, pdf_path):
    """Build a simple PDF job pack using ReportLab."""

    styles = getSampleStyleSheet()
    story = []

    # ---------- Title ----------
    story.append(Paragraph(f"Job pack – Job {job.job_number}", styles["Title"]))
    story.append(Spacer(1, 18))

    # ---------- Header / Job info ----------
    header_data = [
        ["Customer", job.customer_name or ""],
        ["Engine", job.engine_type or ""],
        ["Serial no.", job.serial_number or ""],
        [
            "Template",
            job.template.name if getattr(job, "template", None) else "",
        ],
        [
            "Assigned to",
            job.assigned_to.username if getattr(job, "assigned_to", None) else "",
        ],
        ["Status", job.status.value if job.status is not None else ""],
        [
            "Created",
            job.created_at.strftime("%Y-%m-%d %H:%M")
            if job.created_at
            else "",
        ],
        [
            "Updated",
            job.updated_at.strftime("%Y-%m-%d %H:%M")
            if job.updated_at
            else "",
        ],
    ]

    header_table = Table(header_data, colWidths=[90, 380])
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.black),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.white),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.white),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 24))

    # ---------- Strip survey summary ----------
    total = len(survey_items)
    good = sum(1 for si in survey_items if si.decision == DecisionEnum.REUSE)
    replace = sum(1 for si in survey_items if si.decision == DecisionEnum.REPLACE)
    missing = sum(1 for si in survey_items if si.decision == DecisionEnum.SCRAP)
    locked = sum(1 for si in survey_items if getattr(si, "always_replace", False))
    undecided = total - good - replace - missing

    story.append(Paragraph("Strip survey summary", styles["Heading2"]))
    story.append(Spacer(1, 6))

    summary_data = [
        ["Total survey lines", str(total)],
        ["Good / reuse", str(good)],
        ["Replace", str(replace)],
        ["Missing / scrap", str(missing)],
        ["Locked always-replace", str(locked)],
        ["Undecided", str(undecided)],
    ]

    summary_table = Table(summary_data, colWidths=[200, 60])
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, -1), colors.darkblue),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.white),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.white),
            ]
        )
    )
    story.append(summary_table)
    story.append(Spacer(1, 24))

    # ---------- Parts / BOM ----------
    story.append(Paragraph("Parts / BOM", styles["Heading2"]))
    story.append(Spacer(1, 6))

    if parts:
        bom_data = [
            ["Part no", "Name", "Qty required", "Stock status", "Notes"],
        ]

        for p in parts:
            stock = p.stock_status
            if stock is not None:
                stock_val = getattr(stock, "value", None) or getattr(
                    stock, "name", None
                )
            else:
                stock_val = ""

            bom_data.append(
                [
                    p.part_no or "",
                    p.name or "",
                    str(p.qty_required or ""),
                    stock_val,
                    p.notes or "",
                ]
            )

        bom_table = Table(
            bom_data,
            colWidths=[80, 200, 60, 80, 120],
            repeatRows=1,
        )
        bom_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
                    ("TEXTCOLOR", (0, 1), (-1, -1), colors.black),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ]
            )
        )
        story.append(bom_table)
    else:
        story.append(
            Paragraph("No parts have been generated yet for this job.", styles["Normal"])
        )

    story.append(Spacer(1, 24))

    # ---------- Attachments summary ----------
    story.append(Paragraph("Attachments", styles["Heading2"]))
    story.append(Spacer(1, 6))

    if attachments:
        attach_data = [["Stored filename", "Original filename", "Uploaded by", "Uploaded at"]]
        for a in attachments:
            uploader = a.uploaded_by.username if a.uploaded_by else ""
            uploaded_at = (
                a.created_at.strftime("%Y-%m-%d %H:%M") if a.created_at else ""
            )
            attach_data.append(
                [
                    a.filename or "",
                    getattr(a, "original_filename", "") or "",
                    uploader,
                    uploaded_at,
                ]
            )

        attach_table = Table(
            attach_data,
            colWidths=[110, 140, 80, 100],
            repeatRows=1,
        )
        attach_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
                    ("TEXTCOLOR", (0, 1), (-1, -1), colors.black),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ]
            )
        )
        story.append(attach_table)
    else:
        story.append(Paragraph("No attachments for this job.", styles["Normal"]))

    # ---------- Build PDF ----------
    doc = SimpleDocTemplate(pdf_path, pagesize=A4)
    doc.build(story)
# ---------------------------------------------------------------------
# Job pack export (ZIP)
# ---------------------------------------------------------------------
@app.route("/jobs/<int:job_id>/export", methods=["GET"])
@login_required
def job_export(job_id):
    job = Job.query.get_or_404(job_id)

    survey_items = (
        SurveyItem.query
        .filter_by(job_id=job.id)
        .order_by(SurveyItem.section, SurveyItem.pos)
        .all()
    )
    parts = (
        PartRequirement.query
        .filter_by(job_id=job.id)
        .order_by(PartRequirement.part_no)
        .all()
    )
    attachments = Attachment.query.filter_by(job_id=job.id).all()

    # Base temp dir for building the pack
    base_dir = tempfile.mkdtemp(prefix=f"job_{job.job_number}_")
    try:
        # 1) PDF summary
        pdf_path = os.path.join(base_dir, f"Job_{job.job_number}_pack.pdf")
        build_job_pack_pdf(job, survey_items, parts, attachments, pdf_path)

        # 2) Survey CSV
        survey_dir = os.path.join(base_dir, "survey")
        os.makedirs(survey_dir, exist_ok=True)
        survey_csv_path = os.path.join(survey_dir, "survey_items.csv")
        with open(survey_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Section",
                    "Pos",
                    "Part no",
                    "Qty",
                    "Name",
                    "Decision",
                    "Notes",
                ]
            )
            for si in survey_items:
                dec = si.decision.name if si.decision is not None else ""
                writer.writerow(
                    [
                        si.section or "",
                        si.pos or "",
                        si.part_no or "",
                        si.qty or "",
                        si.name or "",
                        dec,
                        si.notes or "",
                    ]
                )

        # 3) Parts CSV
        parts_dir = os.path.join(base_dir, "parts")
        os.makedirs(parts_dir, exist_ok=True)
        parts_csv_path = os.path.join(parts_dir, "parts_list.csv")
        with open(parts_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Part no",
                    "Name",
                    "Qty required",
                    "Stock status",
                    "Notes",
                ]
            )
            for p in parts:
                stock = p.stock_status
                if stock is not None:
                    stock_val = getattr(stock, "value", None) or getattr(
                        stock, "name", None
                    )
                else:
                    stock_val = ""
                writer.writerow(
                    [
                        p.part_no or "",
                        p.name or "",
                        p.qty_required or "",
                        stock_val,
                        p.notes or "",
                    ]
                )

        # 4) Attachments folder + index CSV
        attachments_dir = os.path.join(base_dir, "attachments")
        os.makedirs(attachments_dir, exist_ok=True)

        index_csv_path = os.path.join(attachments_dir, "attachments_index.csv")
        with open(index_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Stored filename",
                    "Original filename",
                    "Uploaded by",
                    "Uploaded at",
                ]
            )
            for a in attachments:
                uploader = a.uploaded_by.username if a.uploaded_by else ""
                writer.writerow(
                    [
                        a.filename,
                        getattr(a, "original_filename", "") or "",
                        uploader,
                        a.created_at.isoformat() if a.created_at else "",
                    ]
                )

                # Copy actual file into the zip structure if it exists
                if a.filename:
                    src = os.path.join(UPLOAD_FOLDER, a.filename)
                    if os.path.exists(src):
                        shutil.copy(src, os.path.join(attachments_dir, a.filename))

        # 5) Zip the whole pack
        zip_basename = f"Job_{job.job_number}_pack"
        zip_path = shutil.make_archive(
            base_name=os.path.join(base_dir, zip_basename),
            format="zip",
            root_dir=base_dir,
        )

        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{zip_basename}.zip",
        )
    finally:
        # Clean up temp directory after sending
        shutil.rmtree(base_dir, ignore_errors=True)

# ---------------------------------------------------------------------
# Build view (placeholder)
# ---------------------------------------------------------------------
@app.route("/jobs/<int:job_id>/build")
@login_required
def job_build(job_id):
    job = Job.query.get_or_404(job_id)
    return render_template("job_build.html", job=job)


# ---------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------
@app.route("/jobs/<int:job_id>/attachments", methods=["GET", "POST"])
@login_required
def job_attachments(job_id):
    job = Job.query.get_or_404(job_id)

    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or upload.filename == "":
            flash("No file selected.", "danger")
            return redirect(url_for("job_attachments", job_id=job.id))

        rel_path = save_uploaded_file(upload, subdir=str(job.id))
        att = Attachment(
            job_id=job.id,
            filename=upload.filename,
            file_path=rel_path,
        )
        db.session.add(att)
        db.session.commit()
        flash("Attachment uploaded.", "success")
        return redirect(url_for("job_attachments", job_id=job.id))

    attachments = Attachment.query.filter_by(job_id=job.id).all()
    return render_template("job_attachments.html", job=job, attachments=attachments)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ---------------------------------------------------------------------
# Admin – users
# ---------------------------------------------------------------------
@app.route("/admin/users")
@login_required
@roles_required(RoleEnum.ADMIN.value)
def users_list():
    """
    List all users for admin.
    """
    users = User.query.order_by(User.username).all()
    return render_template("users_list.html", users=users)


@app.route("/admin/users/create", methods=["GET", "POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def user_create():
    """
    Create a new user (admin only).
    """
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        role = request.form.get("role")

        # Basic validation
        if not username or not password or not role:
            flash("All fields are required.", "danger")
            return redirect(url_for("user_create"))

        # Enforce unique usernames
        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "danger")
            return redirect(url_for("user_create"))

        # Role must be one of our enum values
        valid_roles = [r.value for r in RoleEnum]
        if role not in valid_roles:
            flash("Invalid role selected.", "danger")
            return redirect(url_for("user_create"))

        user = User(
            username=username,
            role=role,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()
        flash("User created.", "success")
        return redirect(url_for("users_list"))

    # GET
    roles = list(RoleEnum)
    return render_template("user_create.html", RoleEnum=RoleEnum)


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def user_delete(user_id):
    """
    Delete a user (cannot delete yourself).
    """
    # Prevent self-delete foot-gun
    if user_id == session.get("user_id"):
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("users_list"))

    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash("User deleted.", "success")
    return redirect(url_for("users_list"))


# ---------------------------------------------------------------------
# Template builder (Admin only)
# ---------------------------------------------------------------------
@app.route("/admin/templates")
@login_required
@roles_required(RoleEnum.ADMIN.value)
def templates_list():
    templates = Template.query.order_by(Template.name).all()
    return render_template("templates_list.html", templates=templates)


@app.route("/admin/templates/create", methods=["GET", "POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def template_create():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        engine_type = request.form.get("engine_type", "").strip()

        if not name:
            flash("Template name is required.", "danger")
            return redirect(url_for("template_create"))

        if Template.query.filter_by(name=name).first():
            flash("A template with that name already exists.", "danger")
            return redirect(url_for("template_create"))

        template = Template(name=name, engine_type=engine_type or None)
        db.session.add(template)
        db.session.commit()
        flash("Template created.", "success")
        return redirect(url_for("templates_list"))

    return render_template("template_create.html")


@app.route("/admin/templates/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def template_edit(template_id):
    """
    Edit a template header + its lines.
    Also supports:
      - Importing a CSV/XLSX parts list (action=import_sheet)
      - Assigning a single TemplateGroup via the 'Kit name' dropdown.
    """
    template = Template.query.get_or_404(template_id)

    items = (
        TemplateItem.query.filter_by(template_id=template.id)
        .order_by(TemplateItem.section, TemplateItem.pos, TemplateItem.id)
        .all()
    )

    template_groups = (
        TemplateGroup.query.filter_by(template_id=template.id)
        .order_by(TemplateGroup.label)
        .all()
    )

    # Import sheet (blocked when template is locked)
    if (
        request.method == "POST"
        and request.form.get("action") == "import_sheet"
        and not template.is_locked
    ):
        uploaded = request.files.get("file")
        if not uploaded or uploaded.filename == "":
            flash("No file selected for import.", "danger")
            return redirect(url_for("template_edit", template_id=template.id))

        filename = uploaded.filename.lower()
        try:
            if filename.endswith(".csv"):
                df = pd.read_csv(uploaded)
            else:
                df = pd.read_excel(uploaded)
        except Exception as exc:
            flash(f"Could not read file: {exc}", "danger")
            return redirect(url_for("template_edit", template_id=template.id))

        df.columns = [c.strip().lower() for c in df.columns]

        def col(*names):
            for n in names:
                key = n.lower().strip()
                if key in df.columns:
                    return df[key]
            for n in names:
                key = n.lower().strip()
                for colname in df.columns:
                    if key in colname:
                        return df[colname]
            return None

        col_section = col("section", "section code", "section_name")
        col_pos = col("pos", "position", "item", "item no")
        col_part = col("part_no", "part no", "partno", "part number", "partnumber", "part")
        col_name = col("name", "description", "desc", "part name", "item description")
        col_qty = col("qty", "quantity", "qty req", "qty required")

        if col_section is None or col_part is None or col_name is None:
            flash(
                "Import failed: file must contain at least Section, Part No, and Name columns.",
                "danger",
            )
            return redirect(url_for("template_edit", template_id=template.id))

        TemplateItem.query.filter_by(template_id=template.id).delete()
        db.session.flush()

        for idx in range(len(df)):
            section = str(col_section.iloc[idx]) if not pd.isna(col_section.iloc[idx]) else ""
            part_no = str(col_part.iloc[idx]) if not pd.isna(col_part.iloc[idx]) else ""
            name = str(col_name.iloc[idx]) if not pd.isna(col_name.iloc[idx]) else ""

            if not part_no and not name:
                continue

            pos_val = None
            if col_pos is not None and not pd.isna(col_pos.iloc[idx]):
                try:
                    pos_val = int(col_pos.iloc[idx])
                except (ValueError, TypeError):
                    pos_val = None

            qty_val = 1
            if col_qty is not None and not pd.isna(col_qty.iloc[idx]):
                try:
                    qty_val = int(col_qty.iloc[idx])
                except (ValueError, TypeError):
                    qty_val = 1

            t_item = TemplateItem(
                template_id=template.id,
                section=section,
                pos=pos_val,
                part_no=part_no,
                name=name,
                qty=qty_val,
            )
            db.session.add(t_item)

        db.session.commit()
        flash("Template lines replaced from imported parts list.", "success")
        return redirect(url_for("template_edit", template_id=template.id))

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        name = (request.form.get("name") or "").strip()
        engine_type = (request.form.get("engine_type") or "").strip()
        locked_flag = request.form.get("locked")
        new_group_label = (request.form.get("new_group_label") or "").strip()

        if name:
            template.name = name
        template.engine_type = engine_type or None
        template.is_locked = bool(locked_flag)

        # Add kit name (TemplateGroup) with safe, unique code
        if new_group_label and (not getattr(template, "is_locked", False)):
            safe = "".join(
                ch.lower() if ch.isalnum() else "_" for ch in new_group_label
            ).strip("_") or "kit"
            base_code = safe[:32]
            code = base_code
            counter = 1
            while TemplateGroup.query.filter_by(template_id=template.id, code=code).first() is not None:
                counter += 1
                code = f"{base_code}_{counter}"
            existing = TemplateGroup.query.filter_by(
                template_id=template.id, label=new_group_label
            ).first()
            if not existing:
                new_group = TemplateGroup(
                    template_id=template.id,
                    code=code,
                    label=new_group_label,
                    type="other",
                )
                db.session.add(new_group)

        items = (
            TemplateItem.query.filter_by(template_id=template.id)
            .order_by(TemplateItem.section, TemplateItem.pos, TemplateItem.id)
            .all()
        )

        for item in items:
            if template.is_locked:
                break
            item.section = request.form.get(f"section_{item.id}") or ""
            pos_val = request.form.get(f"pos_{item.id}")
            try:
                item.pos = int(pos_val) if pos_val else None
            except ValueError:
                item.pos = None
            ...

            item.part_no = request.form.get(f"part_no_{item.id}") or ""
            item.name = request.form.get(f"name_{item.id}") or ""
            qty_val = request.form.get(f"qty_{item.id}")
            try:
                item.qty = int(qty_val) if qty_val else 1
            except ValueError:
                item.qty = 1

            item.always_replace = bool(request.form.get(f"always_replace_{item.id}"))
            item.in_kit = bool(request.form.get(f"in_kit_{item.id}"))
            item.big_ticket = bool(request.form.get(f"big_ticket_{item.id}"))
            item.requires_test = bool(request.form.get(f"requires_test_{item.id}"))
            item.requires_measurement = bool(
                request.form.get(f"requires_measurement_{item.id}")
            )

            item.measurement_label = (
                request.form.get(f"measurement_label_{item.id}") or None
            )
            item.measurement_unit = (
                request.form.get(f"measurement_unit_{item.id}") or None
            )

            min_val = request.form.get(f"measurement_min_{item.id}")
            max_val = request.form.get(f"measurement_max_{item.id}")
            try:
                item.measurement_min = float(min_val) if (min_val not in (None, "")) else None
            except ValueError:
                item.measurement_min = None
            try:
                item.measurement_max = float(max_val) if (max_val not in (None, "")) else None
            except ValueError:
                item.measurement_max = None

            group_val = request.form.get(f"group_{item.id}") or ""
            if group_val:
                try:
                    grp = TemplateGroup.query.get(int(group_val))
                except ValueError:
                    grp = None
                if grp:
                    item.groups = [grp]
                else:
                    item.groups = []
            else:
                item.groups = []

        if action == "add_row" and not template.is_locked:
            new_section = request.form.get("new_section") or ""
            new_pos = request.form.get("new_pos")
            new_part_no = request.form.get("new_part_no") or ""
            new_name = request.form.get("new_name") or ""
            new_qty = request.form.get("new_qty")

            try:
                new_pos_int = int(new_pos) if new_pos else None
            except ValueError:
                new_pos_int = None

            try:
                new_qty_int = int(new_qty) if new_qty else 1
            except ValueError:
                new_qty_int = 1

            new_item = TemplateItem(
                template_id=template.id,
                section=new_section,
                pos=new_pos_int,
                part_no=new_part_no,
                name=new_name,
                qty=new_qty_int,
                always_replace=bool(request.form.get("new_always_replace")),
                in_kit=bool(request.form.get("new_in_kit")),
                big_ticket=bool(request.form.get("new_big_ticket")),
                requires_test=bool(request.form.get("new_requires_test")),
            )
            db.session.add(new_item)

        db.session.commit()
        flash("Template updated.", "success")

        if action == "add_row":
            return redirect(url_for("template_edit", template_id=template.id))
        return redirect(url_for("templates_list"))

    return render_template(
        "template_edit.html",
        template=template,
        items=items,
        template_groups=template_groups,
    )


@app.route("/admin/templates/<int:template_id>/delete", methods=["POST"])
@login_required
@roles_required(RoleEnum.ADMIN.value)
def template_delete(template_id):
    template = Template.query.get_or_404(template_id)
    db.session.delete(template)
    db.session.commit()
    flash("Template deleted.", "success")
    return redirect(url_for("templates_list"))


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5001)