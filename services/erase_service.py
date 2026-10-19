import collections
import pymupdf as fitz

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


def drift(before, after, skip: fitz.Rect):
    grid = collections.defaultdict(list)
    for c, x, y, _ in after:
        grid[(c, int(x // 3), int(y // 3))].append((x, y))
    bad = []
    for c, x, y, bb in before:
        if fitz.Rect(bb).intersects(skip):
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
    page.add_redact_annot(r, fill=fill or False)
    try:
        page.apply_redactions(images=images, graphics=0)
    except TypeError:
        page.apply_redactions(images=images)


def _redaction_is_safe(page: fitz.Page, r: fitz.Rect, fill, images: int, before) -> bool:
    tmp = fitz.open()
    try:
        tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        tp = tmp[0]
        _redact(tp, r, fill, images)
        return not drift(before, char_sig(tp), r + (-1.5, -1.5, 1.5, 1.5))
    except Exception:
        return False
    finally:
        tmp.close()


def erase_underline_rect(page: fitz.Page, rect: tuple):
    """Cleanly erases old vector underlines by expanding the erase clip safely."""
    r = fitz.Rect(rect) + (-1.5, -2.0, 1.5, 2.0)
    bg = sample_bg(page, r, pad=2.0)
    page.draw_rect(r, color=None, fill=bg, overlay=True)


def safe_erase(page: fitz.Page, rect: fitz.Rect, fill_bg: bool = False, images: int = 0, ghost: list = None):
    r = fitz.Rect(rect)
    before = char_sig(page)
    bg = sample_bg(page, r) if fill_bg else None

    if _redaction_is_safe(page, r, bg, images, before):
        _redact(page, r, bg, images)
        return "redacted"

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


def remove_underline(page: fitz.Page, rect) -> None:
    """Get rid of one old underline. Works for hairline strokes too (their rect has height 0).
    1) delete exactly that path; 2) check it is really gone; 3) only if not, paint a band that is
    just ~1pt tall over it (never the +-2pt box the old code used, which swallowed table borders)."""
    x0, y0, x1, y1 = rect
    ym = (y0 + y1) / 2.0
    band = fitz.Rect(x0, ym - 0.6, x1, ym + 0.6)
    remove_line_art(page, band)
    if _hairline_present(page, x0, x1, ym):
        thin = fitz.Rect(x0, ym - 0.45, x1, ym + 0.45)
        page.draw_rect(thin, color=None, fill=sample_bg(page, thin, pad=1.0), overlay=True)


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
            print(f"[edit] could not move a drawing: {e}", flush=True)
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
            print(f"[edit] could not paint out a drawing: {e}", flush=True)
    print(f"[edit] moved {len(drawings)} drawings, {len(left)} needed painting out", flush=True)
    return True
