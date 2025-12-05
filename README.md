# Reman Lean Core

Digitised strip survey and job pack workflow for engine reman at Carwood.

## What this version includes

- Job creation with:
  - Job number (unique)
  - Customer name
  - Engine type
  - Serial number
  - Template selection
  - Assigned technician
- Technician strip survey
  - Survey lines seeded from templates
  - Decisions: REUSE / REPLACE / SCRAP
  - Measurement flags and results
- BOM / parts list
  - Generated from survey decisions
  - 100% (always replace) + REPLACE/SCRAP lines only
  - Big-ticket / for-test / kits highlighted in separate cards
- Job pack view
  - Single screen summary of survey, BOM and attachments
- Admin test helpers
  - Autofill survey for demos
  - Mark all lines “Good”
- Test harness
  - Creates 30 TEST-* jobs
  - Compares “needed” vs BOM parts to verify logic

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # on Mac
pip install -r requirements.txt

(.venv) richbrotherton@Richs-MacBook-Pro reman_app_main % python -m scripts.test_jobs
Using template: test (id=1)
Deleted 30 old TEST-* jobs.

=== Test summary ===
Jobs tested:         30
Mismatches found:    0
Avg survey lines:    15.0
Avg needed lines:    11.1
Avg BOM lines:       11.1
Avg created (BOM):   11.1

All jobs had matching needed_lines and BOM lines. ✔