import os
import copy
import time
from typing import List, Optional

import pymupdf as fitz
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from services import sessions
from services.sessions import Session
from services.page_geometry import extract_page_geometry
from services.spatial_cluster import SpatialClusterer, ParagraphBlock
from services.obj_reflow import TextReflowEngine
from services.compiler import PDFCompiler, draw_text_with_glyph_fallback, compute_empirical_width_scale
from services.erase_service import safe_erase
from services.diff_edit import try_diff_edit
from services.font_service import reset_cache, font_chain

router = APIRouter()

MAX_PX = 4000
ZOOM = 1.6
MAX_UNDO = 15
NO_DOC = "Please open your PDF first."


def need(request: Request) -> Session:
    s, _ = sessions.get(request.headers.get("cookie"), create=False)
    if s is None or s.doc is None:
        raise HTTPException(status_code=400, detail=NO_DOC)
    return s


def zoom_of(page: fitz.Page) -> float:
    return min(ZOOM, MAX_PX / max(page.rect.width, page.rect.height))


def _drop_snapshot_files(s: Session):
    for path, _ in s.undo:
        try:
            os.remove(path)
        except OSError:
            pass
    s.undo = []


def take_snapshot(s: Session):
    path = os.path.join(s.dir, f"u{time.time_ns()}.pdf")
    s.doc.save(path)
    s.undo.append((path, copy.deepcopy(s.ghost)))
    while len(s.undo) > MAX_UNDO:
        try:
            os.remove(s.undo.pop(0)[0])
        except OSError:
            pass


def restore_snapshot(s: Session):
    path, ghost = s.undo.pop()
    s.doc.close()
    d = fitz.open(path)
    if d.needs_pass:
        d.authenticate(s.pw)
    s.doc, s.ghost, s.blocks = d, ghost, {}
    reset_cache(s.cache)


def blocks_for(s: Session, page_idx: int) -> List[ParagraphBlock]:
    geom = extract_page_geometry(s.doc, page_idx, ghost=s.ghost.get(page_idx))
    return SpatialClusterer().process(geom)


def find_target_block(blocks: List[ParagraphBlock], op: dict) -> Optional[ParagraphBlock]:
    if not blocks:
        return None
    block_id = op.get("block_id")
    op_bbox = op.get("bbox", [])
    old_text = op.get("old", "").strip()

    if isinstance(block_id, int) and 0 <= block_id < len(blocks):
        cand = blocks[block_id]
        if not op_bbox or max(abs(a - c) for a, c in zip(cand.bbox, op_bbox)) < 25.0:
            return cand

    norm_old = " ".join(old_text.split())
    if norm_old:
        for b in blocks:
            if " ".join(b.text.strip().split()) == norm_old:
                return b

    if op_bbox and len(op_bbox) == 4:
        cx, cy = (op_bbox[0] + op_bbox[2]) / 2.0, (op_bbox[1] + op_bbox[3]) / 2.0
        dist = lambda b: ((b.bbox[0] + b.bbox[2]) / 2.0 - cx) ** 2 + ((b.bbox[1] + b.bbox[3]) / 2.0 - cy) ** 2
        best = min(blocks, key=dist)
        if dist(best) < 10000:
            return best

    if norm_old:
        for b in blocks:
            nb = " ".join(b.text.strip().split())
            if nb and (norm_old in nb or nb in norm_old):
                return b
    return None


def _open(s: Session, body: bytes, password: str) -> dict:
    with s.lock, sessions.HEAVY:
        path = os.path.join(s.dir, f"doc{time.time_ns()}.pdf")
        with open(path, "wb") as f:
            f.write(body)
        try:
            doc = fitz.open(path)
        except Exception:
            os.remove(path)
            raise HTTPException(status_code=400, detail="This file is not a readable PDF.")
        if doc.needs_pass and not doc.authenticate(password):
            doc.close()
            os.remove(path)
            raise HTTPException(status_code=401, detail="password")
        if doc.page_count == 0:
            doc.close()
            raise HTTPException(status_code=400, detail="This PDF has no pages.")
        if doc.page_count > sessions.MAX_PAGES:
            doc.close()
            raise HTTPException(status_code=400, detail=f"Too many pages (limit {sessions.MAX_PAGES}).")

        old = s.doc
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        _drop_snapshot_files(s)
        reset_cache(s.cache)
        s.doc, s.pw, s.ghost, s.blocks = doc, password, {}, {}
        return {"pages": [{"w": p.rect.width, "h": p.rect.height, "z": zoom_of(p)} for p in doc]}


