# flow_layout.py
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
    Image, KeepTogether, Table, TableStyle, Flowable
)
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.enums import TA_LEFT
from reportlab.lib import colors

from util_map_and_extract import (
    load_json, build_title_lookup, group_items_detailed, ORDERED_MAJOR,
    MAJOR_TO_SUBSECTION_ORDER, compute_subsection_status
)

# ---------- page furniture ----------

def _on_page(canvas: Canvas, doc, header: dict):
    # Footer: "Report Identification: <address> - <date>"
    address = header.get("address", "")
    date    = header.get("date", "")
    footer  = f"Report Identification: {address} - {date}"
    canvas.setFont("Helvetica", 8)
    canvas.setFillGray(0.3)
    canvas.drawString(0.75*inch, 0.5*inch, footer)

    # Top right legend "I=Inspected  NI=Not Inspected  NP=Not Present  D=Deficient"
    legend = "I=Inspected   NI=Not Inspected   NP=Not Present   D=Deficient"
    w = canvas.stringWidth(legend, "Helvetica", 8)
    canvas.drawString(doc.pagesize[0] - 0.75*inch - w, doc.pagesize[1] - 0.5*inch, legend)

    # page number (bottom right)
    pn = canvas.getPageNumber()
    pn_text = f"Page {pn}"
    wpn = canvas.stringWidth(pn_text, "Helvetica", 8)
    canvas.drawString(doc.pagesize[0] - 0.75*inch - wpn, 0.5*inch, pn_text)

# ---------- styles ----------

def _styles():
    styles = getSampleStyleSheet()
    h_major = ParagraphStyle("h_major", parent=styles["Heading1"], fontName="Helvetica-Bold",
                             fontSize=13, spaceBefore=8, spaceAfter=6)
    h_sub   = ParagraphStyle("h_sub", parent=styles["Heading2"], fontName="Helvetica-Bold",
                             fontSize=11, spaceBefore=8, spaceAfter=4)
    body    = ParagraphStyle("body", parent=styles["BodyText"], fontName="Helvetica",
                             fontSize=10, leading=14, spaceAfter=3)
    label_defect = ParagraphStyle("label_defect", parent=body, textColor=colors.HexColor("#C53030"),
                                  fontName="Helvetica-Bold")
    label_limit  = ParagraphStyle("label_limit",  parent=body, textColor=colors.HexColor("#B7791F"),
                                  fontName="Helvetica-Bold")
    label_info   = ParagraphStyle("label_info",   parent=body, textColor=colors.HexColor("#1A202C"),
                                  fontName="Helvetica-Bold")
    return h_major, h_sub, body, label_defect, label_limit, label_info

def _label_style(t: str, label_defect, label_limit, label_info):
    t = (t or "").lower()
    if t == "defect":
        return label_defect, "■"
    if t == "limit":
        return label_limit, "▲"
    return label_info, "●"  # info/default

# ---------- checklist flowable (draws real squares w/ checkmarks) ----------

class ChecklistLine(Flowable):
    """
    Draw: [ ] I    [ ] NI    [ ] NP    [ ] D
    Checked boxes show a tick mark. This is static page content (not a form field),
    matching the sample’s style where each subsection shows a row of boxes. 
    """
    def __init__(self, status: Dict[str, bool], font_size: float = 9.0):
        super().__init__()
        self.status = status
        self.font_size = font_size
        self.height = font_size + 2
        self.width = 0  # will be computed in draw()

    def draw(self):
        c = self.canv
        c.setFont("Helvetica", self.font_size)
        x = 0
        y = 0  # baseline

        def draw_box(label: str, checked: bool) -> float:
            box = 9  # box size in points
            # square
            c.setLineWidth(0.8)
            c.rect(x, y, box, box, stroke=1, fill=0)
            # tick
            if checked:
                c.setLineWidth(1.2)
                c.line(x+2, y+4, x+4, y+1)
                c.line(x+4, y+1, x+7, y+8)
            # label
            c.drawString(x + box + 4, y+1, label)
            w = box + 4 + c.stringWidth(label, "Helvetica", self.font_size) + 16  # spacing to next
            return w

        x += draw_box("I",  self.status.get("I",  False))
        x += draw_box("NI", self.status.get("NI", False))
        x += draw_box("NP", self.status.get("NP", False))
        x += draw_box("D",  self.status.get("D",  False))
        self.width = x

