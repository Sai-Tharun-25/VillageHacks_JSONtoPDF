#!/usr/bin/env python3
"""
Binsr Inspect Challenge — Creative Bonus Report Builder
------------------------------------------------------

Generates a modern, client-ready PDF ("bonus_pdf.pdf") directly from the
provided inspection.json. It does NOT append or rely on any template PDF.

Features
- Executive Summary w/ bar chart of findings by category
- Clickable Table of Contents (auto-generated)
- Color-coded severity badges
- Images embedded inline next to each finding (downloaded from JSON URLs)
- Video links collected in a final "Videos" section (click to open in browser)
- Verbatim text rendering (no paraphrase); safe wrapping; page-stable layout
- Graceful fallbacks for missing/broken media

Usage:
    pip install reportlab matplotlib requests
    python bonus_pdf.py --json inspection.json --out bonus_pdf.pdf --media-cache .media_cache

Inputs:
- inspection.json (same structure you provided)  [required]

Outputs:
- bonus_pdf.pdf

Notes:
- We never modify the JSON text. The script renders comment text exactly as provided.
- For checklist items, selected options are shown as "Selected: …".
- If 'headerImageUrl' exists in inspection.json it is used as a cover image.
"""

from __future__ import annotations

import os
import io
import re
import sys
import json
import math
import argparse
import mimetypes
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
    Table, TableStyle, PageBreak, Flowable, KeepTogether
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

# ---------- CLI ----------
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="inspection.json", help="Path to inspection.json")
    ap.add_argument("--out", default="bonus_pdf.pdf", help="Output PDF path (required name for challenge)")
    ap.add_argument("--media-cache", default=".media_cache", help="Directory to cache downloaded media")
    return ap.parse_args()

# ---------- Utilities ----------
HTTP_TIMEOUT = (10, 30)  # connect, read

def ensure_dir(p: str) -> str:
    os.makedirs(p, exist_ok=True)
    return p

def sanitize_filename(url: str) -> str:
    name = os.path.basename(urlparse(url).path) or "media"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)

def download_to_cache(url: str, cache_dir: str) -> tuple[str | None, str | None]:
    """
    Download URL into cache_dir. Return (local_path, mime) or (None, None) on failure.
    """
    try:
        ensure_dir(cache_dir)
        local = os.path.join(cache_dir, sanitize_filename(url))
        if os.path.exists(local) and os.path.getsize(local) > 0:
            mime, _ = mimetypes.guess_type(local)
            return local, mime
        headers = {"User-Agent": "binsr-bonus-report/1.0"}
        with requests.get(url, stream=True, timeout=HTTP_TIMEOUT, headers=headers) as r:
            r.raise_for_status()
            mime = r.headers.get("Content-Type")
            with open(local, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
        if not mime:
            mime, _ = mimetypes.guess_type(local)
        return local, mime
    except Exception:
        return None, None

def is_image_mime(m: str | None) -> bool:
    return bool(m and m.lower().startswith("image/"))

def ms_to_date(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        # keep it simple (UTC); consumers typically only care about date
        dt = datetime.utcfromtimestamp(ms/1000.0)
        return dt.strftime("%B %d, %Y")
    except Exception:
        return ""

def to_html(text: str) -> str:
    """Safe HTML: preserves newlines, avoids overflow, keeps wrapping."""
    if text is None:
        return ""
    s = text.replace("\t", "    ")
    s = xml_escape(s)
    s = s.replace("\n", "<br/>")
    return s

def first_nonempty(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "Data not found in test data"

def section_sort_key(sec) -> tuple:
    # prefer numeric sectionNumber when present
    sn = sec.get("sectionNumber")
    if sn and str(sn).isdigit():
        return (int(sn), sec.get("order", 0))
    return (sec.get("order", 0), 0)

# ---------- Severity & badges ----------
def normalize(x: str | None) -> str:
    return (x or "").strip().lower()

def severity_bucket(comment: dict) -> str:
    t = normalize(comment.get("type"))
    tag = normalize(comment.get("tag"))
    # buckets cover the data seen in the JSON
    if "safety" in tag or tag == "safety-hazard" or t == "warning":
        return "Safety Hazard"
    if t in ("defect", "deficiency"):
        return "Defect"
    if t == "warning":
        return "Warning"
    if t == "recommendation":
        return "Recommendation"
    if t in ("limit", "limitation"):
        return "Limitation"
    if t == "info":
        return "Info"
    return "Other"

def badge_color(bucket: str) -> colors.Color:
    cm = {
        "Safety Hazard": colors.Color(0.84, 0.19, 0.19),  # red
        "Defect":        colors.Color(0.96, 0.49, 0.00),  # orange
        "Warning":       colors.Color(1.00, 0.73, 0.20),  # amber-ish
        "Recommendation":colors.Color(0.16, 0.50, 0.73),  # blue
        "Limitation":    colors.Color(0.52, 0.39, 0.74),  # purple
        "Info":          colors.Color(0.49, 0.49, 0.49),  # gray
        "Other":         colors.Color(0.23, 0.60, 0.32),  # green
    }
    return cm.get(bucket, colors.Color(0.23, 0.60, 0.32))

def badge(text: str, bg: colors.Color) -> Table:
    p = Paragraph(f'<para align="center"><b>{xml_escape(text)}</b></para>',
                  ParagraphStyle(name="chip", fontSize=8, textColor=colors.white, leading=10))
    t = Table([[p]], colWidths=[1.8*inch], rowHeights=[0.32*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), bg),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 2),
        ("RIGHTPADDING", (0,0), (-1,-1), 2),
    ]))
    return t

