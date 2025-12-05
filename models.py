from datetime import datetime
from enum import Enum
import enum

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ─────────────────────────────────────────
# Enums
# ─────────────────────────────────────────


class RoleEnum(str, Enum):
    TECHNICIAN = "Technician"
    OFFICE = "Office"
    STORES = "Stores"
    ADMIN = "Admin"


class JobStatusEnum(str, Enum):
    PRE_SURVEY = "pre_survey"
    SURVEY = "survey"
    OFFICE = "office"
    BUILD = "build"
    COMPLETE = "complete"


class DecisionEnum(str, Enum):
    REUSE = "reuse"
    REPLACE = "replace"
    SCRAP = "scrap"
    INSPECT = "inspect"
    NEW = "new"


class StockStatusEnum(str, Enum):
    PENDING = "pending"
    IN_STOCK = "in_stock"
    TO_ORDER = "to_order"
    ORDERED = "ordered"
    RECEIVED = "received"
    FITTED = "fitted"  # for when a part has been used/fitted
    NOT_REQUIRED = "not_required"  # for cancelled / no longer needed


class MeasurementResultEnum(str, Enum):
    WITHIN_SPEC = "within_spec"
    OUT_OF_SPEC = "out_of_spec"
    NOT_MEASURED = "not_measured"


class PickListStatusEnum(str, Enum):
    NEW = "new"
    PICKED = "picked"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


# ─────────────────────────────────────────
# Core models
# ─────────────────────────────────────────


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.Enum(RoleEnum), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # All jobs this user is assigned to as technician
    assigned_jobs = db.relationship(
        "Job",
        foreign_keys="Job.assigned_to_id",
        back_populates="assigned_to",
        lazy="dynamic",
    )


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    job_number = db.Column(db.String(64), unique=True, nullable=False)
    customer_name = db.Column(db.String(128))
    engine_type = db.Column(db.String(128))
    serial_number = db.Column(db.String(128))  # engine/part serial

    status = db.Column(
        db.Enum(JobStatusEnum),
        nullable=False,
        default=JobStatusEnum.PRE_SURVEY,
    )
    survey_layout = db.Column(
        db.String(32),
        nullable=False,
        default="classic",
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    # 🔹 Link to template (Job Pack spec)
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("templates.id"),
        nullable=True,
    )

    # 🔹 Tech assigned to this job
    assigned_to_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
    )

    # 🔹 Who created the job pack (Office/Admin)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
    )

    # Relationships
    template = db.relationship("Template", back_populates="jobs")

    assigned_to = db.relationship(
        "User",
        foreign_keys=[assigned_to_id],
        back_populates="assigned_jobs",
    )

    created_by = db.relationship(
        "User",
        foreign_keys=[created_by_id],
    )

    # Pick lists issued for this job
    pick_lists = db.relationship(
        "PickList",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


class SurveyItem(db.Model):
    __tablename__ = "survey_items"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)

    section = db.Column(db.String(64))
    pos = db.Column(db.Integer)
    part_no = db.Column(db.String(64))
    name = db.Column(db.String(128))
    qty = db.Column(db.Integer, default=1)

    # Flags
    always_replace = db.Column(db.Boolean, default=False)  # 100% replacement
    in_kit = db.Column(db.Boolean, default=False)  # supplied as part of a kit

    # NEW: behaviour flags copied from TemplateItem
    big_ticket = db.Column(db.Boolean, default=False)       # menu / upsell item
    requires_test = db.Column(db.Boolean, default=False)    # needs test before order

    requires_measurement = db.Column(
        db.Boolean,
        default=False,
    )  # measurement needed

    # NEW: kit behaviour used by BOM kit logic
    kit_name = db.Column(db.String(128))        # label that groups lines into a kit
    kit_master = db.Column(db.Boolean, default=False)  # is this the “boxed” kit line?

    # Survey data
    condition = db.Column(db.String(32))
    decision = db.Column(db.Enum(DecisionEnum))
    notes = db.Column(db.Text)

    # Measurements (actual recorded for this job)
    measurement_value = db.Column(db.String(32))
    measurement_unit = db.Column(db.String(16))
    spec_min = db.Column(db.String(32))
    spec_max = db.Column(db.String(32))
    measurement_result = db.Column(
        db.Enum(MeasurementResultEnum),
        default=MeasurementResultEnum.NOT_MEASURED,
    )


