# scripts/test_jobs.py

import random
from statistics import mean

from app import create_app, ensure_survey_from_template
from models import (
    db,
    Job,
    Template,
    SurveyItem,
    PartRequirement,
    JobStatusEnum,
    DecisionEnum,
)

NUM_JOBS = 30  # change if you want more/less


def pick_template():
    tmpl = Template.query.first()
    if not tmpl:
        raise RuntimeError("No templates found in the database.")
    return tmpl


def clear_old_test_jobs():
    """
    Remove any previous TEST-* jobs so we don't hit the UNIQUE constraint
    on jobs.job_number.
    """
    deleted = (
        Job.query.filter(Job.job_number.like("TEST-%"))
        .delete(synchronize_session=False)
    )
    db.session.commit()
    print(f"Deleted {deleted} old TEST-* jobs.")


def create_test_jobs(template, num_jobs):
    jobs = []
    for i in range(num_jobs):
        j = Job(
            job_number=f"TEST-{i+1:04d}",
            customer_name="Test customer",
            engine_type=template.engine_type or "test",
            serial_number=f"SERIAL-{i+1:04d}",
            status=JobStatusEnum.PRE_SURVEY,
            template_id=template.id,
        )
        db.session.add(j)
        jobs.append(j)
    db.session.commit()
    return jobs


def autofill_survey(job):
    """
    Mimic your admin_autofill_survey helper, but in script form.
    """
    ensure_survey_from_template(job)
    items = SurveyItem.query.filter_by(job_id=job.id).all()

    for item in items:
        if item.always_replace:
            # 100% items ⇒ always replace
            item.decision = DecisionEnum.REPLACE
        else:
            # Random-ish decisions for demo purposes
            item.decision = random.choice(
                [DecisionEnum.REUSE, DecisionEnum.REPLACE, DecisionEnum.SCRAP]
            )

    db.session.commit()
    return items


def build_bom_offline(job, survey_items):
    """
    Offline version of your BOM builder that does NOT use request.form
    (so it works without a Flask request context).

    Logic:
      - Clear existing PartRequirement for this job.
      - For each SurveyItem:
          - if always_replace OR decision in (REPLACE, SCRAP) ⇒ create BOM line
          - qty = item.qty or 1   (no per-line override here, because we're not
            inside a real HTTP POST from the survey form).
      - Set job.status = OFFICE.
    """
    # Clear any previous BOM
    PartRequirement.query.filter_by(job_id=job.id).delete()
    db.session.flush()

    created = 0

    for item in survey_items:
        decision = item.decision
        always = bool(item.always_replace)

        if not (always or decision in (DecisionEnum.REPLACE, DecisionEnum.SCRAP)):
            continue

        qty = item.qty or 1

        bom_line = PartRequirement(
            job_id=job.id,
            survey_item_id=item.id,
            section=item.section,
            pos=item.pos,
            part_no=item.part_no,
            name=item.name,
            qty_required=qty,
        )
        db.session.add(bom_line)
        created += 1

    job.status = JobStatusEnum.OFFICE
    db.session.commit()

    parts = PartRequirement.query.filter_by(job_id=job.id).all()
    return created, parts


def run():
    app = create_app()
    with app.app_context():
        template = pick_template()
        print(f"Using template: {template.name} (id={template.id})")

        # Make sure we don't collide with previous TEST-* runs
        clear_old_test_jobs()

        jobs = create_test_jobs(template, NUM_JOBS)

        per_job = []
        for job in jobs:
            survey_items = autofill_survey(job)
            created, bom = build_bom_offline(job, survey_items)

            total_lines = len(survey_items)
            bom_lines = len(bom)
            needed_lines = sum(
                1
                for si in survey_items
                if si.always_replace
                or si.decision in (DecisionEnum.REPLACE, DecisionEnum.SCRAP)
            )

            mismatch = bom_lines != needed_lines

            per_job.append(
                {
                    "job_number": job.job_number,
                    "total_survey_lines": total_lines,
                    "needed_lines": needed_lines,
                    "bom_lines": bom_lines,
                    "created": created,
                    "mismatch": mismatch,
                }
            )

        # ───────────────── Summary ─────────────────
        print("\n=== Test summary ===")
        print(f"Jobs tested:         {len(per_job)}")
        mismatches = [r for r in per_job if r["mismatch"]]
        print(f"Mismatches found:    {len(mismatches)}")

        if per_job:
            print(f"Avg survey lines:    {mean(r['total_survey_lines'] for r in per_job):.1f}")
            print(f"Avg needed lines:    {mean(r['needed_lines'] for r in per_job):.1f}")
            print(f"Avg BOM lines:       {mean(r['bom_lines'] for r in per_job):.1f}")
            print(f"Avg created (BOM):   {mean(r['created'] for r in per_job):.1f}")

        if mismatches:
            print("\nJobs where BOM != expected needed lines:")
            for r in mismatches:
                print(
                    f"  {r['job_number']}: "
                    f"survey={r['total_survey_lines']} "
                    f"needed={r['needed_lines']} "
                    f"bom={r['bom_lines']} "
                    f"(created={r['created']})"
                )
        else:
            print("\nAll jobs had matching needed_lines and BOM lines. ✔")


if __name__ == "__main__":
    run()