# ---------- Flowables ----------
class LinkedImage(Flowable):
    def __init__(self, img_path: str, width: float, height: float, href: str | None = None):
        super().__init__()
        self.path, self._w, self._h, self.href = img_path, width, height, href

    def wrap(self, availW, availH): return self._w, self._h

    def draw(self):
        canv = self.canv
        ir = ImageReader(self.path)
        canv.drawImage(ir, 0, 0, width=self._w, height=self._h,
                       preserveAspectRatio=True, mask="auto")
        if self.href:
            canv.linkURL(self.href, (0, 0, self._w, self._h), relative=1)

class LinkedVideoThumb(Flowable):
    def __init__(self, href: str, thumb_path: str | None = None, width: float = 1.6*inch, height: float = 1.2*inch):
        super().__init__()
        self.href, self.thumb, self._w, self._h = href, thumb_path, width, height

    def wrap(self, availW, availH): return self._w, self._h

    def draw(self):
        canv = self.canv
        if self.thumb:
            ir = ImageReader(self.thumb)
            canv.drawImage(ir, 0, 0, width=self._w, height=self._h, preserveAspectRatio=True, mask="auto")
        else:
            canv.saveState()
            canv.setStrokeColor(colors.HexColor("#9E9E9E"))
            canv.rect(0, 0, self._w, self._h)
            canv.restoreState()
        # Play icon
        canv.saveState()
        canv.setLineWidth(2)
        tri_w = 0.28*self._w
        tri_h = 0.36*self._h
        x0 = self._w/2 - tri_w/4
        pts = [(x0, self._h/2 - tri_h/2), (x0, self._h/2 + tri_h/2), (x0 + tri_w, self._h/2)]
        canv.lines([(pts[0][0], pts[0][1], pts[1][0], pts[1][1]),
                    (pts[1][0], pts[1][1], pts[2][0], pts[2][1]),
                    (pts[2][0], pts[2][1], pts[0][0], pts[0][1])])
        canv.restoreState()
        if self.href:
            canv.linkURL(self.href, (0,0,self._w,self._h), relative=1)

def scale_to_fit(iw, ih, max_w, max_h):
    if iw <= 0 or ih <= 0:
        return max_w, max_h
    s = min(max_w/iw, max_h/ih, 1.0)
    return iw*s, ih*s

