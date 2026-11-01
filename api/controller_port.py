import copy
import logging
import os
import re
import time
import traceback
from typing import List, Optional
from urllib.parse import quote

import pymupdf as fitz
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from services import sessions
from services.sessions import Session
from services.page_geometry import extract_page_geometry
from services.spatial_cluster import SpatialClusterer, ParagraphBlock
from services.markers import find_markers, visible_right_edge
from services.obj_reflow import TextReflowEngine
from services.compiler import PDFCompiler, draw_text_with_glyph_fallback, compute_empirical_width_scale
from services.erase_service import safe_erase
from services.diff_edit import try_diff_edit
from services.font_service import reset_cache, font_chain

log = logging.getLogger("scribble.api")
router = APIRouter()

MAX_PX = 4000
MAX_IMAGE = 10 * 1024 * 1024
NO_DOC = "Your session has expired. Please open your PDF again."


def need(request: Request) -> Session:
    s, _ = sessions.get(request.headers.get("cookie"), create=False)
    if s is None or s.doc is None:
        raise HTTPException(status_code=400, detail=NO_DOC)
    return s


async def read_limited(request: Request, limit: int) -> bytes:
    """Read the body but stop as soon as it is too big. Content-Length can be missing or wrong
    (chunked uploads), so the size is enforced on the bytes actually received."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail=f"Too large (limit {limit >> 20} MB).")
    return bytes(buf)


def zoom_of(page: fitz.Page) -> float:
    return min(1.6, MAX_PX / max(page.rect.width, page.rect.height))


def page_meta(doc) -> list:
    return [{"w": p.rect.width, "h": p.rect.height, "z": zoom_of(p)} for p in doc]


def state(s: Session, **extra) -> dict:
    """What the client needs after every change."""
    return {"rev": s.rev, "can_undo": bool(s.undo), "can_redo": bool(s.redo), **extra}


def changed(s: Session):
    """Call after anything modified the document."""
    s.rev += 1
    s.render.clear()
    s.blocks = {}


# ---------- undo / redo snapshots ----------
def _remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _snapshot_file(s: Session) -> str:
    path = os.path.join(s.dir, f"u{time.time_ns()}.pdf")
    s.doc.save(path)
    return path


def _drop_snapshot_files(s: Session):
    for path, _ in s.undo + s.redo:
        _remove(path)
    s.undo, s.redo = [], []


def _trim(s: Session):
    def used():
        return sum(os.path.getsize(p) for p, _ in s.undo + s.redo if os.path.exists(p))
    while s.undo and (len(s.undo) > sessions.MAX_UNDO or (len(s.undo) > 1 and used() > sessions.MAX_UNDO_BYTES)):
        _remove(s.undo.pop(0)[0])
    while len(s.redo) > sessions.MAX_UNDO:
        _remove(s.redo.pop(0)[0])


def take_snapshot(s: Session):
    s.undo.append((_snapshot_file(s), copy.deepcopy(s.ghost)))
    for path, _ in s.redo:                      # a new change ends the redo history
        _remove(path)
    s.redo = []
    _trim(s)


def _load(s: Session, path: str, ghost: dict):
    with open(path, "rb") as f:
        data = f.read()
    _remove(path)
    d = fitz.open("pdf", data)
    if d.needs_pass:
        d.authenticate(s.pw)
    old, s.doc, s.ghost = s.doc, d, ghost
    try:
        old.close()
    except Exception:
        pass
    reset_cache(s.cache)
    changed(s)


def restore_snapshot(s: Session):
    path, ghost = s.undo.pop()
    _load(s, path, ghost)


def _char_widths(page, doc, block, new_text, w_scale, cache):
    """Rendered width of every character of new_text in the style it will be drawn with."""
    from services.compiler import layout_run
    from services.style_runs import Run, old_char_styles, new_char_styles
    styles = new_char_styles(block.text, old_char_styles(block), new_text)
    memo, out = {}, []
    for ch, st in zip(new_text, styles):
        k = (ch, st)
        if k not in memo:
            pieces, _ = layout_run(page, doc, Run(ch, st), cache, width_scale=w_scale)
            memo[k] = sum(p.width for p in pieces)
        out.append(memo[k])
    return out


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


def _clean_name(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[^\w .()\-]", "_", name, flags=re.UNICODE).strip(" .") or "document"
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name[:80]


def _open(s: Session, body: bytes, password: str, name: str) -> dict:
    with s.lock, sessions.HEAVY:
        path = os.path.join(s.dir, f"doc{time.time_ns()}.pdf")
        with open(path, "wb") as f:
            f.write(body)
        try:
            doc = fitz.open(path)
        except Exception:
            _remove(path)
            raise HTTPException(status_code=400, detail="This file is not a readable PDF.")
        if doc.needs_pass and not doc.authenticate(password):
            doc.close()
            _remove(path)
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
        s.doc, s.pw, s.ghost, s.name = doc, password, {}, _clean_name(name)
        changed(s)
        return {"pages": page_meta(doc), "name": s.name, **state(s)}


@router.post("/open")
async def open_pdf(request: Request, password: str = "", name: str = ""):
    s, is_new = sessions.get(request.headers.get("cookie"), create=True)
    if s is None:
        return JSONResponse({"error": "The editor is busy right now. Please try again in a few minutes."}, status_code=503)
    try:
        body = await read_limited(request, sessions.MAX_UPLOAD)
        data = await run_in_threadpool(_open, s, body, password, name)
        resp = JSONResponse(data)
    except HTTPException as e:
        resp = JSONResponse({"error": e.detail, "detail": e.detail}, status_code=e.status_code)
    if is_new:
        resp.set_cookie("sid", s.id, httponly=True, samesite="strict", path="/",
                        secure=os.environ.get("COOKIE_SECURE") == "1")
    return resp


@router.get("/ping")
def ping(request: Request):
    """Keeps the session alive while the tab is open and tells the client if it was lost."""
    s, _ = sessions.get(request.headers.get("cookie"), create=False)
    return {"alive": bool(s and s.doc is not None)}


@router.delete("/session")
def delete_session(request: Request):
    """'Delete my file now': document, undo history and temp files are removed immediately."""
    sessions.end(request.headers.get("cookie"))
    resp = JSONResponse({"deleted": True})
    resp.delete_cookie("sid", path="/")
    return resp


@router.get("/pageimg/{page_idx}")
def get_page_image(page_idx: int, request: Request, s: float = 1.6):
    sess = need(request)
    with sess.lock, sessions.HEAVY:
        if not 0 <= page_idx < len(sess.doc):
            raise HTTPException(status_code=404, detail="Page not found")
        pg = sess.doc[page_idx]
        want = min(4.0, max(0.5, round(s * 4) / 4))                  # steps of 0.25 keep the cache useful
        z = min(want, MAX_PX / max(pg.rect.width, pg.rect.height))
        key = (page_idx, round(z, 3))
        hit = sess.render.get(key)
        if hit is None:
            pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            png = pix.tobytes("png")
            # photos / scans make huge PNGs: JPEG is several times smaller and fine for those pages
            hit = (png, "image/png") if len(png) < 1_200_000 else (pix.tobytes("jpeg", jpg_quality=88), "image/jpeg")
            sess.render[key] = hit
            while len(sess.render) > sessions.RENDER_CACHE:
                sess.render.popitem(last=False)
        else:
            sess.render.move_to_end(key)
        return Response(content=hit[0], media_type=hit[1],
                        headers={"Cache-Control": "private, max-age=86400"})


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


@router.get("/search")
def search(q: str, request: Request):
    s = need(request)
    q = q.strip()
    if not 1 <= len(q) <= 100:
        raise HTTPException(status_code=400, detail="Type something to search for.")
    with s.lock, sessions.HEAVY:
        hits, total, truncated = [], 0, False
        for i, pg in enumerate(s.doc):
            rects = []
            for r in pg.search_for(q):
                d = fitz.Rect(r) * pg.rotation_matrix
                d.normalize()
                rects.append(list(d))
            if rects:
                hits.append({"page": i, "rects": rects})
                total += len(rects)
            if total >= 500:
                truncated = True
                break
        return {"count": total, "hits": hits, "truncated": truncated}


SERIF_ETC = {"sans": ("Helvetica", 0), "serif": ("Times", 4), "mono": ("Courier", 8)}


def _bad(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail=msg)


def _edit(s: Session, op: dict) -> dict:
    with s.lock, sessions.HEAVY:
        doc = s.doc
        op_type = op.get("type")
        if op_type not in ("line", "add", "cover", "highlight"):
            _bad(f"Unknown op type: {op_type}")
        try:
            page_num = int(op.get("page", 0))
        except (TypeError, ValueError):
            _bad("Invalid page number")
        if not 0 <= page_num < len(doc):
            _bad("Invalid page number")
        if op_type == "line" and (not isinstance(op.get("new", ""), str) or len(op.get("new", "")) > 20000):
            _bad("That text is too long.")
        if op_type == "line" and op.get("old", "") == op.get("new", "") and not op.get("marks"):
            return state(s, unchanged=True, missing="", note="")

        page = doc[page_num]
        take_snapshot(s)
        try:
            ghost = s.ghost.setdefault(page_num, [])

            if op_type == "line":
                new_text = op.get("new", "")
                from services.style_runs import clean_marks
                marks = clean_marks(op.get("marks"), len(new_text))
                blocks = s.blocks.get(page_num) or blocks_for(s, page_num)
                target = find_target_block(blocks, op)
                if target is None:
                    raise LookupError("That text moved or changed. Reload the page and try again.")

                # 1) minimal-diff edit: unchanged glyphs, borders and underlines are never touched
                rep = None if marks else try_diff_edit(doc, page_num, target, new_text, blocks, s.cache, ghost)
                if rep is not None:
                    changed(s)
                    return state(s, missing=rep["substituted"], note="Edit applied")

                # 2) fallback: re-typeset the paragraph
                inplace = bool(marks) and new_text == target.text     # style change only: keep every line break
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
                if inplace:
                    from services.obj_reflow import ReflowedLine, ReflowResult
                    nl = len(target.lines)
                    reflow = ReflowResult(
                        lines=[ReflowedLine(text=ln.text, baseline_origin=(ln.bbox[0], ln.baseline_y), bbox=tuple(ln.bbox),
                                            width=ln.bbox[2] - ln.bbox[0], is_last_in_para=(k == nl - 1),
                                            target_width=ln.bbox[2] - ln.bbox[0]) for k, ln in enumerate(target.lines)],
                        total_bbox=tuple(target.bbox), height_delta=0.0)
                elif (len(target.lines) == 1
                        and "\n" not in new_text
                        and _measure(new_text) <= max(max_single_w, target.bbox[2] - target.bbox[0]) + 15.0):
                    reflow = TextReflowEngine.single_line(new_text, target.bbox, first_baseline)
                else:
                    orig_baselines = [ln.baseline_y for ln in target.lines]
                    orig_text_lefts = [ln.bbox[0] for ln in target.lines]   # true left edge (bullet included)
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
                        markers=(find_markers(target) if target.alignment in ("left", "justify") else []),
                        right_limit_x=(visible_right_edge(target) if target.alignment == "justify" else None),
                        char_widths=_char_widths(page, doc, target, new_text, w_scale, s.cache),
                    )

                log.warning("REFLOW ends=%s markers=%s", [(ln.text.split() or [""])[-1] for ln in reflow.lines], find_markers(target))
                report = PDFCompiler(doc, cache=s.cache).apply_edit_and_cascade(
                    page_num=page_num, target_block=target, reflow_res=reflow,
                    all_blocks=blocks, new_text=new_text, ghost=ghost, marks=marks)
                changed(s)
                return state(s, missing=report["substituted"], note="Edit applied")

            if op_type == "add":
                text = str(op.get("text", ""))
                if not text.strip() or len(text) > 4000:
                    raise ValueError("Type some text first.")
                fam, fam_flags = SERIF_ETC.get(op.get("family"), SERIF_ETC["sans"])
                flags = fam_flags | (16 if op.get("bold") else 0) | (2 if op.get("italic") else 0)
                size = min(200.0, max(4.0, float(op.get("size", 12))))
                c = int(op.get("color", 0))
                rgb = ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
                missing = set()
                for k, line in enumerate(text.split("\n")):                 # (x, y) is the baseline of line 1
                    if not line.strip():
                        continue
                    pt = fitz.Point(float(op["x"]), float(op["y"]) + k * size * 1.25) * page.derotation_matrix
                    res = draw_text_with_glyph_fallback(page, doc, (pt.x, pt.y), line, fam, flags, size, rgb, cache=s.cache)
                    if isinstance(res, (set, str)):
                        missing.update(res)
                changed(s)
                return state(s, missing="".join(sorted(missing)) if missing else "", note="")

            r = fitz.Rect(op["rect"]) * page.derotation_matrix
            r.normalize()
            if op_type == "highlight":
                if r.width >= 1 and r.height >= 1:
                    c = int(op.get("color", 0xFFE600))
                    a = page.add_highlight_annot(quads=[r.quad])
                    a.set_colors(stroke=((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255))
                    a.update()
                changed(s)
                return state(s, missing="", note="")

            if r.width >= 1 and r.height >= 1:
                safe_erase(page, r, fill_bg=True, images=2, ghost=ghost, cache=s.cache)
            changed(s)
            return state(s, missing="", note="")

        except HTTPException:
            restore_snapshot(s)
            raise
        except (LookupError, ValueError) as e:       # problems the user can act on
            restore_snapshot(s)
            error_stack = traceback.format_exc()
            print(error_stack)
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            log.exception("edit failed (type=%s)", op_type)
            restore_snapshot(s)
            raise HTTPException(status_code=500, detail="That change couldn't be applied to this part of the PDF. "
                                                        "Your document is unchanged.")


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


def _remap_pages(s: Session, order: List[int]):
    """order[new_index] = old_index. Hidden-glyph bookkeeping is keyed by page index, so it moves too."""
    s.ghost = {new: copy.deepcopy(s.ghost[old]) for new, old in enumerate(order) if old in s.ghost}


def _pages_op(s: Session, op: dict) -> dict:
    with s.lock, sessions.HEAVY:
        doc, n = s.doc, len(s.doc)
        kind = op.get("op")
        try:
            i = int(op.get("page"))
        except (TypeError, ValueError):
            _bad("Invalid page number")
        if not 0 <= i < n:
            _bad("Invalid page number")
        if kind == "delete" and n == 1:
            _bad("A PDF needs at least one page.")
        take_snapshot(s)
        try:
            if kind == "rotate":
                deg = 90 if int(op.get("deg", 90)) % 360 == 90 else (270 if int(op.get("deg", 90)) % 360 == 270 else 180)
                doc[i].set_rotation((doc[i].rotation + deg) % 360)
            elif kind == "delete":
                doc.delete_page(i)
                _remap_pages(s, [k for k in range(n) if k != i])
            elif kind == "duplicate":
                doc.fullcopy_page(i, i + 1)
                order = list(range(n))
                order.insert(i + 1, i)
                _remap_pages(s, order)
            elif kind == "move":
                to = max(0, min(n - 1, int(op.get("to"))))
                if to != i:
                    doc.move_page(i, to + 1 if to > i and to + 1 < n else (-1 if to > i else to))
                    order = list(range(n))
                    order.insert(to, order.pop(i))
                    _remap_pages(s, order)
            else:
                _bad("Unknown page action")
            changed(s)
            return state(s, pages=page_meta(doc))
        except HTTPException:
            restore_snapshot(s)
            raise
        except Exception:
            log.exception("page op failed (%s)", kind)
            restore_snapshot(s)
            raise HTTPException(status_code=500, detail="Couldn't change the pages. Your document is unchanged.")


@router.post("/pages")
async def pages_op(request: Request):
    s = need(request)
    try:
        op = await request.json()
        assert isinstance(op, dict)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad request.")
    return await run_in_threadpool(_pages_op, s, op)


def _add_image(s: Session, data: bytes, page_num: int, x: float, y: float, w: float) -> dict:
    try:
        pix = fitz.Pixmap(data)
        iw, ih = pix.width, pix.height
        assert iw > 0 and ih > 0
    except Exception:
        _bad("That doesn't look like a PNG or JPEG image.")
    with s.lock, sessions.HEAVY:
        if not 0 <= page_num < len(s.doc):
            _bad("Invalid page number")
        page = s.doc[page_num]
        w = min(max(w, 8.0), page.rect.width)
        h = w * ih / iw
        disp = fitz.Rect(x, y, x + w, y + h)
        take_snapshot(s)
        try:
            r = disp * page.derotation_matrix
            r.normalize()
            page.insert_image(r, stream=data, rotate=(-page.rotation) % 360, keep_proportion=False)
            changed(s)
            return state(s)
        except Exception:
            log.exception("image insert failed")
            restore_snapshot(s)
            raise HTTPException(status_code=500, detail="Couldn't place that image. Your document is unchanged.")


@router.post("/image")
async def add_image(request: Request, page: int, x: float, y: float, w: float = 150.0):
    s = need(request)
    data = await read_limited(request, MAX_IMAGE)
    return await run_in_threadpool(_add_image, s, data, page, x, y, w)


@router.post("/undo")
def undo_pdf(request: Request):
    s = need(request)
    with s.lock, sessions.HEAVY:
        if not s.undo:
            raise HTTPException(status_code=400, detail="Nothing to undo")
        s.redo.append((_snapshot_file(s), copy.deepcopy(s.ghost)))
        restore_snapshot(s)
        return state(s, pages=page_meta(s.doc))


@router.post("/redo")
def redo_pdf(request: Request):
    s = need(request)
    with s.lock, sessions.HEAVY:
        if not s.redo:
            raise HTTPException(status_code=400, detail="Nothing to redo")
        s.undo.append((_snapshot_file(s), copy.deepcopy(s.ghost)))
        path, ghost = s.redo.pop()
        _load(s, path, ghost)
        return state(s, pages=page_meta(s.doc))


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
                log.warning("font subsetting failed, saving without it", exc_info=True)
                data = None
        final = data or s.doc.tobytes(garbage=3, deflate=True)
        fname = f"{s.name}-edited.pdf"
        ascii_name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", fname)
        return Response(content=final, media_type="application/pdf", headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}"})