class PartRequirement(db.Model):
    """
    BOM / parts list line per job.
    Generated from SurveyItem when decision == REPLACE (or always_replace),
    then owned/edited by Office.
    """

    __tablename__ = "part_requirements"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)

    # Link back to the survey line that created this BOM line
    survey_item_id = db.Column(
        db.Integer,
        db.ForeignKey("survey_items.id"),
        nullable=True,
    )
    survey_item = db.relationship(
        "SurveyItem",
        backref="part_requirement",
        uselist=False,
    )

    # Copy of the survey/template meta at time of BOM generation
    section = db.Column(db.String(64))  # TA / MAN group
    pos = db.Column(db.Integer)  # positional index within section
    part_no = db.Column(db.String(120))
    name = db.Column(db.String(255))

    # How many we actually need to order/use
    qty_required = db.Column(db.Integer, default=1)

    # BOM / stock status for this job/line
    stock_status = db.Column(
        db.Enum(StockStatusEnum),
        nullable=False,
        default=StockStatusEnum.TO_ORDER,
    )

    supplier = db.Column(db.String(120))
    po_number = db.Column(db.String(120))
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class Attachment(db.Model):
    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    filename = db.Column(db.String(255))
    file_path = db.Column(db.String(255))
    file_type = db.Column(db.String(64))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────
# Templates
# ─────────────────────────────────────────


class Template(db.Model):
    __tablename__ = "templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, nullable=False)
    engine_type = db.Column(db.String(128))

    # Lock flag – when True, only Admin can edit this template
    is_locked = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    items = db.relationship(
        "TemplateItem",
        backref="template",
        cascade="all, delete-orphan",
    )

    # Logical grouping of template lines (measured items, kits, big ticket, etc.)
    groups = db.relationship(
        "TemplateGroup",
        backref="template",
        cascade="all, delete-orphan",
    )

    # 🔹 All jobs that use this template as their Job Pack spec
    jobs = db.relationship(
        "Job",
        back_populates="template",
    )


class TemplateItem(db.Model):
    __tablename__ = "template_items"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("templates.id"),
        nullable=False,
    )

    section = db.Column(db.String(64))
    pos = db.Column(db.Integer)
    part_no = db.Column(db.String(64))
    name = db.Column(db.String(128))
    qty = db.Column(db.Integer, default=1)

    # Flags at template level
    always_replace = db.Column(db.Boolean, default=False)  # 100% replacement
    in_kit = db.Column(db.Boolean, default=False)          # part comes inside a kit

    # NEW: behaviour flags
    big_ticket = db.Column(db.Boolean, default=False)      # menu / upsell item
    requires_test = db.Column(db.Boolean, default=False)   # needs test before decision

    requires_measurement = db.Column(
        db.Boolean,
        default=False,
    )  # needs measurement row

    # Measurement spec (one spec per template line)
    measurement_label = db.Column(db.String(255))  # e.g. "Big end bore"
    measurement_unit = db.Column(db.String(32))    # e.g. "mm", "µm"
    measurement_min = db.Column(db.Float)          # lower limit
    measurement_max = db.Column(db.Float)          # upper limit

    # Many-to-many groups (big ticket, measured set, kit, etc.)
    groups = db.relationship(
        "TemplateGroup",
        secondary="template_item_groups",
        back_populates="items",
        lazy="joined",
    )

    # One-to-one stores overlay
    store_item = db.relationship(
        "StoreItem",
        backref="template_item",
        uselist=False,
        cascade="all, delete-orphan",
    )


class TemplateGroup(db.Model):
    """
    Logical grouping of template lines inside a template.

    Examples:
      - "Measured items – critical"
      - "Big ticket items"
      - "KIT 200123 – gasket set"
      - "Electricals for test"
    """

    __tablename__ = "template_groups"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("templates.id"),
        nullable=False,
    )

    # Short internal reference (used to avoid accidental duplicates per template)
    code = db.Column(db.String(64), nullable=False)

    # Nice display label for the UI
    label = db.Column(db.String(255), nullable=False)

    # Category / behaviour hint:
    # 'measure', 'big_ticket', 'kit', 'test_electrical', 'other', etc.
    type = db.Column(db.String(32), nullable=False, default="other")

    # Optional: parent kit / assembly part number
    parent_part_no = db.Column(db.String(64))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # All template items that belong to this group
    items = db.relationship(
        "TemplateItem",
        secondary="template_item_groups",
        back_populates="groups",
    )