def indent(flowable: Flowable, tabs: int, usable_w: float) -> Table:
    """Indent a flowable without risking frame overflow."""
    left = max(tabs * 0.30 * inch, 0)
    content_w = max(usable_w - left - 0.25, 1)
    t = Table([["", flowable]], colWidths=[left, content_w], hAlign="LEFT",
              style=TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"),
                                ("LEFTPADDING", (0,0), (-1,-1), 0),
                                ("RIGHTPADDING",(0,0), (-1,-1), 0),
                                ("TOPPADDING",  (0,0), (-1,-1), 0),
                                ("BOTTOMPADDING",(0,0), (-1,-1), 0)]))
    return t

# ---------- Numbered Canvas (for 'Page X of Y') ----------
# class NumberedCanvas(canvas.Canvas):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self._saved_page_states = []

#     def showPage(self):
#         # Save the current page state, but DO NOT call super().showPage() here
#         self._saved_page_states.append(dict(self.__dict__))
#         self._startPage()

#     def save(self):
#         num_pages = len(self._saved_page_states)
#         for state in self._saved_page_states:
#             self.__dict__.update(state)
#             self.draw_page_number(num_pages)
#             # draw footer/header with num_pages here if you do page numbering
#             super().showPage()
#         super().save()

#     def draw_page_number(self, page_count):
#         self.setFont("Helvetica", 8.5)
#         w, h = self._pagesize
#         text = f"Page {self._pageNumber} of {page_count}"
#         self.drawRightString(w - 36, 24, text)

from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []
        # NEW: keep anchors so we can re-apply ToC destinations on the final pass
        self._toc_anchors = []  # list of tuples: (page_index, key, text, level)

    # called from your TOCDoc.afterFlowable (see small patch below)
    def remember_toc_anchor(self, page, key, text=None, level=None):
        self._toc_anchors.append((page, key, text, level))

    def showPage(self):
        # Save the current page state but DO NOT output a page here.
        # This is the only change needed to stop duplicate pages.
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        n = len(self._saved_page_states)
        for i, state in enumerate(self._saved_page_states, start=1):
            self.__dict__.update(state)

            # Re-apply bookmarks/outlines for this physical page so ToC links work
            for (p, key, text, level) in (a for a in self._toc_anchors if a[0] == i):
                self.bookmarkPage(key)
                try:
                    # outline entry is optional; bookmark is enough for clickable ToC
                    self.addOutlineEntry(text or key, key, level=(level or 0), closed=False)
                except Exception:
                    pass

            # Keep your original footer API/signature
            self.draw_page_number(n)

            # Now emit the page once
            canvas.Canvas.showPage(self)

        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        self.setFont("Helvetica", 8.5)
        w, h = self._pagesize
        text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(w - 36, 24, text)

# ---------- Document (ToC hooks) ----------
class TOCDoc(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph):
            style = flowable.style.name
            if style in ("H1", "H2", "H3"):
                key = f"bk_{id(flowable)}"
                flowable._bookmarkName = key        # anchor at the heading
                level = {"H1": 0, "H2": 1, "H3": 2}[style]
                text = flowable.getPlainText()
                # Register ToC entry
                self.notify("TOCEntry", (level, text, self.page, key))
                # Make ToC entries clickable (PDF outline + bookmark)
                # try:
                #     self.canv.bookmarkPage(key)
                #     self.canv.addOutlineEntry(text, key, level=level, closed=False)
                # except Exception:
                #     pass
                if hasattr(self.canv, "remember_toc_anchor"):
                    self.canv.remember_toc_anchor(self.page, key, text, level)


# ---------- Build chart ----------
def make_counts_chart(counts: dict[str, int], path: str):
    import matplotlib.pyplot as plt
    labels = list(counts.keys())
    values = [counts[k] for k in labels]
    fig = plt.figure(figsize=(6.0, 3.0))
    plt.bar(labels, values)          # color left default per instructions
    plt.title("Findings by Category")
    plt.xticks(rotation=20, ha="right")
    plt.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)

# ---------- Header/Footer ----------
def fit_font_size(text: str, face: str, max_size: int, max_width: float, min_size: int = 7) -> int:
    for sz in range(max_size, min_size-1, -1):
        if pdfmetrics.stringWidth(text, face, sz) <= max_width:
            return sz
    return min_size

