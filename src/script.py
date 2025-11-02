import os
import re
import json
import mimetypes
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from trec_prefix import prepend_trec_pages

import requests
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import Flowable
from reportlab.platypus import Table, TableStyle
from reportlab.pdfgen import canvas as rl_canvas
from functools import lru_cache
from PIL import Image

# ====== CONFIG (your paths) ======
JSON_PATH = "inspection.json"
TREC_TEMPLATE_PDF = "src/TREC_Template_Blank.pdf"
BODY_PDF = "src/inspection_body.pdf"            # temp body file
OUT_PDF  = "output_pdf.pdf"          # final report
MEDIA_DIR = "data/_media_cache"

PAGE_SIZE = LETTER
MARGINS   = dict(left=1*inch, right=1*inch, top=1*inch, bottom=1*inch)
MARGINS["top"] = 1.5 * inch

# One font everywhere (header/footer use same face; size may shrink to fit)
FONT_NAME = "Helvetica"
FONT_SIZE = 11

# Indentation: physical tab to avoid rounding drift
TAB_PT = 0.30 * inch

# Uniform media sizing
IMAGE_MAX_WIDTH   = 3.5 * inch
IMAGE_MAX_HEIGHT  = 2.5 * inch
VIDEO_MAX_WIDTH   = 3.5 * inch
VIDEO_MAX_HEIGHT  = 2.5 * inch

HTTP_TIMEOUT = (10, 30)  # connect, read
EPS = 0.5  # sub-point epsilon to prevent rounding overflow

# Footer text (convert unusual bullets to standard bullets for PDF reliability)
FOOTER_TEXT_RAW = "REI 7-6 (8/9/21)                       Promulgated by the Texas Real Estate Commission (512) 936-3000   www.trec.texas.gov"
FOOTER_TEXT = FOOTER_TEXT_RAW.replace("", "•")

# Legend styling
LEGEND_TEXT_SIZE   = 9
LEGEND_BOX_HEIGHT  = 16     # height of the bordered bar
LEGEND_TOP_OFFSET  = 4      # how far above the content frame the legend sits
LEGEND_INNER_PAD   = 6      # left padding inside the bordered bar


