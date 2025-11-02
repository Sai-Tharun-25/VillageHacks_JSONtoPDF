# trec_prefix.py
# pip install pypdf reportlab

import os, json
import re
from datetime import datetime
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, ArrayObject, BooleanObject, DictionaryObject
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth

# 1) "ADDITIONAL INFORMATION PROVIDED BY INSPECTOR" box (single-line here)
PAGE2_INFO_X      = 0.72 * inch   # left edge inside the big box
PAGE2_INFO_Y      = 9.10 * inch   # baseline inside the box
PAGE2_INFO_MAX_W  = 6.40 * inch   # usable width inside the box
PAGE2_INFO_FONT   = "Helvetica"
PAGE2_INFO_SIZE   = 10

# 2) The "Page 2 of __" blank on the right side footer area
# We only draw the TOTAL number ("__") part.
PAGE2_TOTAL_X     = 7.05 * inch   # where the blank sits for total pages
PAGE2_TOTAL_Y     = 0.70 * inch   # baseline near the footer line
PAGE2_TOTAL_FONT  = "Helvetica"
PAGE2_TOTAL_SIZE  = 10

# ---------- JSON helpers ----------
def _get(d, path, default=None):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def _first(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

from datetime import datetime

def _fmt_date(val):
    if val is None or val == "":
        return ""
    # epoch ms / seconds
    if isinstance(val, (int, float)):
        # value is in milliseconds in your JSON
        return datetime.fromtimestamp(val / 1000.0).strftime("%m/%d/%Y")
    # try common string formats
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s[:19], fmt).strftime("%m/%d/%Y")
        except Exception:
            pass
    return s


def _extract_vals(data: dict) -> dict:
    insp = data.get("inspection", {}) if isinstance(data, dict) else {}
    return {
        "client":    (_get(insp, "clientInfo.name") or "").strip(),
        "address":   (_get(insp, "address.fullAddress") or "").strip(),
        "inspector": (_get(insp, "inspector.name") or "").strip(),
        "date":      _fmt_date(
                         _get(insp, "schedule.date")
                         or _get(insp, "dateOfInspection")
                         or _get(insp, "date")
                     ),
    }

def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())

def _page_field_names(reader, page_index: int):
    names = []
    page = reader.pages[page_index]
    if "/Annots" in page:
        for a in page["/Annots"]:
            obj = a.get_object()
            if "/T" in obj:
                names.append(obj["/T"])
    return names

# ---------- tiny text-fit + overlay ----------
def _fit_draw(canvas, x, y, text, max_w, font=PAGE2_INFO_FONT, start=PAGE2_INFO_SIZE, min_size=8.0):
    """Shrink-to-fit single line."""
    t = text or ""
    sz = start
    while stringWidth(t, font, sz) > max_w and sz > min_size:
        sz -= 0.5
    canvas.setFont(font, sz)
    canvas.drawString(x, y, t)

def _make_overlay_pdf(path_out, values):
    """Draw Client / Date / Address / Inspector onto TREC page 1 areas."""
    c = canvas.Canvas(path_out, pagesize=letter)
    xL, wL = 0.72*inch, 3.15*inch   # left column x + width
    xR, wR = 4.92*inch, 2.25*inch   # right column x + width
    y1 = 9.40*inch  # Client / Date
    y2 = 9.08*inch  # Address
    y3 = 8.74*inch  # Inspector

    _fit_draw(c, xL, y1, values.get("client", ""),    max_w=wL)
    _fit_draw(c, xR, y1, values.get("date", ""),      max_w=wR)
    _fit_draw(c, xL, y2, values.get("address", ""),   max_w=wL)
    _fit_draw(c, xL, y3, values.get("inspector", ""), max_w=wL)

    c.showPage()
    c.save()

