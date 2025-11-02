#!/usr/bin/env python3
"""
Probe the AcroForm tree (ignoring page /Annots) and write a visible test
value into EVERY /Tx field so we can see what's actually fillable.

Outputs:
  - TREC_AllFields_TEST.pdf       (open in Acrobat and look for [[TEST:<name>]] everywhere)
  - acro_fields_full_dump.txt     (complete field tree dump: name, type, flags, multiline, kids)
"""

from pathlib import Path
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    NameObject, BooleanObject, TextStringObject, DictionaryObject,
    IndirectObject, ArrayObject, NumberObject
)

PDF_IN  = "TREC_Template_Blank.pdf"
PDF_OUT = "TREC_AllFields_TEST.pdf"
DUMP_TXT= "acro_fields_full_dump.txt"

MULTILINE_BIT = 1 << 12  # 4096

def as_obj(x):
    return x.get_object() if isinstance(x, IndirectObject) else x

def get_acroform(reader: PdfReader):
    root = as_obj(reader.trailer.get("/Root"))
    if not isinstance(root, dict):
        return None
    return as_obj(root.get("/AcroForm"))

def ensure_appearances(writer: PdfWriter, reader: PdfReader):
    acro = get_acroform(reader)
    if not isinstance(acro, dict):
        return
    writer._root_object.update({NameObject("/AcroForm"): acro})
    acroform = writer._root_object["/AcroForm"]
    acroform.update({NameObject("/NeedAppearances"): BooleanObject(True)})
    acroform.update({NameObject("/DA"): TextStringObject("/Helv 0 Tf 0 g")})
    # add Helvetica so /Helv resolves
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

def dump_fields(acro: DictionaryObject):
    lines = []
    def walk(flds, depth=0):
        for f in flds or []:
            obj = as_obj(f)
            if not isinstance(obj, dict):
                continue
            nm = obj.get("/T")
            ft = obj.get("/FT")
            ff = int(obj.get("/Ff") or 0)
            kids = obj.get("/Kids")
            ml = bool(ff & MULTILINE_BIT)
            lines.append(("  " * depth) + f"{nm} \t{ft} \tFf={ff} \tMultiline={int(ml)}")
            if kids:
                walk(kids, depth+1)
    walk(acro.get("/Fields", []))
    return "\n".join(lines) if lines else "(No fields found in /AcroForm)"

def set_all_tx_values(writer: PdfWriter, acro: DictionaryObject):
    """
    Write [[TEST:<name>]] into every /Tx field we can address by /T name,
    using update_page_form_field_values across all pages (works without /P).
    """
    # Build a flat mapping name -> value
    mapping = {}
    def collect(flds):
        for f in flds or []:
            obj = as_obj(f)
            if not isinstance(obj, dict):
                continue
            nm = obj.get("/T")
            ft = obj.get("/FT")
            kids = obj.get("/Kids")
            if ft == NameObject("/Tx") and nm:
                mapping[nm] = f"[[TEST:{nm}]]"
            if kids:
                collect(kids)
    collect(acro.get("/Fields", []))

    # Apply mapping to every page (safe: fields that don't belong are ignored)
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, mapping)
        except Exception:
            pass

def main():
    if not Path(PDF_IN).exists():
        raise FileNotFoundError(PDF_IN)
    reader = PdfReader(PDF_IN)
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)

    ensure_appearances(writer, reader)

    acro = get_acroform(reader)
    dump = dump_fields(acro) if isinstance(acro, dict) else "(No /AcroForm)"
    Path(DUMP_TXT).write_text(dump, encoding="utf-8")

    if isinstance(acro, dict):
        set_all_tx_values(writer, acro)

    with open(PDF_OUT, "wb") as f:
        writer.write(f)

    print(f"Wrote: {PDF_OUT}")
    print(f"Wrote: {DUMP_TXT}")

if __name__ == "__main__":
    main()