# ================== Utilities ==================
def to_roman(n: int) -> str:
    vals = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),
            (100,"C"),(90,"XC"),(50,"L"),(40,"XL"),
            (10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
    out = []
    for v,s in vals:
        while n >= v:
            out.append(s); n -= v
    return "".join(out)

def alpha_label(idx: int) -> str:
    s, i = "", idx
    while True:
        s = chr(ord("A") + (i % 26)) + s
        i = i // 26 - 1
        if i < 0:
            return s

def first_nonempty(*cands):
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c
    return None

def sanitize_filename(url: str) -> str:
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "media"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "media"

def ensure_dir(p): os.makedirs(p, exist_ok=True); return p

def download_url(url: str, dest_dir: str):
    ensure_dir(dest_dir)
    local = os.path.join(dest_dir, sanitize_filename(url))
    if os.path.exists(local):
        mime, _ = mimetypes.guess_type(local)
        return local, mime
    headers = {"User-Agent": "pdf-media-fetch/1.0"}
    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT, headers=headers) as r:
        r.raise_for_status()
        mime = r.headers.get("Content-Type")
        with open(local, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk: f.write(chunk)
    if not mime:
        mime, _ = mimetypes.guess_type(local)
    return local, mime

def is_image_mime(m: str | None) -> bool:
    return bool(m and m.lower().startswith("image/"))

def scale_to_fit(iw, ih, max_w, max_h):
    if iw <= 0 or ih <= 0:
        return max_w, max_h
    s = min(max_w/iw, max_h/ih, 1.0)
    return iw*s, ih*s

def to_html_preserving_ws(text: str) -> str:
    """Preserve tabs/multiple spaces but still allow wrapping inside the frame."""
    s = (text or "").replace("\t", "    ")
    s = xml_escape(s)
    s = re.sub(r" {2,}", lambda m: " " + "&nbsp;"*(len(m.group(0))-1), s)
    s = s.replace("\n", "<br/>")
    return s

def draw_status_legend(canvas, left_margin, top_margin, page_w, page_h):
    """
    Draws the two-line legend (labels + bordered bar) entirely inside
    the top margin area, just above the content frame.
    """
    usable_w = page_w - 2 * left_margin
    # Bottom of top margin (i.e., top edge of the content frame)
    y_frame_top = page_h - top_margin
    # Place legend just above the frame by LEGEND_TOP_OFFSET points
    y0 = y_frame_top + LEGEND_TOP_OFFSET

    # 1) Top line: the long labels, spread across the width
    labels = [("I", "Inspected"), ("NI", "Not Inspected"),
              ("NP", "Not Present"), ("D", "Deficient")]
    col_w = usable_w / 4.0
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", LEGEND_TEXT_SIZE)
    for i, (abbr, full) in enumerate(labels):
        canvas.drawString(left_margin + i * col_w, y0 + LEGEND_BOX_HEIGHT + 6, f"{abbr}={full}")
    canvas.restoreState()

    # 2) Bordered bar with "I  NI  NP  D"
    canvas.saveState()
    canvas.setLineWidth(1.2)
    canvas.rect(left_margin, y0, usable_w, LEGEND_BOX_HEIGHT)
    canvas.setFont("Helvetica-Bold", LEGEND_TEXT_SIZE + 1)
    text = "  ".join(["      ", "I", "NI", "NP", "D"])
    ty = y0 + (LEGEND_BOX_HEIGHT - (LEGEND_TEXT_SIZE + 1)) / 2.0  # vertical center
    canvas.drawString(left_margin + LEGEND_INNER_PAD, ty, text)
    canvas.restoreState()

from functools import lru_cache

@lru_cache(maxsize=256)
def get_image_size_cached(path):
    """Cache image size to avoid reopening repeatedly."""
    with Image.open(path) as im:
        return im.size
    
# ---- Checkboxes config ----
CHECK_SIZE_PT = 10      # square size
CHECK_GAP_PT  = 6       # gap between squares
CHECK_LABELS  = ["I", "NI", "NP", "D"]
CHECKS_WIDTH  = 4*CHECK_SIZE_PT + 3*CHECK_GAP_PT  # total width of the 4 boxes

def normalize_status(status):
    """Return one of 'I','NI','NP','D' (or None) from common inputs."""
    if isinstance(status, str):
        s = status.strip().lower()
        mapping = {
            "i": "I", "inspected": "I",
            "ni": "NI", "not inspected": "NI",
            "np": "NP", "not present": "NP",
            "d": "D", "deficient": "D", "defect": "D",
        }
        if s in mapping: return mapping[s]
        # try prefix match
        for k, v in mapping.items():
            if s.startswith(k): return v
    if isinstance(status, dict):
        # e.g. {"I": true, "NI": false, ...}
        for k in CHECK_LABELS:
            if status.get(k) or status.get(k.lower()): return k
    if isinstance(status, (list, tuple, set)):
        for x in status:
            v = normalize_status(x)
            if v: return v
    return None

# ----------- Media Flowables -----------
class LinkedImage(Flowable):
    def __init__(self, img_path, width, height, href=None):
        super().__init__(); self.img_path = img_path
        self._w = width; self._h = height; self.href = href
    def wrap(self, availW, availH): return self._w, self._h
    def draw(self):
        ir = ImageReader(self.img_path)
        self.canv.drawImage(ir, 0, 0, width=self._w, height=self._h,
                            preserveAspectRatio=True, mask="auto")
        if self.href:
            self.canv.linkURL(self.href, (0, 0, self._w, self._h), relative=1)

class LinkedVideoThumb(Flowable):
    def __init__(self, href, thumb_path=None, width=1.2*inch, height=0.9*inch):
        super().__init__(); self.href = href; self.thumb_path = thumb_path
        self._w = width; self._h = height
    def wrap(self, availW, availH): return self._w, self._h
    def _draw_play(self, cx, cy, w, h):
        canv = self.canv; canv.saveState(); canv.setLineWidth(2)
        tri_w = 0.28*w; tri_h = 0.36*h; x0 = cx - tri_w/3
        pts = [(x0, cy-tri_h/2), (x0, cy+tri_h/2), (x0+tri_w, cy)]
        canv.lines([(pts[0][0], pts[0][1], pts[1][0], pts[1][1]),
                    (pts[1][0], pts[1][1], pts[2][0], pts[2][1]),
                    (pts[2][0], pts[2][1], pts[0][0], pts[0][1])])
        canv.restoreState()
    def draw(self):
        canv = self.canv
        if self.thumb_path:
            ir = ImageReader(self.thumb_path)
            canv.drawImage(ir, 0, 0, width=self._w, height=self._h,
                           preserveAspectRatio=True, mask="auto")
        else:
            canv.saveState(); canv.setLineWidth(1.2); canv.rect(0,0,self._w,self._h); canv.restoreState()
        self._draw_play(self._w/2, self._h/2, self._w, self._h)
        if self.href: canv.linkURL(self.href, (0,0,self._w,self._h), relative=1)

def indent_cell(flowable, tabs, usable_w):
    """Indent a flowable with a 2-col table that NEVER exceeds the text frame."""
    left = max(tabs * TAB_PT, 0)
    content_w = max(usable_w - left - EPS, 1)
    tbl = Table(
        [["", flowable]],
        colWidths=[left, content_w],
        hAlign="LEFT",
        style=TableStyle([
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING", (0,0), (-1,-1), 0),
            ("RIGHTPADDING",(0,0), (-1,-1), 0),
            ("TOPPADDING",  (0,0), (-1,-1), 0),
            ("BOTTOMPADDING",(0,0),(-1,-1), 0),
        ])
    )
    return tbl

class StatusChecks(Flowable):
    """Four check boxes (I, NI, NP, D). If cross_all=True, X all four boxes."""
    def __init__(self, selected=None, *, cross_all=False, size=CHECK_SIZE_PT, gap=CHECK_GAP_PT):
        super().__init__()
        self.selected = selected          # one of CHECK_LABELS (or None)
        self.cross_all = cross_all        # when True, cross every box
        self.size = size
        self.gap = gap
        self._w = 4*size + 3*gap
        self._h = size

    def wrap(self, availW, availH):
        return self._w, self._h

    def draw(self):
        c = self.canv
        x = 0
        for label in CHECK_LABELS:
            c.rect(x, 0, self.size, self.size)
            if self.cross_all or (self.selected == label):
                c.setLineWidth(1.2)
                c.line(x+1.5, 1.5, x+self.size-1.5, self.size-1.5)
                c.line(x+self.size-1.5, 1.5, x+1.5, self.size-1.5)
            x += self.size + self.gap

class NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, page_offset=0, left_margin=72, right_margin=72,
                 bottom_margin=72, font_name="Helvetica", font_size=9, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self.page_offset   = page_offset
        self.left_margin   = left_margin
        self.right_margin  = right_margin
        self.bottom_margin = bottom_margin
        self.font_name     = font_name
        self.font_size     = font_size

    def showPage(self):
        # Save the current page state, but DO NOT emit the page here.
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()  # prepare for the next page without writing

    def save(self):
        total_body  = len(self._saved_page_states)
        total_final = total_body + self.page_offset
        GAP_ABOVE_FOOTER = 3  # points

        for state in self._saved_page_states:
            self.__dict__.update(state)

            body_n  = self._pageNumber
            final_n = self.page_offset + body_n
            label   = f"Page {final_n} of {total_final}"

            w, h = self._pagesize
            tw   = pdfmetrics.stringWidth(label, self.font_name, self.font_size)
            x    = (w - tw) / 2.0
            y    = (self.bottom_margin / 2.0) + self.font_size + GAP_ABOVE_FOOTER  # centered above footer

            self.setFont(self.font_name, self.font_size)
            self.drawString(x, y, label)

            rl_canvas.Canvas.showPage(self)  # emit the numbered page once

        rl_canvas.Canvas.save(self)

# ----------- Header / Footer helpers -----------
def fit_font_size(text: str, face: str, max_size: int, max_width: float, min_size: int = 7) -> int:
    """Find a font size (<= max_size) that fits within max_width; down to min_size."""
    for sz in range(max_size, min_size - 1, -1):
        if pdfmetrics.stringWidth(text, face, sz) <= max_width:
            return sz
    return min_size

def elide_to_width(text: str, face: str, size: int, max_width: float) -> str:
    """Ellipsize the text from the right until it fits within max_width."""
    if pdfmetrics.stringWidth(text, face, size) <= max_width:
        return text
    ell = "..."
    ell_w = pdfmetrics.stringWidth(ell, face, size)
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid] + ell
        w = pdfmetrics.stringWidth(candidate, face, size)
        if w <= max_width:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best or ell

