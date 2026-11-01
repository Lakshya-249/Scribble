import pymupdf as fitz
from typing import Dict, Any, Optional, Union, List, Tuple

from services.erase_service import unghost


def _is_invisible_patch(d: Dict[str, Any]) -> bool:
    """Drawings that are not visible lines or borders and must never cut text lines apart:
      - clip paths (the clip our own 'hide old text' step adds, or a viewer clip around an image/table);
      - fill-only near-white rectangles: the background patches that erasing / moving text paints over
        old content. Their EDGES used to be read as table borders, so after an edit a paragraph was
        split along the patch (a box ending in the middle of a word).
    Real borders are stroked ('s'/'fs') or dark / coloured, so they are kept."""
    t = d.get("type")
    if t == "clip":
        return True
    if t == "f":
        fill = d.get("fill")
        if isinstance(fill, (list, tuple)) and fill:
            ch = fill[:3] if len(fill) >= 3 else fill[:1]
            if min(ch) >= 0.93:
                return True
    return False


def _extract_vector_primitives(
    page: fitz.Page,
) -> Tuple[List[Tuple[float, float, float, float, int]], List[Tuple[float, float, float, float]], List[Dict[str, Any]]]:
    """
    Extracts vector lines and rectangles from the PDF page and separates them into:
      - h_segments: (x0, y0, x1, y1, color_int) horizontal rules / underlines / table row borders
      - v_barriers: (x0, y0, x1, y1) vertical column dividers / table cell borders
      - boxes:      [{"bbox": (x0, y0, x1, y1)}] rectangular containers
    """
    h_segments: List[Tuple[float, float, float, float, int]] = []
    v_barriers: List[Tuple[float, float, float, float]] = []
    boxes: List[Dict[str, Any]] = []

    try:
        drawings = page.get_drawings()
    except Exception:
        return h_segments, v_barriers, boxes

    for d in drawings:
        rect = d.get("rect")
        if rect is None or _is_invisible_patch(d):
            continue
        rx0, ry0, rx1, ry1 = float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
        w = rx1 - rx0
        h = ry1 - ry0
        color = d.get("color") or d.get("fill") or (0, 0, 0)
        if isinstance(color, (list, tuple)) and len(color) >= 3:
            c_int = (round(color[0] * 255) << 16) | (round(color[1] * 255) << 8) | round(color[2] * 255)
        else:
            c_int = 0

        if w >= 4.0 and h <= 3.5:
            h_segments.append((rx0, ry0, rx1, ry1, c_int))
            continue

        if h >= 6.0 and w <= 3.5:
            v_barriers.append((rx0, ry0, rx1, ry1))
            continue

        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                x0, y0 = min(p1.x, p2.x), min(p1.y, p2.y)
                x1, y1 = max(p1.x, p2.x), max(p1.y, p2.y)
                lw = float(d.get("width") or 1.0)
                if abs(p1.y - p2.y) <= 1.5 and (x1 - x0) >= 4.0:
                    h_segments.append((x0, y0 - lw * 0.5, x1, y1 + lw * 0.5, c_int))
                elif abs(p1.x - p2.x) <= 1.5 and (y1 - y0) >= 6.0:
                    v_barriers.append((x0 - lw * 0.5, y0, x1 + lw * 0.5, y1))
            elif kind == "re":
                r = item[1]
                bx0, by0, bx1, by1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
                bw, bh = bx1 - bx0, by1 - by0
                if bw >= 4.0 and bh <= 3.5:
                    h_segments.append((bx0, by0, bx1, by1, c_int))
                elif bh >= 6.0 and bw <= 3.5:
                    v_barriers.append((bx0, by0, bx1, by1))
                elif bw >= 12.0 and bh >= 8.0:
                    boxes.append({"bbox": (bx0, by0, bx1, by1)})
                    h_segments.append((bx0, by0 - 0.5, bx1, by0 + 0.5, c_int))
                    h_segments.append((bx0, by1 - 0.5, bx1, by1 + 0.5, c_int))
                    v_barriers.append((bx0 - 0.5, by0, bx0 + 0.5, by1))
                    v_barriers.append((bx1 - 0.5, by0, bx1 + 0.5, by1))

    return h_segments, v_barriers, boxes


def _stroke_traces(page: fitz.Page):
    """Text painted as OUTLINE (render mode 1 or 2 with a line width) -> [(bbox, size, line width)]. Producers use
    it as a faux bold: the same text is drawn once filled and once stroked. Rebuilding that text as plain fill
    looks ~40% thinner, so the width is carried on the span and repeated when the text is drawn again."""
    out = []
    try:
        for tr in page.get_texttrace():
            lw = tr.get("linewidth")
            if tr.get("type") in (1, 2) and lw and lw > 0:
                out.append((fitz.Rect(tr["bbox"]), float(tr.get("size") or 0.0), float(lw)))
    except Exception:
        pass
    return out


def _stroke_for(span_bbox, size, traces) -> float:
    r = fitz.Rect(span_bbox)
    cx, cy = (r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0
    for tb, ts, lw in traces:
        if tb.x0 - 1.0 <= cx <= tb.x1 + 1.0 and tb.y0 - 1.0 <= cy <= tb.y1 + 1.0 and abs(ts - float(size or 0)) < 0.6:
            return lw
    return 0.0


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
    # Explicitly disable ligature splitting anomalies by reading rawdict cleanly
    page_data = page.get_text("rawdict")

    h_segments, v_barriers, boxes = _extract_vector_primitives(page)

    stroke_traces = _stroke_traces(page)
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
                    "stroke": _stroke_for(span.get("bbox", line["bbox"]), span.get("size"), stroke_traces) if stroke_traces else 0.0,
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
        "h_segments": h_segments,
        "v_barriers": v_barriers,
        "boxes": boxes,
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
