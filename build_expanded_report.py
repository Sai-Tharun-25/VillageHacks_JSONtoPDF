#!/usr/bin/env python3
"""
Build a TREC report with flowing narrative (no big text boxes).
- Keep official TREC Page 1–2 from the blank template (Page 1 fields are filled).
- From page 3 onward, generate narrative pages that can expand indefinitely.
- Images come from local paths or data-URIs (no downloading).

Required:
  - TREC_Template_Blank.pdf
  - inspection.json
  - mappings.json
Outputs:
  - TREC_Expanded_Report.pdf
"""

from pathlib import Path
import re
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    NameObject, BooleanObject, TextStringObject, DictionaryObject,
    IndirectObject, ArrayObject
)

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch

from util_map_and_extract import load_json, extract_header_info
from flow_layout import build_flow_pdf


# --- hardcoded paths ---
TEMPLATE   = Path("TREC_Template_Blank.pdf")
JSON_FILE  = Path("inspection.json")
MAP_FILE   = Path("mappings.json")
FLOW_TMP   = Path("~flow_pages_tmp.pdf")
OUTPUT     = Path("TREC_Expanded_Report.pdf")
PAGE1_DEBUG = Path("~page1_field_debug.txt")


def _as_obj(x):
    try:
        return x.get_object() if isinstance(x, IndirectObject) else x
    except Exception:
        return x

def _add_front_pages_and_strip_fields(writer: PdfWriter, template_reader: PdfReader, count: int = 2) -> None:
    """
    Add the first `count` pages from the blank TREC template, then remove all /Widget annotations
    so the final report has NO interactive form fields.
    """
    n = min(count, len(template_reader.pages))
    for i in range(n):
        writer.add_page(template_reader.pages[i])

    # Strip annotations from those front pages (removes all form inputs visually & interactively)
    for i in range(n):
        page = writer.pages[i]
        if "/Annots" in page:
            page[NameObject("/Annots")] = ArrayObject()

    # Also ensure the doc root has no /AcroForm dictionary
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]

def _fill_page1_by_overlay(writer: PdfWriter, header: dict, tmp_overlay: Path = Path("~p1_overlay.pdf")) -> None:
    """
    Draw Name of Client, Date of Inspection, Address, Inspector, TREC #, Sponsor, Sponsor TREC #
    directly onto Page 1 using a 1-page overlay PDF, then merge it.
    This avoids all form fields and leaves plain, printable page content.
    """
    # 1) Build a 1-page overlay at approximate field locations on the TREC Page 1
    c = canvas.Canvas(str(tmp_overlay), pagesize=letter)
    c.setFont("Helvetica", 10)

    # Coordinates tuned for the TREC box layout on Page 1 (letter, portrait)
    # left column x, right column x (where text should land inside the boxes)
    xL, xR = 1.25*inch, 4.75*inch
    # row baselines from top box down (Y decreases downward)
    y1 = 9.15*inch   # Name of Client / Date of Inspection
    y2 = 8.60*inch   # Address of Inspected Property
    y3 = 8.05*inch   # Name of Inspector / TREC License #
    y4 = 7.50*inch   # Name of Sponsor / TREC License # (sponsor)

    c.drawString(xL, y1, header.get("client",""))
    c.drawString(xR, y1, header.get("date",""))
    c.drawString(xL, y2, header.get("address",""))
    c.drawString(xL, y3, header.get("inspector",""))
    c.drawString(xR, y3, header.get("trec_license",""))
    c.drawString(xL, y4, header.get("sponsor",""))
    c.drawString(xR, y4, header.get("sponsor_license",""))

    c.showPage()
    c.save()

    # 2) Merge overlay onto Page 1
    ov = PdfReader(str(tmp_overlay))
    writer.pages[0].merge_page(ov.pages[0])