def make_onpage(header_text: str, footer_text: str):
    def onpage(canvas, doc):
        canvas.saveState()
        page_w, page_h = doc.pagesize
        lm = doc.leftMargin
        rm = doc.rightMargin
        tm = doc.topMargin
        bm = doc.bottomMargin
        usable_w = page_w - lm - rm

        # ----- Header (already in your code) -----
        hdr_size = fit_font_size(header_text, FONT_NAME, FONT_SIZE, usable_w, min_size=7)
        hdr_draw = elide_to_width(header_text, FONT_NAME, hdr_size, usable_w)
        canvas.setFont(FONT_NAME, hdr_size)
        canvas.drawString(lm, page_h - (tm * 0.65), hdr_draw)

        # ----- Legend in the top margin -----
        draw_status_legend(canvas, lm, tm, page_w, page_h)

        # ----- Footer (already in your code) -----
        ftr_size = fit_font_size(FOOTER_TEXT, FONT_NAME, 9, usable_w, min_size=7)
        ftr_draw = elide_to_width(FOOTER_TEXT, FONT_NAME, ftr_size, usable_w)
        canvas.setFont(FONT_NAME, ftr_size)
        canvas.drawString(lm, bm * 0.5, ftr_draw)

        canvas.restoreState()
    return onpage