def _make_page2_overlay(path_out, *, addl_text: str, total_pages: int, page_w, page_h):
    """Overlay for TREC template page 2."""
    from reportlab.pdfgen import canvas as rl_canvas
    c = rl_canvas.Canvas(path_out, pagesize=(page_w, page_h))
    # 1) Additional info box
    _fit_draw(c, PAGE2_INFO_X, PAGE2_INFO_Y, addl_text, max_w=PAGE2_INFO_MAX_W,
              font=PAGE2_INFO_FONT, start=PAGE2_INFO_SIZE)
    # 2) Fill the total in 'Page 2 of __'
    c.setFont(PAGE2_TOTAL_FONT, PAGE2_TOTAL_SIZE)
    c.drawString(PAGE2_TOTAL_X, PAGE2_TOTAL_Y, str(total_pages))
    c.showPage()
    c.save()

# ---------- PUBLIC API (what main.py imports) ----------
def prepend_trec_pages(json_path: str, template_pdf: str, body_pdf: str, out_pdf: str):
    """
    out_pdf = [template p1 (overlay-filled) + template p2] + body pages
    Safe: refuses bad arg combos and writes atomically.
    """
    # --- load JSON values for overlay ---
    with open(json_path, "r", encoding="utf-8") as f:
        vals = _extract_vals(json.load(f))

    # --- SAFETY: outputs must be distinct from inputs ---
    ap = os.path.abspath
    if ap(out_pdf) == ap(body_pdf):
        raise ValueError("prepend_trec_pages: body_pdf and out_pdf must be different.")
    if ap(out_pdf) == ap(template_pdf):
        raise ValueError("prepend_trec_pages: template_pdf and out_pdf must be different.")

    # --- read inputs ---
    tpl = PdfReader(template_pdf)
    body = PdfReader(body_pdf)

    # --- assemble ---
    w = PdfWriter()
    # add first 2 template pages
    nfront = min(2, len(tpl.pages))
    for i in range(nfront):
        w.add_page(tpl.pages[i])

    # clear annots on page 1 so overlay isn't hidden
    page0 = w.pages[0]
    annots_key = NameObject("/Annots")
    if annots_key in page0:
        page0[annots_key] = ArrayObject()

    # overlay page 1 (client/date/address/inspector)
    tmp_overlay_p1 = out_pdf + ".__p1_overlay.pdf"
    _make_overlay_pdf(tmp_overlay_p1, vals)
    ov = PdfReader(tmp_overlay_p1)
    w.pages[0].merge_page(ov.pages[0])

    # ---- PAGE 2 overlay (two items) ----
    if nfront >= 2:
        page2 = w.pages[1]  # the template's second page in the writer

        # Copy AcroForm into the writer so fields exist at write time
        tpl_root = tpl.trailer["/Root"]
        if "/AcroForm" in tpl_root:
            w._root_object[NameObject("/AcroForm")] = tpl_root["/AcroForm"]
        else:
            w._root_object[NameObject("/AcroForm")] = DictionaryObject()
        # Ask viewers to regenerate appearances so text is visible
        w._root_object["/AcroForm"][NameObject("/NeedAppearances")] = BooleanObject(True)

        # Total pages in the FINAL doc = 2 template pages + body pages
        total_pages = nfront + len(body.pages)

        # FILL the two fields on page 2
        w.update_page_form_field_values(page2, {
            "Text1":     "Data not found in test data",  # Additional info box
            "Page 2 of": str(total_pages),               # the blank in “Page 2 of ___”
        })

    #print("[p2 fields]", _page_field_names(PdfReader("TREC_Template_Blank.pdf"), 1))
    # append body pages
    for pg in body.pages:
        w.add_page(pg)

    # ---- Atomic write + cleanup ----
    tmp_out = out_pdf + ".__tmp.pdf"
    with open(tmp_out, "wb") as f:
        w.write(f)
    os.replace(tmp_out, out_pdf)
    try:
        os.remove(tmp_overlay_p1)
    except Exception:
        pass
    



