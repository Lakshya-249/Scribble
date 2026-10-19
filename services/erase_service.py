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
