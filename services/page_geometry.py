import pymupdf as fitz
from typing import Dict, Any, Optional, Union

from services.erase_service import unghost


def extract_page_geometry(
    pdf_source: Union[str, fitz.Document], page_number: int = 0, ghost: Optional[list] = None
) -> Dict[str, Any]:
    if isinstance(pdf_source, str):
        doc = fitz.open(pdf_source)
        should_close = True
    else:
        doc = pdf_source
        should_close = False

    page = doc.load_page(page_number)
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    page_data = page.get_text("rawdict")

    extracted_blocks = []
    for block in page_data.get("blocks", []):
        if block.get("type") != 0:
            continue

        block_record = {"bbox": block["bbox"], "lines": []}
        for line in block.get("lines", []):
            line_record = {
                "bbox": line["bbox"],
                "wmode": line.get("wmode", 0),
                "dir": line.get("dir", (1.0, 0.0)),
                "spans": [],
            }
            for span in line.get("spans", []):
                span_record = {
                    "font": span.get("font"),
                    "size": span.get("size"),
                    "color": span.get("color"),
                    "flags": span.get("flags"),
                    "chars": [],
                }
                for char in span.get("chars", []):
                    span_record["chars"].append({
                        "char": char.get("c"),
                        "origin": char.get("origin"),
                        "bbox": char.get("bbox"),
                    })
                line_record["spans"].append(span_record)
            block_record["lines"].append(line_record)
        extracted_blocks.append(block_record)

    if ghost:
        _drop_ghosts(extracted_blocks, ghost)

    if should_close:
        doc.close()

    return {
        "page_index": page_number,
        "page_width": page_width,
        "page_height": page_height,
        "blocks": extracted_blocks,
    }


def _drop_ghosts(blocks, ghost):
    """Remove glyphs that were hidden (clipped) by an earlier edit, so the editor never offers
    old and new text together. Lines left with no visible letter are dropped too."""
    flat = [(c, c["origin"][0], c["origin"][1]) for b in blocks for ln in b["lines"]
            for sp in ln["spans"] for c in sp["chars"] if c["char"].strip()]
    keep = {id(t[0]) for t in unghost(flat, ghost, lambda t: (t[0]["char"], t[1], t[2]))}
    for b in blocks:
        for ln in b["lines"]:
            for sp in ln["spans"]:
                sp["chars"] = [c for c in sp["chars"] if not c["char"].strip() or id(c) in keep]
            ln["spans"] = [sp for sp in ln["spans"] if any(c["char"].strip() for c in sp["chars"])]
        b["lines"] = [ln for ln in b["lines"] if ln["spans"]]
    blocks[:] = [b for b in blocks if b["lines"]]