def draw_header_footer(canvas, doc, header_text: str):
    canvas.saveState()
    page_w, page_h = doc.pagesize
    lm, rm, tm, bm = doc.leftMargin, doc.rightMargin, doc.topMargin, doc.bottomMargin
    usable_w = page_w - lm - rm

    # Header (single line)
    hsz = fit_font_size(header_text, "Helvetica", 11, usable_w)
    canvas.setFont("Helvetica", hsz)
    canvas.drawString(lm, page_h - (tm * 0.65), header_text)

    # Footer is added by NumberedCanvas (page numbers)
    canvas.restoreState()

# ---------- Story builder ----------
def build_story(data: dict, media_cache: str) -> list:
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1", fontSize=18, leading=22, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="H2", fontSize=14, leading=18, spaceBefore=10, spaceAfter=6))
    styles.add(ParagraphStyle(name="H3", fontSize=12, leading=15, spaceBefore=6, spaceAfter=4))
    styles.add(ParagraphStyle(name="Body", fontSize=10.3, leading=13.2))
    styles.add(ParagraphStyle(name="Small", fontSize=8.2, leading=10.1, textColor=colors.grey))
    styles.add(ParagraphStyle(name="CenterSmall", parent=styles["Small"], alignment=TA_CENTER))

    insp = data.get("inspection", {}) or {}
    addr = insp.get("address", {}) or {}
    client = insp.get("clientInfo", {}) or {}
    inspector = insp.get("inspector", {}) or {}
    booking = insp.get("bookingFormData", {}) or {}
    schedule = insp.get("schedule") or booking.get("schedule") or {}
    date_str = ms_to_date(schedule.get("date"))
    sf = (booking.get("propertyInfo", {}) or {}).get("squareFootage") or booking.get("squareFootage")
    header_image_url = insp.get("headerImageUrl")  # optional cover photo

    # Collect stats
    sections = sorted(insp.get("sections", []) or [], key=section_sort_key)
    severity_counts = Counter()
    flagged = []
    photo_total, video_total = 0, 0
    videos = []

    for sec in sections:
        for li in sec.get("lineItems", []) or []:
            for c in li.get("comments", []) or []:
                severity_counts[severity_bucket(c)] += 1
                photo_total += len(c.get("photos") or [])
                video_total += len(c.get("videos") or [])
                if c.get("isFlagged"):
                    flagged.append(c)
                for v in c.get("videos") or []:
                    videos.append({
                        "url": v.get("url"),
                        "thumb": v.get("thumbnail") or v.get("thumbnailUrl"),
                        "commentNumber": c.get("commentNumber"),
                        "label": c.get("label"),
                        "section": sec.get("name"),
                        "location": c.get("location"),
                    })

    # Begin story
    story = []

    # Cover
    story.append(Paragraph(f"<b>Property:</b> {first_nonempty(addr.get('fullAddress'), addr.get('street'))}", styles["Body"]))
    story.append(Paragraph(f"<b>Client:</b> {first_nonempty(client.get('name'))}  &nbsp;&nbsp; <b>Inspector:</b> {first_nonempty(inspector.get('name'))}", styles["Body"]))
    date_str = ms_to_date(schedule.get("date"))
    story.append(Paragraph(f"<b>Inspection Date:</b> {first_nonempty(date_str)}", styles["Body"]))
    sf = (booking.get("propertyInfo", {}) or {}).get("squareFootage") or booking.get("squareFootage")
    sf_str = f"{int(sf):,} sq ft" if isinstance(sf, (int, float)) and sf else "Data not found in test data"
    story.append(Paragraph(f"<b>Approx. Living Area:</b> {sf_str}", styles["Body"]))


    if header_image_url:
        loc, mime = download_to_cache(header_image_url, media_cache)
        if loc and is_image_mime(mime):
            try:
                iw, ih = ImageReader(loc).getSize()
                w, h = scale_to_fit(iw, ih, 6.2*inch, 2.6*inch)
                story.append(RLImage(loc, width=w, height=h))
                story.append(Spacer(1, 0.12*inch))
            except Exception:
                pass

    # Executive Summary
    story.append(Paragraph("Executive Summary", styles["H2"]))
    stats = [
        ["Total Findings (comments)", f"{sum(severity_counts.values())}"],
        ["Photos (from JSON)", f"{photo_total}"],
        ["Videos (linked at end)", f"{video_total}"],
        ["Flagged by Inspector", f"{len(flagged)}"],
    ]
    t = Table(stats, colWidths=[3.2*inch, 2.9*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#F1F4F8")),
        ("GRID",(0,0),(-1,-1), 0.25, colors.HexColor("#DCE1E7")),
        ("LEFTPADDING",(0,0),(-1,-1),6),
        ("RIGHTPADDING",(0,0),(-1,-1),6),
        ("TOPPADDING",(0,0),(-1,-1),4),
        ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]))
    story.append(t)

    # Chart
    if severity_counts:
        chart_png = os.path.join(media_cache, "_summary_chart.png")
        try:
            make_counts_chart(dict(severity_counts), chart_png)
            story.append(Spacer(1, 0.08*inch))
            story.append(RLImage(chart_png, width=5.9*inch, height=3.0*inch))
        except Exception:
            pass

    # Legend
    story.append(Spacer(1, 0.08*inch))
    story.append(Paragraph("Color Coding", styles["H3"]))
    legend_row = [
        badge("Safety Hazard", badge_color("Safety Hazard")),
        badge("Defect",        badge_color("Defect")),
        badge("Recommendation",badge_color("Recommendation")),
        badge("Limitation",    badge_color("Limitation")),
        badge("Info",          badge_color("Info")),
    ]
    lg = Table([legend_row], colWidths=[1.35*inch]*5)
    story.append(lg)

    story.append(PageBreak())

    # Table of Contents
    story.append(Paragraph("Table of Contents", styles["H1"]))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(name="TOC1", fontSize=11, leading=13, leftIndent=12, firstLineIndent=-6, spaceBefore=3),
        ParagraphStyle(name="TOC2", fontSize=9.6, leading=11, leftIndent=28, firstLineIndent=-6, spaceBefore=1.5),
        ParagraphStyle(name="TOC3", fontSize=9.2, leading=10.5, leftIndent=44, firstLineIndent=-6, spaceBefore=1),
    ]
    story.append(toc)
    story.append(PageBreak())

    # Findings by Section
    story.append(Paragraph("Findings by Section", styles["H1"]))

    page_w, _ = letter
    usable_w = page_w - (36 + 36)  # margins will be set on doc

    def add_comment_images(flow, photos: list[dict]):
        if not photos:
            return
        # Grid, 2-wide
        tiles = []
        row = []
        for i, ph in enumerate(photos, start=1):
            url = ph.get("url")
            cap = ph.get("caption") or ""
            if not url:
                continue
            loc, mime = download_to_cache(url, media_cache)
            if loc and is_image_mime(mime):
                try:
                    iw, ih = ImageReader(loc).getSize()
                    W, H = 2.8*inch, 2.05*inch
                    w, h = scale_to_fit(iw, ih, W, H)
                    img = LinkedImage(loc, w, h, href=url)
                    cell = Table([[img], [Paragraph(f'<font size="7" color="#666666">{xml_escape(cap)}</font>', styles["CenterSmall"])]],
                                 colWidths=[W], rowHeights=[H, 0.22*inch])
                    cell.setStyle(TableStyle([
                        ("ALIGN",(0,0),(-1,-1),"CENTER"),
                        ("VALIGN",(0,0),(-1,-1),"TOP"),
                        ("BOX",(0,0),(-1,-1),0.25, colors.HexColor("#E5E5E5")),
                        ("TOPPADDING",(0,0),(-1,-1),2),
                        ("BOTTOMPADDING",(0,0),(-1,-1),2),
                    ]))
                    row.append(cell)
                except Exception:
                    row.append(Paragraph(f'<font size="7">Image unavailable<br/>{xml_escape(url)}</font>', styles["CenterSmall"]))
            else:
                row.append(Paragraph(f'<font size="7">Image unavailable<br/>{xml_escape(url)}</font>', styles["CenterSmall"]))
            if len(row) == 2:
                tiles.append(row); row = []
        if row:
            row.append("")  # pad
            tiles.append(row)
        grid = Table(tiles, colWidths=[3.0*inch, 3.0*inch])
        grid.setStyle(TableStyle([
            ("LEFTPADDING",(0,0),(-1,-1),4),
            ("RIGHTPADDING",(0,0),(-1,-1),4),
            ("TOPPADDING",(0,0),(-1,-1),4),
            ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        flow.append(grid)
        flow.append(Spacer(1, 0.06*inch))

    # Iterate sections/line-items/comments
    for s_idx, sec in enumerate(sections, start=1):
        sec_title = f"{sec.get('sectionNumber') or s_idx}. {sec.get('name') or 'Section'}"
        story.append(Paragraph(sec_title, styles["H2"]))

        line_items = sorted(sec.get("lineItems", []) or [], key=lambda li: (li.get("order", 0), li.get("title") or li.get("name") or ""))

        for li_idx, li in enumerate(line_items, start=1):
            li_name = first_nonempty(li.get("title"), li.get("name"))
            status = li.get("inspectionStatus")
            status_html = f' <font size="8" color="#777777">[Status: {xml_escape(first_nonempty(status))}]</font>'

            story.append(Paragraph(f"<b>{xml_escape(li_name)}</b>{status_html}", styles["Body"]))

            comments = sorted(li.get("comments", []) or [], key=lambda c: c.get("order", 0))
            if not comments:
                story.append(Spacer(1, 0.05*inch))
                continue

            for c in comments:
                # Left column (badge + location + "Selected" for checklists)
                bucket = severity_bucket(c)
                col = badge_color(bucket)
                tag_text = bucket
                left = Table([
                    [badge(tag_text, col)],
                    [Paragraph(f'<font size="8" color="#666666">Location</font><br/><b>{xml_escape(first_nonempty(c.get("location")))}</b>', styles["Body"])],
                ], colWidths=[1.95*inch])

                left.setStyle(TableStyle([
                    ("VALIGN",(0,0),(-1,-1),"TOP"),
                    ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#FAFAFA")),
                    ("BOX",(0,0),(-1,-1), 0.25, colors.HexColor("#EAEAEA")),
                    ("LEFTPADDING",(0,0),(-1,-1),4),
                    ("RIGHTPADDING",(0,0),(-1,-1),4),
                    ("TOPPADDING",(0,0),(-1,-1),3),
                    ("BOTTOMPADDING",(0,0),(-1,-1),3),
                ]))

                title_line = f"<b>{xml_escape(c.get('commentNumber') or '')} • {xml_escape(c.get('label') or '')}</b>"
                body_line = first_nonempty(c.get("text"), c.get("content"), c.get("commentText"), c.get("value"))
                body_html = to_html(body_line)


                # include checklist selections if present
                selected = c.get("selectedOptions") or []
                selected_html = ""
                if selected:
                    selected_html = f'<br/><font size="8" color="#666666"><b>Selected:</b> {xml_escape(", ".join(str(x) for x in selected if x))}</font>'

                right_stack = [
                    Paragraph(title_line, styles["Body"]),
                    Spacer(1, 2),
                    Paragraph(body_html + selected_html, styles["Body"]),
                ]
                if c.get("recommendation"):
                    right_stack += [Spacer(1,2),
                                    Paragraph(f'<font size="8" color="#666666">Recommendation</font><br/><b>{xml_escape(str(c.get("recommendation")))}</b>', styles["Body"])]

                right_tbl = Table([[x] for x in right_stack], colWidths=[4.95*inch])
                right_tbl.setStyle(TableStyle([
                    ("VALIGN",(0,0),(-1,-1),"TOP"),
                    ("LEFTPADDING",(0,0),(-1,-1),0),
                    ("RIGHTPADDING",(0,0),(-1,-1),0),
                    ("TOPPADDING",(0,0),(-1,-1),0),
                    ("BOTTOMPADDING",(0,0),(-1,-1),0),
                ]))

                card = Table([[left, right_tbl]], colWidths=[2.10*inch, 4.95*inch])
                card.setStyle(TableStyle([
                    ("VALIGN",(0,0),(-1,-1),"TOP"),
                    ("BOX",(0,0),(-1,-1),0.5, colors.HexColor("#E0E0E0")),
                    ("LEFTPADDING",(0,0),(-1,-1),6),
                    ("RIGHTPADDING",(0,0),(-1,-1),6),
                    ("TOPPADDING",(0,0),(-1,-1),6),
                    ("BOTTOMPADDING",(0,0),(-1,-1),6),
                ]))
                story.append(card)
                story.append(Spacer(1, 0.06*inch))

                # Images (inline gallery)
                add_comment_images(story, c.get("photos") or [])

                # If photos exist but could not be downloaded (e.g., offline), show the URLs as links
                if (c.get("photos") or []) and all(not (download_to_cache(p.get("url"), media_cache)[0]) for p in c["photos"] if p.get("url")):
                    links = [f'<link href="{xml_escape(p.get("url"))}">{xml_escape(p.get("url"))}</link>' for p in c["photos"] if p.get("url")]
                    if links:
                        story.append(Paragraph("Image links (online): " + " • ".join(links), styles["Small"]))
                        story.append(Spacer(1, 0.04*inch))

            story.append(Spacer(1, 0.08*inch))

    # Videos (at the end)
    story.append(PageBreak())
    story.append(Paragraph("Videos (Download Links)", styles["H1"]))
    if videos:
        rows = [["Comment", "Section", "Location", "URL", "Preview"]]
        for v in videos:
            # optional thumbnail
            thumb_path = None
            if v.get("thumb"):
                p, m = download_to_cache(v["thumb"], media_cache)
                if p and is_image_mime(m):
                    thumb_path = p
            thumb_flow = LinkedVideoThumb(href=v.get("url") or v.get("thumb") or "", thumb_path=thumb_path, width=1.6*inch, height=1.2*inch)
            rows.append([
                xml_escape(v.get("commentNumber") or ""),
                xml_escape(v.get("section") or ""),
                xml_escape(v.get("location") or "—"),
                f'<link href="{xml_escape(v.get("url") or "")}">{xml_escape(v.get("url") or "")}</link>',
                thumb_flow
            ])
        tbl = Table(rows, colWidths=[0.95*inch, 2.2*inch, 1.5*inch, 2.0*inch, 1.7*inch])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#F4F6F8")),
            ("GRID",(0,0),(-1,-1), 0.25, colors.HexColor("#DDE2E7")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LEFTPADDING",(0,0),(-1,-1),4),
            ("RIGHTPADDING",(0,0),(-1,-1),4),
            ("TOPPADDING",(0,0),(-1,-1),3),
            ("BOTTOMPADDING",(0,0),(-1,-1),3),
        ]))
        story.append(tbl)
    else:
        story.append(Paragraph("No videos provided in this inspection.", styles["Body"]))

    return story

# ---------- Main ----------
def main():
    args = parse_args()
    with open(args.json, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Build header text
    insp = data.get("inspection", {}) or {}
    addr = insp.get("address", {}) or {}
    full_addr = addr.get("fullAddress") or addr.get("street") or ""
    header = f"Report Identification: {full_addr}".strip()

    doc = TOCDoc(
        args.out,
        pagesize=letter,
        leftMargin=36, rightMargin=36, topMargin=42, bottomMargin=42,
        title="Home Inspection Report (Creative Bonus)"
    )
    story = build_story(data, args.media_cache)

    # Build with header/footer + numbered canvas
    onpage = lambda canv, d: draw_header_footer(canv, d, header)
    # doc.build(story, onFirstPage=onpage, onLaterPages=onpage, canvasmaker=NumberedCanvas)
    doc.multiBuild(story, onFirstPage=onpage, onLaterPages=onpage, canvasmaker=NumberedCanvas)

    print(f"Created: {args.out}")

if __name__ == "__main__":
    main()
