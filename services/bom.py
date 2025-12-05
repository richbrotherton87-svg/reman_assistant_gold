# services/bom.py
from datetime import datetime

from models import (
    db,
    Job,
    SurveyItem,
    PartRequirement,
    DecisionEnum,
    StockStatusEnum,
)


def generate_bom_for_job(job_id: int, form_data) -> None:
    """
    Build the Parts / BOM list for a job, based on its SurveyItems.

    - Lines where decision == REPLACE (or always_replace locked as REPLACE)
      become BOM lines.
    - Kit logic:
        * SurveyItem.kit_name groups lines into a 'kit'
        * SurveyItem.kit_master marks the kit's boxed part
        * If a kit_master line is REPLACE, we keep ONLY that line
          and drop the other lines in the same kit_name group from the BOM.
        * If there is no master, or master isn't REPLACE, all lines behave
          normally (no suppression).

    Existing PartRequirement rows for this job are wiped first.
    """

    job = Job.query.get(job_id)
    if not job:
        return

    # 1) Wipe existing BOM for this job
    PartRequirement.query.filter_by(job_id=job.id).delete()
    db.session.flush()

    # 2) Load survey items fresh from DB
    survey_items = (
        SurveyItem.query.filter_by(job_id=job.id)
        .order_by(SurveyItem.section, SurveyItem.pos, SurveyItem.id)
        .all()
    )

    candidate_lines = []  # list of (survey_item, part_requirement)

    for s in survey_items:
        # Treat always_replace as REPLACE, regardless of dropdown
        decision = s.decision
        if s.always_replace and decision is None:
            decision = DecisionEnum.REPLACE

        # Only REPLACE lines generate BOM rows at this stage
        if decision != DecisionEnum.REPLACE:
            continue

        qty = s.qty or 0
        if qty <= 0:
            continue

        p = PartRequirement(
            job_id=job.id,
            survey_item_id=s.id,
            section=s.section,
            pos=s.pos,
            part_no=s.part_no,
            name=s.name,
            qty_required=qty,
            stock_status=StockStatusEnum.TO_ORDER,
            supplier=None,
            po_number=None,
            notes=None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        candidate_lines.append((s, p))

    # 3) Kit suppression logic
    # Group candidate lines by kit_name
    kits = {}
    for s, p in candidate_lines:
        if not s.kit_name:
            continue
        info = kits.setdefault(
            s.kit_name,
            {"master": None, "members": []},
        )
        info["members"].append((s, p))
        if s.kit_master:
            info["master"] = (s, p)

    # Decide which lines to keep
    keep = set()  # indices in candidate_lines

    # First, mark all non-kit lines to keep
    for idx, (s, p) in enumerate(candidate_lines):
        if not s.kit_name:
            keep.add(idx)

    # Now process each kit group
    for kit_name, info in kits.items():
        master_pair = info["master"]
        members = info["members"]

        if master_pair is None:
            # No kit master defined: keep all members
            for s, p in members:
                idx = candidate_lines.index((s, p))
                keep.add(idx)
            continue

        master_s, master_p = master_pair

        # Only collapse if master is REPLACE
        master_decision = master_s.decision
        if master_s.always_replace and master_decision is None:
            master_decision = DecisionEnum.REPLACE

        if master_decision == DecisionEnum.REPLACE:
            # Keep only the master line
            idx = candidate_lines.index((master_s, master_p))
            keep.add(idx)
        else:
            # Master not being replaced – behave like normal, keep all
            for s, p in members:
                idx = candidate_lines.index((s, p))
                keep.add(idx)

    # 4) Persist kept PartRequirement rows
    for idx, (s, p) in enumerate(candidate_lines):
        if idx not in keep:
            continue
        db.session.add(p)

    db.session.commit()