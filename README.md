Reman Assistant – Gold Build (Dec 2025)

A technician-built web application for managing engine remanufacturing jobs.

Every engine gets one digital Job Pack that contains:
	•	Structured strip survey (template-driven)
	•	Auto-generated BOM from survey decisions
	•	Parts status (pending → ordered → received → in stock)
	•	Attachments (photos, approvals, invoices, certs)
	•	Full traceability from strip → office → build → test → completion

This repository contains the Gold working build currently used day-to-day on the shop floor.

⸻

Overview

Reman Assistant replaces paper surveys, spreadsheets, and WhatsApp photos with a single source of truth for each engine.

It is built specifically remanufacturing workflow and focuses on:
	•	Technician speed and clarity
	•	Office accuracy and communication
	•	Stores visibility of parts requirements
	•	Traceability and accountability for ISO-style processes

⸻

Tech Stack
	•	Python 3 / Flask
	•	Flask-SQLAlchemy
	•	SQLite (local development DB)
	•	Jinja2 templating
	•	HTML/CSS dark-mode technician-friendly UI

No external cloud services. Runs fully locally or on an internal server.

⸻

Job Flow Summary
	1.	Office creates a Job
	•	Job number, customer, engine type, survey template.
	2.	Technician performs the Strip Survey
	•	Section diagrams visible during the process
	•	Decisions: Reuse / Replace / Scrap / Inspect
	•	Big-ticket and test items clearly highlighted
	•	Measurement capture with template-defined limits
	3.	Office reviews & processes the BOM
	•	Automatically generated after survey completion
	•	Grouped by section and kit
	•	Parts marked through: to order → ordered → received → in stock
	4.	Build → Test → Completion
	•	Job card updates in real time
	•	Job Pack becomes the full audit trail

⸻

How to Run Locally
	1.	Clone the repository

	•	git clone https://github.com/richbrotherton87-svg/reman_assistant_gold.git
	•	cd reman_assistant_gold

	2.	Create and activate a virtual environment

	•	python3 -m venv .venv
	•	source .venv/bin/activate

	3.	Install dependencies

	•	pip install -r requirements.txt

	4.	Initialise the database (first time only)

	•	python init_db.py

	5.	Run the application

	•	python app.py

Then open: http://127.0.0.1:5000

Default login (development only):
	•	Username: admin
	•	Password: admin

.
├── app.py                # Flask application, routes, core logic
├── models.py             # SQLAlchemy models for all tables
├── init_db.py            # Database initialisation + admin seeding
│
├── templates/            # Jinja2 templates (UI pages)
│   ├── base.html
│   ├── dashboard.html
│   ├── job_detail.html
│   ├── job_survey.html
│   ├── job_parts.html
│   └── job_pack.html
│
├── static/               # CSS, JS, icons
│   └── style.css
│
├── uploads/              # Diagrams + photos (ignored by Git)
│
├── requirements.txt      # Frozen dependencies for this build
└── README.md             # This documentation

Current Status (Gold Build)
	•	Fully working survey → BOM → build pipeline
	•	Template logic functioning
	•	Section image viewer active
	•	Big-ticket & test-item tinting live
	•	End-to-end UI stable
	•	Suitable for a 5-terminal pilot with server-backed database
	•	Codebase Git-controlled and ready for collaborative development

⸻

Future Improvements (not required for current use)
	•	Audit logging for ISO-style traceability
	•	Stores master integration
	•	Postgres backend for multi-user production
	•	Nginx/Gunicorn deployment setup
	•	Job timeline view
	•	KPI dashboard

Made with pride by
Rich Brotherton – Engine Remanufacturing Technician