@router.post("/open")
async def open_pdf(request: Request, password: str = ""):
    s, is_new = sessions.get(request.headers.get("cookie"), create=True)
    if s is None:
        return JSONResponse({"error": "The editor is busy. Please try again in a few minutes."}, status_code=503)
    body = await request.body()
    try:
        data = await run_in_threadpool(_open, s, body, password)
        resp = JSONResponse(data)
    except HTTPException as e:
        resp = JSONResponse({"error": e.detail, "detail": e.detail}, status_code=e.status_code)
    if is_new:
        resp.set_cookie("sid", s.id, httponly=True, samesite="strict", path="/",
                        secure=os.environ.get("COOKIE_SECURE") == "1")
    return resp


@router.get("/pageimg/{page_idx}")
def get_page_image(page_idx: int, request: Request):
    s = need(request)
    with s.lock, sessions.HEAVY:
        if not 0 <= page_idx < len(s.doc):
            raise HTTPException(status_code=404, detail="Page not found")
        pg = s.doc[page_idx]
        z = zoom_of(pg)
        pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        return Response(content=pix.tobytes("png"), media_type="image/png")


@router.get("/text/{page_idx}")
def get_page_text(page_idx: int, request: Request):
    s = need(request)
    with s.lock, sessions.HEAVY:
        if not 0 <= page_idx < len(s.doc):
            raise HTTPException(status_code=404, detail="Page not found")
        blocks = blocks_for(s, page_idx)
        s.blocks[page_idx] = blocks
        pg = s.doc[page_idx]
        out = []
        for idx, p in enumerate(blocks):
            first = p.lines[0].tokens[0] if (p.lines and p.lines[0].tokens) else None
            sz = p.lines[0].dominant_font_size if p.lines else 11.0
            lh = p.measured_line_height if p.measured_line_height else (sz * 1.28)
            out.append({
                "id": idx,
                "text": p.text,
                "bbox": list(p.bbox),
                "dbox": list(fitz.Rect(p.bbox) * pg.rotation_matrix),
                "font": first.font_name if first else "helv",
                "size": sz,
                "line_height": lh,
                "flags": first.flags if first else 0,
                "color": first.color if first else 0,
                "align": p.alignment,
                "lines_count": len(p.lines),
                "max_w": (p.container_max_x1 - p.bbox[0]) if p.container_max_x1 else (p.bbox[2] - p.bbox[0]),
            })
        return out


SERIF_ETC = {"sans": ("Helvetica", 0), "serif": ("Times", 4), "mono": ("Courier", 8)}


