import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)


def _nice(dt):
    if not dt:
        return "-"
    if isinstance(dt, str):
        return dt
    # Small, human-readable timestamp
    return dt.strftime("%Y-%m-%d %H:%M")


def build_job_pack_pdf(job, survey_items, parts, attachments, output_path):
    """
    Build a single PDF summarising the job pack.

    - Job header (customer, engine, serial, status)
    - Survey summary (counts for Good / Replace / Missing / Undecided)
    - Parts/BOM table
    - Attachments index
    """
    styles = getSampleStyleSheet()
    title = styles["Title"]
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    normal = styles["BodyText"]

    story = []

    # ---------------------------------------------------------
    # Header
    # ---------------------------------------------------------
    story.append(Paragraph(f"Job pack – Job {job.job_number}", title))
    story.append(Spacer(1, 12))

    header_data = [
        ["Customer", job.customer_name or "-"],
        ["Engine", job.engine_type or "-"],
        ["Serial no.", job.serial_number or "-"],
        ["Template", job.template.name if getattr(job, "template", None) else "-"],
        ["Status", job.status.value if job.status else "-"],
        ["Created", _nice(job.created_at)],
        ["Updated", _nice(job.updated_at)],
    ]

    header_table = Table(header_data, colWidths=[90, 350])
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.HexColor("#020617"), colors.HexColor("#030712")]),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#e5e7eb")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#1f2937")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    story.append(header_table)
    story.append(Spacer(1, 18))

    # ---------------------------------------------------------
    # Survey summary: counts by decision
    # ---------------------------------------------------------
    story.append(Paragraph("Strip survey summary", h1))
    story.append(Spacer(1, 6))

    total_lines = len(survey_items)
    good = 0
    replace = 0
    missing = 0
    locked_auto = 0
    undecided = 0

    for si in survey_items:
        dec = getattr(si, "decision", None)
        always_replace = bool(getattr(si, "always_replace", False))

        if always_replace:
            locked_auto += 1

        if not dec:
            undecided += 1
        else:
            name = dec.name if hasattr(dec, "name") else str(dec)
            name = name.upper()
            if "REUSE" in name or "GOOD" in name:
                good += 1
            elif "REPLACE" in name:
                replace += 1
            elif "SCRAP" in name or "MISSING" in name:
                missing += 1

    summary_data = [
        ["Total survey lines", str(total_lines)],
        ["Good / reuse", str(good)],
        ["Replace", str(replace)],
        ["Missing / scrap", str(missing)],
        ["Locked always-replace", str(locked_auto)],
        ["Undecided", str(undecided)],
    ]

    survey_table = Table(summary_data, colWidths=[150, 60])
    survey_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.HexColor("#020617"), colors.HexColor("#030712")]),
                ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#e5e7eb")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#1f2937")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(survey_table)
    story.append(Spacer(1, 18))

    # ---------------------------------------------------------
    # Parts / BOM table
    # ---------------------------------------------------------
    story.append(Paragraph("Parts / BOM", h1))
    story.append(Spacer(1, 4))

    if not parts:
        story.append(Paragraph("No parts have been generated for this job.", normal))
    else:
        bom_rows = [
            ["Part no", "Name", "Qty", "Stock", "Notes"],
        ]

        for p in parts:
            stock = getattr(p, "stock_status", None)
            if stock is not None:
                stock_val = getattr(stock, "value", None) or getattr(stock, "name", None)
            else:
                stock_val = "-"

            bom_rows.append(
                [
                    p.part_no or "",
                    (p.name or "")[:60],
                    str(getattr(p, "qty_required", "") or ""),
                    stock_val or "-",
                    (getattr(p, "notes", "") or "")[:40],
                ]
            )

        bom_table = Table(
            bom_rows,
            colWidths=[80, 210, 30, 60, 110],
            repeatRows=1,
        )
        bom_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("FONTSIZE", (0, 1), (-1, -1), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.HexColor("#020617"), colors.HexColor("#030712")]),
                    ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#e5e7eb")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#1f2937")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (2, 1), (2, -1), "RIGHT"),
                    ("ALIGN", (3, 1), (3, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(bom_table)

    story.append(PageBreak())

    # ---------------------------------------------------------
    # Attachments index
    # ---------------------------------------------------------
    story.append(Paragraph("Attachments", h1))
    story.append(Spacer(1, 4))

    if not attachments:
        story.append(Paragraph("No attachments uploaded for this job.", normal))
    else:
        rows = [["File", "Uploaded by", "Uploaded at"]]
        for a in attachments:
            uploader = getattr(a, "uploaded_by", None)
            if uploader is not None:
                uploaded_by = getattr(uploader, "username", None) or str(uploader)
            else:
                uploaded_by = "-"

            rows.append(
                [
                    getattr(a, "original_filename", None) or a.filename,
                    uploaded_by,
                    _nice(getattr(a, "created_at", None)),
                ]
            )

        att_table = Table(rows, colWidths=[250, 100, 100], repeatRows=1)
        att_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("FONTSIZE", (0, 1), (-1, -1), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.HexColor("#020617"), colors.HexColor("#030712")]),
                    ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#e5e7eb")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#1f2937")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(att_table)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    doc.build(story)