class TemplateItemGroup(db.Model):
    """
    Join table: many TemplateItems ↔ many TemplateGroups.
    """

    __tablename__ = "template_item_groups"

    template_item_id = db.Column(
        db.Integer,
        db.ForeignKey("template_items.id"),
        primary_key=True,
    )
    template_group_id = db.Column(
        db.Integer,
        db.ForeignKey("template_groups.id"),
        primary_key=True,
    )


class StoreItem(db.Model):
    """
    Lite stores overlay: one row per TemplateItem.

    Uses the template as the full parts catalogue, and just tracks
    bin + stock against each template line.
    """

    __tablename__ = "store_items"

    id = db.Column(db.Integer, primary_key=True)

    # One-to-one with TemplateItem
    template_item_id = db.Column(
        db.Integer,
        db.ForeignKey("template_items.id"),
        nullable=False,
        unique=True,
    )

    # Convenience copy so we can filter by engine later if we want
    engine_type = db.Column(db.String(128))

    bin_location = db.Column(db.String(64))
    qty_in_stock = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


# ─────────────────────────────────────────
# Pick lists (Stores inbox)
# ─────────────────────────────────────────


class PickList(db.Model):
    __tablename__ = "pick_lists"

    id = db.Column(db.Integer, primary_key=True)

    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    job = db.relationship("Job", back_populates="pick_lists")

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship(
        "User",
        backref="pick_lists_created",
    )

    status = db.Column(
        db.Enum(PickListStatusEnum),
        default=PickListStatusEnum.NEW,
        nullable=False,
    )


class PickListItem(db.Model):
    __tablename__ = "pick_list_items"

    id = db.Column(db.Integer, primary_key=True)

    pick_list_id = db.Column(
        db.Integer,
        db.ForeignKey("pick_lists.id"),
        nullable=False,
    )
    pick_list = db.relationship(
        "PickList",
        backref=db.backref(
            "items",
            cascade="all, delete-orphan",
            lazy="joined",
        ),
    )

    part_no = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(255))
    bin_location = db.Column(db.String(64))

    qty_required = db.Column(db.Integer, default=0, nullable=False)
    stock_at_issue = db.Column(db.Integer, default=0, nullable=False)


# ─────────────────────────────────────────
# Stores foundations – EngineType & Part catalogue
# (used by both BOM and future Stores module)
# ─────────────────────────────────────────

# Association table for many-to-many relation:
# one part can belong to several engine types, one engine type has many parts.
engine_type_parts = db.Table(
    "engine_type_parts",
    db.Column(
        "engine_type_id",
        db.Integer,
        db.ForeignKey("engine_types.id"),
        primary_key=True,
    ),
    db.Column(
        "part_id",
        db.Integer,
        db.ForeignKey("parts.id"),
        primary_key=True,
    ),
)


class EngineTypeModel(db.Model):
    # renamed to EngineTypeModel to avoid clashing with Job.engine_type string
    """
    Master record for an engine family/type used in Stores / catalogue.

    This is separate from Job.engine_type string so we don't break your existing data.
    You can gradually wire Jobs/Templates to this later.
    """

    __tablename__ = "engine_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(128), unique=True, nullable=False)  # e.g. MAN_D0836_LF
    name = db.Column(db.String(255), nullable=False)  # e.g. MAN D0836 LF Engine

    parts = db.relationship(
        "Part",
        secondary=engine_type_parts,
        back_populates="engine_types",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<EngineTypeModel code={self.code!r}>"


class Part(db.Model):
    """
    Master part catalogue record across engine types.

    Populated from Template/TemplateItem when an engine template is synced
    into the Stores catalogue.
    """

    __tablename__ = "parts"

    id = db.Column(db.Integer, primary_key=True)
    part_no = db.Column(db.String(128), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    unit = db.Column(db.String(32), nullable=True)

    section = db.Column(db.String(64), nullable=True)
    pos = db.Column(db.Integer, nullable=True)
    qty_per_engine = db.Column(db.Integer, nullable=True)

    # From template flags
    is_always_required = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )  # 100% item
    is_in_kit = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )  # supplied as part of a kit

    # Raw OEM / template metadata if we want to keep more detail
    oem_meta = db.Column(db.JSON, nullable=True)

    engine_types = db.relationship(
        "EngineTypeModel",
        secondary=engine_type_parts,
        back_populates="parts",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Part part_no={self.part_no!r} name={self.name!r}>"
    
 