def _add_appearances_helv8(writer: PdfWriter, reader: PdfReader):
    root = _as_obj(reader.trailer.get("/Root"))
    if not isinstance(root, dict): return
    acro = _as_obj(root.get("/AcroForm"))
    if not isinstance(acro, dict): return
    writer._root_object.update({NameObject("/AcroForm"): acro})
    acroform = writer._root_object["/AcroForm"]
    acroform.update({NameObject("/NeedAppearances"): BooleanObject(True)})
    acroform.update({NameObject("/DA"): TextStringObject("/Helv 8 Tf 0 g")})

    # ensure /Helv present
    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font_dict)
    dr = acroform.get("/DR") or DictionaryObject()
    fonts = dr.get("/Font") or DictionaryObject()
    fonts.update({NameObject("/Helv"): font_ref})
    dr.update({NameObject("/Font"): fonts})
    acroform.update({NameObject("/DR"): dr})

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def _enumerate_text_fields(reader: PdfReader) -> list[str]:
    """
    Return a flat list of /T names for Tx fields (including kids).
    """
    names = []
    root = _as_obj(reader.trailer.get("/Root"))
    acro = _as_obj(root.get("/AcroForm")) if isinstance(root, dict) else None
    fields = _as_obj(acro.get("/Fields")) if isinstance(acro, dict) else None
    if not fields: return names

    def walk(ref):
        f = _as_obj(ref)
        if not isinstance(f, dict): return
        if f.get("/FT") == NameObject("/Tx") and f.get("/T"):
            names.append(str(f.get("/T")))
        for k in _as_obj(f.get("/Kids") or []):
            walk(k)

    for ref in fields:
        walk(ref)
    return names

def _fill_page1_identity_fuzzy(writer: PdfWriter, base_reader: PdfReader, header: dict):
    """
    Fuzzy-fill the Page 1 identity fields. We match against common labels:
      - Name of Client
      - Date of Inspection
      - Address of Inspected Property
      - Name of Inspector
      - TREC License #                (inspector license)
      - Name of Sponsor (if applicable)
      - TREC License # (sponsor)      (fields that contain 'sponsor' + 'license')
    """
    wanted = {
        "nameofclient": header.get("client", ""),
        "dateofinspection": header.get("date", ""),
        "addressofinspectedproperty": header.get("address", ""),
        "nameofinspector": header.get("inspector", ""),
    }
    inspector_lic  = header.get("trec_license", "")
    sponsor_name   = header.get("sponsor", "")
    sponsor_lic    = header.get("sponsor_license", "")

    all_names = _enumerate_text_fields(base_reader)
    mapped = {}

    for fld in all_names:
        n = _norm(fld)
        # direct label matches
        if n in wanted and wanted[n]:
            mapped[fld] = wanted[n]
            continue
        # sponsor vs inspector license/name disambiguation
        if "treclicense" in n:
            if "sponsor" in n and sponsor_lic:
                mapped[fld] = sponsor_lic
            elif inspector_lic:
                mapped[fld] = inspector_lic
        elif "nameof" in n and "sponsor" in n and sponsor_name:
            mapped[fld] = sponsor_name

    # Apply to all pages (whichever copy of the field exists will get the value)
    hits = []
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, mapped)
            hits.extend(mapped.keys())
        except Exception:
            pass

    # Debug: write what we found/mapped
    try:
        PAGE1_DEBUG.write_text(
            "Fields discovered:\n- " + "\n- ".join(all_names) +
            "\n\nMapped values:\n" + "\n".join([f"{k} => {v}" for k,v in mapped.items()]),
            encoding="utf-8"
        )
    except Exception:
        pass


def main():
    for p in [TEMPLATE, JSON_FILE, MAP_FILE]:
        if not p.exists():
            raise FileNotFoundError(f"Missing required file: {p}")

    data   = load_json(JSON_FILE)
    header = extract_header_info(data)

    # 1) Build flow pages (narrative + images + checklist)
    build_flow_pdf(JSON_FILE, MAP_FILE, FLOW_TMP, header)

    # 2) Keep official TREC Page 1–2 from the blank template (form fields intact)
    rdr_tpl = PdfReader(str(TEMPLATE))
    wr = PdfWriter()

    # Keep BOTH Page 1 and Page 2; remove all fields so report has no inputs
    _add_front_pages_and_strip_fields(wr, rdr_tpl, count=2)

    # Draw Page 1 identity values as content (no fields)
    _fill_page1_by_overlay(wr, header)  # <-- fills first page robustly

    # 3) Append flow pages
    rdr_flow = PdfReader(str(FLOW_TMP))
    for p in rdr_flow.pages:
        wr.add_page(p)

    with open(OUTPUT, "wb") as f:
        wr.write(f)

    print(f"Wrote: {OUTPUT.resolve()}")
    print(f"Debug written: {PAGE1_DEBUG.resolve()}")

if __name__ == "__main__":
    main()
