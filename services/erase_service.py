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
    """items minus ONE item per matching ghost entry ([char, x, y]). Counting matters: when new
    text is drawn exactly over hidden old text both exist in the file; the hidden copy must go
    and the new one must stay."""
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
    """Dry-run the redaction on a throw-away one-page copy and check that no glyph outside
    r moved. MuPDF rewrites the page's content stream when it redacts, and on some PDFs that
    shifts unrelated letters (first letters / kerned pairs squashed together). Trying it on a
    copy means the real page is never damaged, so nothing has to be rolled back."""
    tmp = fitz.open()
    try:
        tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        tp = tmp[0]
        _redact(tp, r, fill, images)
        return not drift(before, char_sig(tp), r + (-1.5, -1.5, 1.5, 1.5))
    except Exception:
        return False          # when in doubt, do not rewrite the stream
    finally:
        tmp.close()


def safe_erase(page: fitz.Page, rect: fitz.Rect, fill_bg: bool = False, images: int = 0, ghost: list = None):
    """
    Removes the content inside `rect`.
      1. dry-run the real redaction on a copy; if nothing else moves, apply it to the page;
      2. otherwise hide the area with a clip + background fill, which leaves every other byte
         of the page alone. The hidden glyphs are appended to `ghost` as [char, x, y] so the
         editor can ignore them instead of showing old and new text together.
    """
    r = fitz.Rect(rect)
    before = char_sig(page)
    bg = sample_bg(page, r) if fill_bg else None

    if _redaction_is_safe(page, r, bg, images, before):
        _redact(page, r, bg, images)
        return "redacted"

    hide_rect(page, r, bg or sample_bg(page, r))
    if ghost is not None:
        fresh = unghost(before, ghost, lambda t: (t[0], t[1], t[2]))     # not already hidden
        ghost.extend([c, x, y] for c, x, y, bb in fresh
                     if r.contains(fitz.Point((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)))
    return "clipped"

def erase_underline_rect(page: fitz.Page, rect: fitz.Rect):
    """Erases a thin vector underline rectangle cleanly without shifting any text."""
    r = fitz.Rect(rect) + (-0.5, -0.5, 0.5, 0.5)
    bg = sample_bg(page, r, pad=1.5)
    page.draw_rect(r, color=None, fill=bg, overlay=True)
