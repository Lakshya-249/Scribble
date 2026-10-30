import collections
import logging
import pymupdf as fitz

log = logging.getLogger("scribble.erase")

DRIFT_TOL = 0.75


def char_sig(page: fitz.Page):
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                for c in s["chars"]:
                    if c["c"].strip():
                        out.append((c["c"], c["origin"][0], c["origin"][1], tuple(c["bbox"])))
    return out


def drift(before, after, skip):
    """Glyphs of `before` that are missing from `after` (or moved), ignoring anything inside `skip`
    (one Rect or a list of Rects: those glyphs are meant to disappear)."""
    skips = [skip] if isinstance(skip, fitz.Rect) else list(skip)
    grid = collections.defaultdict(list)
    for c, x, y, _ in after:
        grid[(c, int(x // 3), int(y // 3))].append((x, y))
    bad = []
    for c, x, y, bb in before:
        if any(fitz.Rect(bb).intersects(k) for k in skips):
            continue
        gx, gy, ok = int(x // 3), int(y // 3), False
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for x2, y2 in grid.get((c, gx + a, gy + b), ()):
                    if abs(x - x2) < DRIFT_TOL and abs(y - y2) < DRIFT_TOL:
                        ok = True
                        break
                if ok:
                    break
            if ok:
                break
        if not ok:
            bad.append((c, round(x, 1), round(y, 1)))
    return bad


def unghost(items, ghost, key):
    if not ghost:
        return list(items)
    idx = collections.defaultdict(list)
    for c, x, y in ghost:
        idx[(c, int(x // 3), int(y // 3))].append((x, y))
    out = []
    for it in items:
        c, x, y = key(it)
        hit = None
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                lst = idx.get((c, int(x // 3) + a, int(y // 3) + b))
                for k, (gx, gy) in enumerate(lst or ()):
                    if abs(gx - x) < 0.6 and abs(gy - y) < 0.6:
                        hit = (lst, k)
                        break
                if hit:
                    break
            if hit:
                break
        if hit:
            hit[0].pop(hit[1])
        else:
            out.append(it)
    return out


def sample_bg(page: fitz.Page, r: fitz.Rect, pad: float = 2.0):
    try:
        view = fitz.Rect(r) * page.rotation_matrix
        view.normalize()
        big = (view + (-pad, -pad, pad, pad)) & page.rect
        pix = page.get_pixmap(clip=big, dpi=144, alpha=False)
        t = max(1, int(pad * 2 * 0.75))
        cnt = collections.Counter()
        w, h = pix.width, pix.height
        for y in range(h):
            xs = range(w) if (y < t or y >= h - t) else list(range(t)) + list(range(w - t, w))
            for x in xs:
                cnt[pix.pixel(x, y)[:3]] += 1
        return tuple(v / 255 for v in cnt.most_common(1)[0][0])
    except Exception:
        return (1, 1, 1)


def hide_rect(page: fitz.Page, r: fitz.Rect, bg):
    doc = page.parent
    pr = fitz.Rect(r) * ~page.transformation_matrix
    pr.normalize()
    page.wrap_contents()
    first = page.get_contents()[0]
    clip = (
        "-100000 -100000 200000 200000 re "
        f"{pr.x0:.4f} {pr.y0:.4f} {pr.width:.4f} {pr.height:.4f} re W* n\n"
    )
    doc.update_stream(first, (doc.xref_stream(first) or b"") + clip.encode())
    page.draw_rect(fitz.Rect(r), color=None, fill=bg, overlay=True)


def _redact(page: fitz.Page, r: fitz.Rect, fill, images: int):
    _redact_many(page, [(r, fill)], images)


def _redact_many(page: fitz.Page, items, images: int):
    for rect, fill in items:
        page.add_redact_annot(rect, fill=fill or False)
    try:
        page.apply_redactions(images=images, graphics=0)
    except TypeError:
        page.apply_redactions(images=images)


def _drift_after(page: fitz.Page, items, images: int, before, skip, expect_removed: int = None) -> list:
    """Run the redaction on a throw-away copy of the page; return the glyphs it would move.
    None means the simulation itself failed (treat as unsafe). With `expect_removed`, the glyph
    count must drop by exactly that many (proves nothing we meant to delete survived)."""
    tmp = fitz.open()
    try:
        tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        _redact_many(tmp[0], items, images)
        after = char_sig(tmp[0])
        if expect_removed is not None and len(before) - len(after) != expect_removed:
            return None
        return drift(before, after, skip)
    except Exception:
        return None
    finally:
        tmp.close()


def _redaction_is_safe(page: fitz.Page, r: fitz.Rect, fill, images: int, before) -> bool:
    return _drift_after(page, [(r, fill)], images, before, r + (-1.5, -1.5, 1.5, 1.5)) == []


def _glyph_records(page: fitz.Page):
    """Every visible glyph with the style it was drawn in (horizontal text only)."""
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            dx, dy = ln.get("dir", (1.0, 0.0))
            horizontal = abs(dy) < 0.01 and dx > 0.99
            for si, sp in enumerate(ln["spans"]):
                for ci, c in enumerate(sp["chars"]):
                    out.append({"c": c["c"], "x": c["origin"][0], "y": c["origin"][1], "bbox": tuple(c["bbox"]),
                                "font": sp["font"], "flags": int(sp["flags"]), "size": float(sp["size"]),
                                "color": int(sp["color"] or 0), "ok": horizontal, "span": id(sp), "k": ci})
    return out


def _reanchor(page: fitz.Page, glyphs: list, cache: dict):
    """Draw glyphs again where they were, one word at a time so the text layer stays readable."""
    from services.compiler import layout_run, draw_pieces      # lazy: compiler imports this module
    from services.style_runs import Style, Run
    glyphs = sorted(glyphs, key=lambda g: (g["span"], g["k"]))
    word, missing = [], set()

    def flush():
        if not word:
            return
        g0 = word[0]
        st = Style(g0["font"], g0["flags"], round(g0["size"], 3), g0["color"], False)
        pieces, fb = layout_run(page, page.parent, Run("".join(g["c"] for g in word), st), cache)
        for pc in pieces:
            if pc.substituted:
                missing.update(ch for ch in pc.text if not ch.isspace())
        draw_pieces(page, g0["x"], g0["y"], pieces, st, fb, cache)
        word.clear()

    for g in glyphs:
        if not g["c"].strip():
            flush()
            continue
        if word:
            w = word[-1]
            same = (g["span"] == w["span"] and g["k"] == w["k"] + 1 and abs(g["y"] - w["y"]) < 0.3
                    and (g["font"], g["flags"], g["size"], g["color"]) == (w["font"], w["flags"], w["size"], w["color"]))
            if not same:
                flush()
        word.append(g)
    flush()
    return missing


def erase_underline_rect(page: fitz.Page, rect: tuple):
    """Cleanly erases old vector underlines by expanding the erase clip safely."""
    r = fitz.Rect(rect) + (-1.5, -2.0, 1.5, 2.0)
    bg = sample_bg(page, r, pad=2.0)
    page.draw_rect(r, color=None, fill=bg, overlay=True)


def safe_erase(page: fitz.Page, rect: fitz.Rect, fill_bg: bool = False, images: int = 0, ghost: list = None,
               cache: dict = None):
    """Remove whatever is inside `rect`.

    1. Plain redaction when it leaves every other glyph where it was.
    2. Otherwise (MuPDF closes the gap after a deleted glyph, shifting the rest of that text string)
       redact the shifted glyphs as well and draw them again at their original positions. The old text is
       really gone from the file.
    3. Last resort: clip the old glyphs away. They stay in the file, so they are remembered in `ghost`.
    Returns "redacted", "reanchored" or "clipped".
    """
    r = fitz.Rect(rect)
    before = char_sig(page)
    bg = sample_bg(page, r) if fill_bg else None
    pad = r + (-1.5, -1.5, 1.5, 1.5)

    bad = _drift_after(page, [(r, bg)], images, before, pad)
    if bad == []:
        _redact(page, r, bg, images)
        return "redacted"

    if bad and cache is not None:
        try:
            recs = _glyph_records(page)
            idx = collections.defaultdict(list)
            for g in recs:
                idx[(g["c"], int(g["x"] // 3), int(g["y"] // 3))].append(g)
            moved = []
            for c, x, y in bad:
                hit = next((g for a in (-1, 0, 1) for b2 in (-1, 0, 1)
                            for g in idx.get((c, int(x // 3) + a, int(y // 3) + b2), ())
                            if abs(g["x"] - x) < 0.2 and abs(g["y"] - y) < 0.2), None)
                moved.append(hit)
            if moved and all(g is not None and g["ok"] for g in moved):
                # MuPDF tests later glyphs at their already shifted position, so one box per line must
                # reach from the first deleted glyph to the last shifted one: shifted glyphs stay inside it.
                bands = []
                for g in sorted(moved, key=lambda g: (g["y"], g["x"])):
                    gb = fitz.Rect(g["bbox"])
                    for bd in bands:
                        if abs(bd["y"] - g["y"]) < 2.0:
                            bd["rect"] |= gb
                            break
                    else:
                        bands.append({"y": g["y"], "rect": gb})
                boxes = []
                for bd in bands:
                    rr = bd["rect"]
                    if r.y0 < rr.y1 and r.y1 > rr.y0:
                        rr = rr | r
                    boxes.append(rr + (0.2, 0.2, -0.2, -0.2))
                def _in_r(g):
                    return r.contains(fitz.Point((g["bbox"][0] + g["bbox"][2]) / 2, (g["bbox"][1] + g["bbox"][3]) / 2))

                def _in_box(g):
                    return any(fitz.Rect(g["bbox"]).intersects(bx) for bx in boxes)

                vis = [g for g in recs if g["c"].strip()]
                gone = [g for g in vis if _in_r(g) or _in_box(g)]
                redraw = [g for g in gone if not _in_r(g)]
                if not all(g["ok"] for g in redraw):
                    raise ValueError("rotated text")
                items = [(r, bg)] + [(bx, None) for bx in boxes]
                skips = [pad] + [bx + (-1, -1, 1, 1) for bx in boxes]
                if _drift_after(page, items, images, before, skips, expect_removed=len(gone)) == []:
                    _redact_many(page, items, images)
                    _reanchor(page, [g for g in recs if g in redraw or (not g["c"].strip() and _in_box(g) and not _in_r(g))], cache)
                    return "reanchored"
        except Exception:
            pass                                    # fall through to clipping

    hide_rect(page, r, bg or sample_bg(page, r))
    if ghost is not None:
        fresh = unghost(before, ghost, lambda t: (t[0], t[1], t[2]))
        ghost.extend([c, x, y] for c, x, y, bb in fresh
                     if r.contains(fitz.Point((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)))
    return "clipped"


def remove_line_art(page: fitz.Page, rect, pad: float = 0.3) -> bool:
    """Delete the vector path(s) lying entirely inside `rect` (an old underline) WITHOUT painting
    over anything: table borders and cell shading next to it survive, and text is left alone.
    Returns False if this PyMuPDF cannot do it, so the caller can fall back to painting."""
    r = fitz.Rect(rect) + (-pad, -pad, pad, pad)
    try:
        page.add_redact_annot(r, fill=False)
        page.apply_redactions(images=0, graphics=1, text=fitz.PDF_REDACT_TEXT_NONE)
        return True
    except (TypeError, AttributeError, ValueError):
        for a in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
            page.delete_annot(a)
        return False



def _hairline_present(page: fitz.Page, x0: float, x1: float, ym: float) -> bool:
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None:
            continue
        if (r.height <= 3.5 and r.width >= 4.0 and abs((r.y0 + r.y1) / 2 - ym) <= 1.0
                and r.x0 >= x0 - 1.0 and r.x1 <= x1 + 1.0 and r.width >= 0.8 * (x1 - x0)):
            return True
    return False


def _underline_paths(drawings: list, x0: float, x1: float, ym: float) -> list:
    """The vector path(s) that make up the underline [x0, x1] at height ym."""
    out = []
    for d in drawings:
        r = d.get("rect")
        if r is None or d.get("type") not in ("f", "s", "fs"):
            continue
        if (r.height <= 3.5 and r.width >= 2.0 and abs((r.y0 + r.y1) / 2 - ym) <= 1.5
                and r.x0 >= x0 - 1.0 and r.x1 <= x1 + 1.0 and r.width >= 0.8 * (x1 - x0)):
            out.append(d)
    return out


def remove_underlines(page: fitz.Page, rects: list) -> None:
    """Get rid of several old underlines in ONE go. Always remove them all BEFORE drawing any new one:
    the paint-over fallback of an old underline used to wipe the freshly drawn neighbour (the last
    letters of 'GitHub' lost their underline).

    1) delete the exact path, padded by its stroke width (a 1pt stroke is 0.5pt wider than its rect on
       every side, so the old fixed 0.3pt pad left square-capped ends behind as stray stubs);
    2) re-read the page; 3) only what survived is covered by a thin band (stroke tall, never a box)."""
    if not rects:
        return
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    todo = []                                    # (x0, x1, ym, [paths])
    for (x0, y0, x1, y1) in rects:
        ym = (y0 + y1) / 2.0
        todo.append((x0, x1, ym, _underline_paths(drawings, x0, x1, ym)))

    try:
        for x0, x1, ym, paths in todo:
            if paths:
                for d in paths:
                    pad = max(0.6, float(d.get("width") or 0.0) / 2.0 + 0.4)
                    page.add_redact_annot(fitz.Rect(d["rect"]) + (-pad, -pad, pad, pad), fill=False)
            else:
                page.add_redact_annot(fitz.Rect(x0 - 0.8, ym - 0.9, x1 + 0.8, ym + 0.9), fill=False)
        page.apply_redactions(images=0, graphics=1, text=fitz.PDF_REDACT_TEXT_NONE)
    except (TypeError, AttributeError, ValueError):
        for a in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
            page.delete_annot(a)

    try:
        left = page.get_drawings()
    except Exception:
        return
    for x0, x1, ym, paths in todo:
        for d in _underline_paths(left, x0, x1, ym):
            w = max(1.2, float(d.get("width") or 0.5) + 0.8)
            band = fitz.Rect(x0 - 0.5, ym - w / 2, x1 + 0.5, ym + w / 2)
            page.draw_rect(band, color=None, fill=sample_bg(page, band, pad=1.0), overlay=True)


def remove_underline(page: fitz.Page, rect) -> None:
    """Get rid of one old underline (see remove_underlines)."""
    remove_underlines(page, [rect])


def draw_underline(page: fitz.Page, rect, rgb_colour) -> None:
    """Draw an underline of the given extent. A zero-height rect is a hairline: it must be STROKED,
    a zero-area fill is invisible (this was the 'underline did not follow my edit' bug)."""
    x0, y0, x1, y1 = rect
    if y1 - y0 < 0.5:
        ym = (y0 + y1) / 2.0
        page.draw_line(fitz.Point(x0, ym), fitz.Point(x1, ym), color=rgb_colour, width=0.5)
    else:
        page.draw_rect(fitz.Rect(rect), color=None, fill=rgb_colour, overlay=True)


def collect_drawings_below(page: fitz.Page, y_top: float, x0: float, x1: float) -> list:
    """Vector paths (table borders, cell shading, rules, underlines) that sit below `y_top` and whose
    centre is inside [x0, x1]: they must follow the text when an edit makes the paragraph above grow."""
    out = []
    pr = page.rect
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        r = d.get("rect")
        if r is None or d.get("type") not in ("f", "s", "fs"):
            continue                                    # clip paths etc. cannot be moved safely
        if r.y0 < y_top:
            continue
        if r.height > 0.8 * pr.height and r.width > 0.8 * pr.width:
            continue                                    # page background
        if not (x0 - 2.0 <= (r.x0 + r.x1) / 2.0 <= x1 + 2.0):
            continue
        out.append(d)
    return out


def remove_drawings(page: fitz.Page, drawings: list) -> bool:
    """Delete these vector paths (and only vector paths: text and images are untouched, nothing is painted)."""
    if not drawings:
        return True
    try:
        for d in drawings:
            r = fitz.Rect(d["rect"])
            pad = max(0.5, float(d.get("width") or 0.0) / 2.0 + 0.25)
            page.add_redact_annot(r + (-pad, -pad, pad, pad), fill=False)
        page.apply_redactions(images=0, graphics=1, text=fitz.PDF_REDACT_TEXT_NONE)
        return True
    except (TypeError, AttributeError, ValueError):
        for a in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
            page.delete_annot(a)
        return False


def redraw_drawings(page: fitz.Page, drawings: list, dy: float) -> int:
    """Draw the same paths again, `dy` lower, with their own colour, width, dashes, fill and opacity."""
    n = 0
    for d in drawings:
        try:
            sh = page.new_shape()
            for it in d["items"]:
                k = it[0]
                if k == "l":
                    sh.draw_line(fitz.Point(it[1].x, it[1].y + dy), fitz.Point(it[2].x, it[2].y + dy))
                elif k == "re":
                    r = it[1]
                    sh.draw_rect(fitz.Rect(r.x0, r.y0 + dy, r.x1, r.y1 + dy))
                elif k == "qu":
                    q = it[1]
                    sh.draw_quad(fitz.Quad(*[fitz.Point(p.x, p.y + dy) for p in (q.ul, q.ur, q.ll, q.lr)]))
                elif k == "c":
                    sh.draw_bezier(*[fitz.Point(p.x, p.y + dy) for p in it[1:5]])
            cap = d.get("lineCap", 0)
            cap = cap[0] if isinstance(cap, (list, tuple)) and cap else (cap or 0)
            kw = dict(color=d.get("color"), fill=d.get("fill"), width=d.get("width") or 0,
                      dashes=d.get("dashes"), lineCap=int(cap), lineJoin=int(d.get("lineJoin") or 0),
                      closePath=bool(d.get("closePath", False)), even_odd=bool(d.get("even_odd", False)))
            try:
                sh.finish(fill_opacity=d.get("fill_opacity", 1), stroke_opacity=d.get("stroke_opacity", 1), **kw)
            except TypeError:
                sh.finish(**kw)
            sh.commit(overlay=True)
            n += 1
        except Exception as e:               # one odd path must never abort the edit
            log.debug("could not move a drawing: %s", e)
    return n


def _same_path(a: dict, b: dict) -> bool:
    ra, rb = a["rect"], b["rect"]
    return (a.get("type") == b.get("type") and abs(ra.x0 - rb.x0) < 0.6 and abs(ra.x1 - rb.x1) < 0.6
            and abs(ra.y0 - rb.y0) < 0.6 and abs(ra.y1 - rb.y1) < 0.6)


def paint_out_drawing(page: fitz.Page, d: dict) -> None:
    """Last resort for a path the redaction could not delete: cover just its strokes with the local
    background (thin, never a box), so nothing but the line itself is touched."""
    bg = sample_bg(page, fitz.Rect(d["rect"]) + (-1, -1, 1, 1), pad=3.0)
    w = float(d.get("width") or 0.5) + 1.2
    stroked = d.get("color") is not None and d.get("type") in ("s", "fs")
    for it in d["items"]:
        if it[0] == "l" and stroked:
            page.draw_line(it[1], it[2], color=bg, width=w, overlay=True)
        elif it[0] == "re":
            if d.get("fill") is not None and d.get("type") in ("f", "fs"):
                page.draw_rect(it[1], color=None, fill=bg, overlay=True)
            if stroked:
                page.draw_rect(it[1], color=bg, fill=None, width=w, overlay=True)


def remove_drawings_verified(page: fitz.Page, drawings: list) -> bool:
    """remove_drawings(), then re-read the page: any path that is STILL there is painted out. Without
    this, a table's old top border / first-row lines stayed behind next to the moved copy."""
    if not remove_drawings(page, drawings):
        return False
    try:
        left = [d for d in drawings if any(_same_path(d, n) for n in page.get_drawings())]
    except Exception:
        return True
    for d in left:
        try:
            paint_out_drawing(page, d)
        except Exception as e:
            log.debug("could not paint out a drawing: %s", e)
    log.debug("moved %d drawings, %d needed painting out", len(drawings), len(left))
    return True