def _edit(s: Session, op: dict) -> dict:
    with s.lock, sessions.HEAVY:
        doc = s.doc
        op_type = op.get("type")
        if op_type not in ("line", "add", "cover"):
            raise HTTPException(status_code=400, detail=f"Unknown op type: {op_type}")
        try:
            page_num = int(op.get("page", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid page number")
        if not 0 <= page_num < len(doc):
            raise HTTPException(status_code=400, detail="Invalid page number")
        if op_type == "line" and op.get("old", "") == op.get("new", ""):
            return {"unchanged": True, "missing": "", "note": ""}

        page = doc[page_num]
        take_snapshot(s)
        try:
            ghost = s.ghost.setdefault(page_num, [])

            if op_type == "line":
                new_text = op.get("new", "")
                blocks = s.blocks.get(page_num) or blocks_for(s, page_num)
                target = find_target_block(blocks, op)
                if target is None:
                    raise ValueError("Text not found on this page - reload the page and try again.")

                # 1) minimal-diff edit: unchanged glyphs, borders and underlines are never touched
                rep = try_diff_edit(doc, page_num, target, new_text, blocks, s.cache, ghost)
                if rep is not None:
                    s.blocks.pop(page_num, None)
                    return {"missing": rep["substituted"], "note": "Edit applied"}

                # 2) fallback: re-typeset the paragraph (old behaviour)
                first_baseline = target.lines[0].baseline_y if target.lines else target.bbox[1] + 10.0
                size = target.lines[0].dominant_font_size if target.lines else 11.0
                first_tok = target.lines[0].tokens[0] if (target.lines and target.lines[0].tokens) else None
                fname = first_tok.font_name if first_tok else "helv"
                fflags = first_tok.flags if first_tok else 0

                chain = font_chain(doc, page, fname, fflags, cache=s.cache)
                primary_font = chain[0][0] if chain else fitz.Font("helv")
                w_scale = compute_empirical_width_scale(page, doc, target, s.cache)

                def _measure(txt: str) -> float:
                    return primary_font.text_length(txt, fontsize=size) * w_scale

                max_single_w = (target.container_max_x1 - target.bbox[0]) if target.container_max_x1 else (target.bbox[2] - target.bbox[0])
                if (len(target.lines) == 1
                        and "\n" not in new_text
                        and _measure(new_text) <= max(max_single_w, target.bbox[2] - target.bbox[0]) + 15.0):
                    reflow = TextReflowEngine.single_line(new_text, target.bbox, first_baseline)
                else:
                    orig_baselines = [ln.baseline_y for ln in target.lines]
                    orig_text_lefts = [ln.text_x0 for ln in target.lines]
                    reflow = TextReflowEngine(
                        font_name="helv",
                        font_size=size,
                        line_height=target.measured_line_height,
                        measure_fn=_measure,
                    ).reflow_paragraph(
                        new_text=new_text,
                        original_bbox=target.bbox,
                        initial_baseline_y=first_baseline,
                        page_width=float(page.rect.width),
                        container_max_x1=target.container_max_x1,
                        first_line_indent=target.first_line_indent,
                        hanging_indent=target.hanging_indent,
                        alignment=target.alignment,
                        original_line_baselines=orig_baselines,
                        original_line_text_lefts=orig_text_lefts,
                    )

                report = PDFCompiler(doc, cache=s.cache).apply_edit_and_cascade(
                    page_num=page_num, target_block=target, reflow_res=reflow,
                    all_blocks=blocks, new_text=new_text, ghost=ghost)
                s.blocks.pop(page_num, None)
                return {"missing": report["substituted"], "note": "Edit applied"}

            if op_type == "add":
                fam, fam_flags = SERIF_ETC.get(op.get("family"), SERIF_ETC["sans"])
                flags = fam_flags | (16 if op.get("bold") else 0) | (2 if op.get("italic") else 0)
                pt = fitz.Point(op["x"], op["y"]) * page.derotation_matrix
                c = int(op.get("color", 0))
                draw_text_with_glyph_fallback(
                    page, doc, (pt.x, pt.y), op["text"], fam, flags, float(op.get("size", 12)),
                    ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255), cache=s.cache)
                s.blocks.pop(page_num, None)
                return {"missing": "", "note": ""}

            r = fitz.Rect(op["rect"]) * page.derotation_matrix
            r.normalize()
            if r.width >= 1 and r.height >= 1:
                safe_erase(page, r, fill_bg=True, images=2, ghost=ghost)
            s.blocks.pop(page_num, None)
            return {"missing": "", "note": ""}

        except HTTPException:
            restore_snapshot(s)
            raise
        except Exception as e:
            restore_snapshot(s)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/edit")
async def apply_edit(request: Request):
    s = need(request)
    try:
        op = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Bad request.")
    if not isinstance(op, dict):
        raise HTTPException(status_code=400, detail="Bad request.")
    return await run_in_threadpool(_edit, s, op)


@router.post("/undo")
def undo_pdf(request: Request):
    s = need(request)
    with s.lock:
        if not s.undo:
            raise HTTPException(status_code=400, detail="Nothing to undo")
        restore_snapshot(s)
        return {"status": "undone"}


@router.get("/save")
def save_pdf(request: Request):
    s = need(request)
    with s.lock, sessions.HEAVY:
        data = None
        if not s.doc.is_encrypted:
            try:
                d2 = fitz.open("pdf", s.doc.tobytes())
                d2.subset_fonts()
                data = d2.tobytes(garbage=3, deflate=True)
                d2.close()
            except Exception:
                data = None
        final = data or s.doc.tobytes(garbage=3, deflate=True)
        return Response(content=final, media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=edited.pdf"})