# ------------- Main story builder -------------
def build_story(data: dict):
    styles = getSampleStyleSheet()
    base = ParagraphStyle("Base", parent=styles["Normal"],
                          fontName=FONT_NAME, fontSize=FONT_SIZE,
                          leading=FONT_SIZE+2, leftIndent=0, rightIndent=0)
    section_style = ParagraphStyle("Section", parent=base, fontName="Helvetica-Bold", alignment=TA_CENTER, spaceBefore=6, spaceAfter=4, fontSize=12)
    line_style    = ParagraphStyle("Line",    parent=base, fontName="Helvetica-Bold", fontSize=10)
    comment_style = ParagraphStyle("Comment", parent=base, fontSize=10)
    comment_label_style = ParagraphStyle(
    "CommentLabel",
    parent=base,                # same base you use elsewhere
    fontName="Helvetica-Oblique",
    fontSize=10,
    leftIndent=2*TAB_PT,        # align with the comments
    spaceBefore=4,
    spaceAfter=2
)
    missing_style = ParagraphStyle(
    "MissingData",
    parent=base,
    fontSize=10,
    leftIndent=2*TAB_PT,   # align with comments
)

    page_w, _ = PAGE_SIZE
    usable_w = page_w - (MARGINS["left"] + MARGINS["right"])  # hard frame width

    story = []
    sections = (data.get("inspection", {}) or {}).get("sections", []) or []

    def section_order(s, i):
        n = s.get("sectionNumber") or s.get("order")
        try: return int(n)
        except: return i

    for s_idx, section in enumerate(sorted(sections, key=lambda s: section_order(s, sections.index(s)))):
        sec_title = first_nonempty(section.get("name"), f"Section {s_idx+1}")
        story.append(Paragraph(f"{to_roman(s_idx+1)}. {sec_title}", section_style))

        line_items = sorted((section.get("lineItems") or []),
                            key=lambda li: (li.get("order", 0), li.get("name","")))
        for li_idx, item in enumerate(line_items):
            li_name = first_nonempty(item.get("title"), item.get("name"), "Line Item")

            # 1) Get comments FIRST so we know if there’s data
            comments = sorted((item.get("comments") or []), key=lambda c: c.get("order", 0))
            has_data = len(comments) > 0

            # 2) status & checks  — cross all if no data
            status = normalize_status(item.get("inspectionStatus"))
            checks = StatusChecks(selected=status, cross_all=not has_data)

            # 3) line-item title (indent via first blank column; small gap inside name cell)
            li_para = Paragraph(
                f"{alpha_label(li_idx)}. {li_name}",
                ParagraphStyle("li_row", parent=line_style, leftIndent=12)
            )

            # [ indent(=1 tab) | checkboxes | title ]
            row = Table(
                [["", checks, li_para]],
                colWidths=[
                    TAB_PT,
                    CHECKS_WIDTH,
                    usable_w - TAB_PT - CHECKS_WIDTH - 0.5
                ],
                hAlign="LEFT",
                style=TableStyle([
                    ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
                    ("LEFTPADDING", (0,0), (-1,-1), 0),
                    ("RIGHTPADDING",(0,0), (-1,-1), 0),
                    ("TOPPADDING",  (0,0), (-1,-1), 0),
                    ("BOTTOMPADDING",(0,0), (-1,-1), 2),
                ])
            )
            story.append(row)

            # 4) Comments label
            story.append(Paragraph("<i>Comments:</i>", comment_label_style))

            # 5) Comments or fallback
            if has_data:
                for c in comments:
                    comment_text = first_nonempty(c.get("text"), c.get("content"), c.get("commentText"), c.get("value")) or ""
                    html = to_html_preserving_ws(comment_text)
                    story.append(Paragraph(html, ParagraphStyle("cm", parent=comment_style, leftIndent=2*TAB_PT)))
                    story.append(Spacer(0, 4))

                    # Photos
                    for p in (c.get("photos") or []):
                        url = p.get("url")
                        if not url: continue
                        try:
                            path, mime = download_url(url, MEDIA_DIR)
                            if not is_image_mime(mime): continue
                            iw, ih = get_image_size_cached(path)
                            max_w = min(IMAGE_MAX_WIDTH, usable_w - 2*TAB_PT - EPS)
                            w, h = scale_to_fit(iw, ih, max_w, IMAGE_MAX_HEIGHT)
                            story.append(indent_cell(LinkedImage(path, w, h, href=url), tabs=2, usable_w=usable_w))
                            story.append(Spacer(0, 6))
                        except Exception:
                            pass

                    # Videos -> clickable play badge only; no downloads or thumbnails
                    for v in (c.get("videos") or []):
                        vurl = v.get("url")
                        if not vurl:
                            continue
                        # Draw a simple play badge and make it clickable
                        badge = LinkedVideoThumb(
                            href=vurl,          # clicking opens the URL in the browser (viewer decides window/tab)
                            thumb_path=None,    # <- no thumbnail download
                            width=VIDEO_MAX_WIDTH,
                            height=VIDEO_MAX_HEIGHT,
                        )
                        story.append(indent_cell(badge, tabs=2, usable_w=usable_w))
                        story.append(Spacer(0, 6))
            else:
                # No comments/data → show fallback and (thanks to cross_all=True) all boxes are crossed
                story.append(Paragraph("Data not found in test data", missing_style))
                story.append(Spacer(0, 6))

            story.append(Spacer(0, 6))
        story.append(Spacer(0, 8))

    return story