# ---------- image grid (2–3 per row) ----------

def _image_grid(images: List[tuple], max_width: float = 5.5*inch, cols: int = 3):
    """
    images: list of (local_path, _, _)
    Returns a reportlab Table that lays images out in a clean grid.
    """
    if not images:
        return None
    cell_w = max_width / cols
    rows, row = [], []
    for idx, (imgpath, _, _) in enumerate(images):
        try:
            im = Image(imgpath)
            im._restrictSize(cell_w, 1.7*inch)  # scale to fit grid
            row.append(im)
        except Exception:
            continue
        if len(row) == cols:
            rows.append(row); row = []
    if row:
        # pad last row
        while len(row) < cols:
            row.append(Spacer(1, 1))
        rows.append(row)

    tbl = Table(rows, colWidths=[cell_w]*cols, hAlign='LEFT')
    tbl.setStyle(TableStyle([
        ('LEFTPADDING',  (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING',   (0,0), (-1,-1), 0),
        ('BOTTOMPADDING',(0,0), (-1,-1), 6),
        ('VALIGN',       (0,0), (-1,-1), 'TOP'),
    ]))
    return tbl

# ---------- section flow ----------

def _flow_for_subsection(name: str, items: List[dict], styles, show_status=True):
    h_major, h_sub, body, label_defect, label_limit, label_info = styles
    flow: List = []

    # Subsection header
    flow.append(Paragraph(name, h_sub))

    # Checklist row (I/NI/NP/D) as drawn checkboxes
    if show_status:
        st = compute_subsection_status(items)  # dict: I/NI/NP/D → bool
        flow.append(ChecklistLine(st))
        flow.append(Spacer(1, 4))

    # Each item: bold colored label + comment text + images grid
    for it in items:
        label = it.get("label","Item")
        text  = it.get("text","")
        ctype = (it.get("type") or "info").lower()
        style, icon = _label_style(ctype, label_defect, label_limit, label_info)

        # Label line
        flow.append(Paragraph(f"{icon} <b>{label}</b>", style))
        if text:
            flow.append(Paragraph(text, body))

        # Images (no downloading; local path or data-uri decoded earlier)
        grid = _image_grid(it.get("images", []))
        if grid:
            flow.append(grid)

        flow.append(Spacer(1, 8))

    return flow

# ---------- public API ----------

def build_flow_pdf(json_path: Path|str, mapping_path: Path|str, out_path: Path|str, header: dict):
    data = load_json(json_path)
    mapping = load_json(mapping_path)
    lookup = build_title_lookup(mapping)

    # group items by (MAJOR → SUBSECTION) with label/text/type/images
    grouped = group_items_detailed(data, lookup)

    # doc + page template (flowing pages, no 6-page limit)
    doc = BaseDocTemplate(str(out_path), pagesize=letter)
    frame = Frame(0.75*inch, 0.75*inch, doc.pagesize[0]-1.5*inch, doc.pagesize[1]-1.5*inch, showBoundary=0)
    template = PageTemplate(id='content', frames=[frame], onPage=lambda c,d: _on_page(c,d,header))
    doc.addPageTemplates([template])

    styles = _styles()

    story: List = []

    # Canonical TREC order (major → subsection), only include subsections with items
    for major in ORDERED_MAJOR:
        subsections = MAJOR_TO_SUBSECTION_ORDER[major]
        has_any = any(grouped.get(major, {}).get(sub, []) for sub in subsections)
        if not has_any:
            continue

        story.append(Paragraph(major, styles[0]))
        story.append(Spacer(1, 4))

        for sub in subsections:
            items = grouped.get(major, {}).get(sub, [])
            if not items:
                continue
            story.extend(_flow_for_subsection(sub, items, styles))
            story.append(Spacer(1, 6))

    doc.build(story)