# ----------------- Runner -----------------
def main():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Build header text from JSON (inspection -> address -> fullAddress)
    full_addr = first_nonempty(
        (((data.get("inspection") or {}).get("address") or {}).get("fullAddress")),
        ""
    )
    header_text = f"Report Identification: {full_addr}".strip()

    doc = SimpleDocTemplate(
        BODY_PDF,
        pagesize=PAGE_SIZE,
        leftMargin=MARGINS["left"], rightMargin=MARGINS["right"],
        topMargin=MARGINS["top"], bottomMargin=MARGINS["bottom"],
        title="Inspection Report",
    )

    story = build_story(data)
    onpage = make_onpage(header_text=header_text, footer_text=FOOTER_TEXT)
    doc.build(
        story,
        onFirstPage=onpage,
        onLaterPages=onpage,
        canvasmaker=lambda *a, **k: NumberedCanvas(
            *a, **k,
            page_offset=2,                           # pages 1–2 are the TREC pages
            left_margin=MARGINS["left"],
            right_margin=MARGINS["right"],
            bottom_margin=MARGINS["bottom"],
            font_name=FONT_NAME,
            font_size=9
        ),
    )
    print(f"Body PDF created: {BODY_PDF}")

    # Now prepend the first 2 pages from the TREC template and fill page 1
    prepend_trec_pages(
        json_path=JSON_PATH,                 # your existing JSON path
        template_pdf=TREC_TEMPLATE_PDF,      # the provided template
        body_pdf=BODY_PDF,                   # what we just built
        out_pdf=OUT_PDF                      # final output
    )
    print(f"Final PDF created: {OUT_PDF}")

if __name__ == "__main__":
    main()
