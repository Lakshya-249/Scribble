import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any, Optional

BULLET_CHARS = set("\u2022\u2023\u25e6\u2043\u2219\u25cf\u25aa\u25ab\u25b8\u2013\u2014-*>\u00b7")


def _starts_with_bullet(text: str) -> bool:
    s = text.lstrip()
    if not s:
        return False
    if s[0] in BULLET_CHARS:
        return True
    head = s.split(" ", 1)[0]
    if 1 < len(head) <= 4 and head.rstrip(".)").lstrip("(").isalnum() and any(c in head for c in ".)"):
        return True
    return False


def _detect_hanging_indent(line_text: str, tokens: list) -> float:
    """If a line starts with a bullet ('• Text'), compute the X offset from the bullet's
    left edge to the start of the first word after the bullet so wrapped lines align under 'Text'."""
    if not tokens or not _starts_with_bullet(line_text):
        return 0.0
    x0 = tokens[0].bbox[0]
    saw_bullet = False
    saw_gap = False
    for i, t in enumerate(tokens):
        if not t.char.isspace() and not saw_bullet:
            saw_bullet = True
            continue
        if saw_bullet:
            if t.char.isspace():
                saw_gap = True
                continue
            prev = tokens[i - 1]
            if saw_gap or (t.bbox[0] - prev.bbox[2]) > prev.font_size * 0.12:
                indent = t.bbox[0] - x0
                if 4.0 <= indent <= 36.0:
                    return float(indent)
                break
    return 0.0


@dataclass
class CharToken:
    char: str
    bbox: Tuple[float, float, float, float]
    origin: Tuple[float, float]
    font_name: str
    font_size: float
    flags: int
    color: int = 0
    underline: bool = False
    seq_id: int = 0


@dataclass
class TextLine:
    tokens: List[CharToken] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    baseline_y: float = 0.0
    dominant_font_size: float = 0.0
    text: str = ""
    first_word_w: float = 0.0
    text_x0: float = 0.0  # True starting X of text (excluding bullet symbol)
    underline_rects: List[Tuple[float, float, float, float]] = field(default_factory=list)
    underline_colors: List[int] = field(default_factory=list)   # parallel to underline_rects
    char_tok: List[Optional["CharToken"]] = field(default_factory=list)  # parallel to .text (None = inferred space)

    def _word_gap_em(self) -> float:
        gaps = []
        adv_x2 = None
        for t in self.tokens:
            if adv_x2 is not None and t.char.isalnum() and t.font_size > 0:
                w = t.bbox[2] - t.bbox[0]
                if w > 0.5:
                    gaps.append((t.bbox[0] - adv_x2) / t.font_size)
            if t.char.isalnum():
                adv_x2 = max(adv_x2 or t.bbox[2], t.bbox[2])
            else:
                adv_x2 = None
        gaps.sort()
        base = gaps[int(len(gaps) * 0.3)] if len(gaps) >= 3 else 0.0
        base = max(-0.15, min(0.25, base))
        return max(0.055, base + 0.05)

    def _drop_tracking_spaces(self):
        toks = self.tokens
        letters = sum(1 for t in toks if not t.char.isspace())
        spaces = [t for t in toks if t.char == " "]
        if letters < 6 or len(spaces) < 0.7 * (letters - 1):
            return
        widths = sorted(t.bbox[2] - t.bbox[0] for t in spaces)
        median, widest = widths[len(widths) // 2], widths[-1]
        if widest < 1.8 * max(median, 0.01):
            return
        cut = (median + widest) / 2.0
        self.tokens = [t for t in toks if not (t.char == " " and (t.bbox[2] - t.bbox[0]) < cut)]

    def finalize(self):
        if not self.tokens:
            return
        # Sort by rounded origin X (1.5pt buckets) and then native stream seq_id!
        # This prevents expanded ligatures ('fi', 'fl') with identical/overlapping X from scrambling into 'defni ing'
        self.tokens.sort(key=lambda t: (round(t.origin[0] / 1.5), t.seq_id))
        self._drop_tracking_spaces()

        x0 = min(t.bbox[0] for t in self.tokens)
        y0 = min(t.bbox[1] for t in self.tokens)
        x1 = max(t.bbox[2] for t in self.tokens)
        y1 = max(t.bbox[3] for t in self.tokens)
        for ux0, uy0, ux1, uy1 in self.underline_rects:
            y1 = max(y1, uy1 + 0.5)
#         self.bbox = (x0, y0, x1, y1)
#
#         self.baseline_y = float(np.median([t.origin[1] for t in self.tokens]))
#         self.dominant_font_size = float(np.median([t.font_size for t in self.tokens]))
        self.bbox = (x0, y0, x1, y1)
        self.baseline_y = float(np.median([t.origin[1] for t in self.tokens]))
        self.dominant_font_size = float(np.median([t.font_size for t in self.tokens]))

        # Find true text start X (skip leading bullet/marker tokens)
        first_text_tok = next((t for t in self.tokens if not t.char.isspace() and t.char not in BULLET_CHARS), self.tokens[0])
        self.text_x0 = float(first_text_tok.bbox[0])

        thr = self._word_gap_em()
        chars_out = []
        tok_out = []
        first_end = None
        advancing_x2 = self.tokens[0].bbox[2]

        for i, t in enumerate(self.tokens):
            if i > 0:
                prev_t = self.tokens[i - 1]
                prev_w = prev_t.bbox[2] - prev_t.bbox[0]
                # Guard against 0-width expanded ligature halves ('fi', 'fl') creating false word spaces
                effective_prev_x2 = max(
                    advancing_x2,
                    prev_t.origin[0] + (prev_t.font_size * 0.25 if prev_w < 0.6 else prev_w),
                )
                dx = t.bbox[0] - effective_prev_x2
                size = max(prev_t.font_size, 1.0)
                same_ligature_origin = abs(t.origin[0] - prev_t.origin[0]) < 0.8

                if (not same_ligature_origin
                        and dx > thr * size
                        and not prev_t.char.isspace()
                        and not t.char.isspace()
                        and abs(t.origin[1] - prev_t.origin[1]) < size * 0.5):
                    chars_out.append(" ")
                    tok_out.append(None)
                    if first_end is None:
                        first_end = effective_prev_x2
                elif t.char.isspace() and first_end is None:
                    first_end = effective_prev_x2
            chars_out.append(t.char)
            tok_out.extend([t] * len(t.char))
            advancing_x2 = max(advancing_x2, t.bbox[2])

        if first_end is None:
            first_end = advancing_x2
        self.first_word_w = max(0.0, first_end - self.tokens[0].bbox[0])
        self.text = "".join(chars_out)
        self.char_tok = tok_out if len(tok_out) == len(self.text) else []


@dataclass
class ParagraphBlock:
    lines: List[TextLine] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    text: str = ""
    measured_line_height: Optional[float] = None
    alignment: str = "left"
    first_line_indent: float = 0.0
    hanging_indent: float = 0.0
    container_max_x1: Optional[float] = None
    underline_rects: List[Tuple[float, float, float, float]] = field(default_factory=list)
    char_tok: Optional[list] = None    # parallel to .text: CharToken, or None for inferred spaces / separators
    char_line: Optional[list] = None   # parallel to .text: index into .lines, or -1 for separators

    def finalize(self):
        if not self.lines:
            return
        self.lines.sort(key=lambda ln: ln.baseline_y)

        x0 = min(ln.bbox[0] for ln in self.lines)
        y0 = min(ln.bbox[1] for ln in self.lines)
        x1 = max(ln.bbox[2] for ln in self.lines)
        y1 = max(ln.bbox[3] for ln in self.lines)
        self.underline_rects = [r for ln in self.lines for r in ln.underline_rects]
        for ux0, uy0, ux1, uy1 in self.underline_rects:
            y1 = max(y1, uy1 + 0.5)
        self.bbox = (x0, y0, x1, y1)

        # 1. Detect hanging indent for bulleted/numbered items
        if self.lines:
            self.hanging_indent = _detect_hanging_indent(self.lines[0].text, self.lines[0].tokens)
            if len(self.lines) >= 2 and self.hanging_indent == 0.0:
                diff_indent = self.lines[1].bbox[0] - self.lines[0].bbox[0]
                if 4.0 <= diff_indent <= 36.0 and _starts_with_bullet(self.lines[0].text):
                    self.hanging_indent = float(diff_indent)

        # 2. Measure true baseline-to-baseline leading
        if len(self.lines) >= 2:
            diffs = [
                self.lines[i + 1].baseline_y - self.lines[i].baseline_y
                for i in range(len(self.lines) - 1)
                if (self.lines[i + 1].baseline_y - self.lines[i].baseline_y) > 2.0
            ]
            if diffs:
                self.measured_line_height = float(np.median(diffs))

        # 3. Detect paragraph alignment & first-line indent
        if len(self.lines) >= 2:
            non_last = self.lines[:-1]
            lefts = [ln.bbox[0] for ln in self.lines]
            rights = [ln.bbox[2] for ln in non_last]
            centers = [(ln.bbox[0] + ln.bbox[2]) * 0.5 for ln in self.lines]

            body_left = float(np.median(lefts[1:])) if len(lefts) > 1 else lefts[0]
            if (self.hanging_indent == 0.0
                    and abs(lefts[0] - body_left) > 4.0
                    and all(abs(l - body_left) <= 2.0 for l in lefts[1:])):
                self.first_line_indent = lefts[0] - body_left

            right_spread = max(rights) - min(rights) if rights else 999.0
            left_spread = max(lefts[1:]) - min(lefts[1:]) if len(lefts) > 1 else 0.0
            center_spread = max(centers) - min(centers)

            # one non-last line always has right_spread == 0, so 2-line paragraphs were all called 'justify'
            if len(rights) >= 2 and right_spread <= 3.0 and left_spread <= 3.0 and (x1 - x0) > 120.0:
                self.alignment = "justify"
            elif center_spread <= 3.0 and left_spread > 4.0:
                self.alignment = "center"
            elif right_spread <= 2.5 and left_spread > 4.0:
                self.alignment = "right"
            else:
                self.alignment = "left"

        # 4. Reconstruct soft-wrapped lines vs. explicit line breaks
        right = x1
        firsts = [ln.text.split(" ")[0] for ln in self.lines]
        top = max((firsts.count(w) for w in set(firsts)), default=0)
        bullet = sum(1 for w in firsts if _starts_with_bullet(w))
        list_like = len(self.lines) >= 2 and (top >= max(2, 0.6 * len(self.lines)) or bullet >= 2)
        flowing = (not list_like and len(self.lines) >= 2
                   and (right - self.lines[-1].bbox[2]) > -2.0)
        parts, ct, cl = [], [], []
        for k, ln in enumerate(self.lines):
            parts.append(ln.text)
            ct.extend(ln.char_tok if ln.char_tok else [None] * len(ln.text))
            cl.extend([k] * len(ln.text))
            if k + 1 < len(self.lines):
                nxt = self.lines[k + 1]
                need = nxt.first_word_w + 0.25 * ln.dominant_font_size
                if flowing and (right - ln.bbox[2]) < need + 4.0:
                    hyphen = ln.text.endswith("-") and nxt.text[:1].islower()
                    sep = "" if (hyphen or ln.text.endswith(" ")) else " "
                else:
                    sep = "\n"
                parts.append(sep)
                ct.extend([None] * len(sep))
                cl.extend([-1] * len(sep))
        self.text = "".join(parts)
        ok = all(ln.char_tok for ln in self.lines) and len(ct) == len(self.text)
        self.char_tok, self.char_line = (ct, cl) if ok else (None, None)


def _has_v_barrier(x_left: float, x_right: float, y_top: float, y_bot: float,
                   v_barriers: List[Tuple[float, float, float, float]]) -> bool:
    if x_right < x_left:
        x_left, x_right = x_right, x_left
    for vx0, vy0, vx1, vy1 in v_barriers:
        vx_mid = (vx0 + vx1) * 0.5
        if (x_left - 1.0) <= vx_mid <= (x_right + 1.0):
            overlap_y = min(y_bot, vy1) - max(y_top, vy0)
            if overlap_y >= 0.35 * max(1.0, y_bot - y_top):
                return True
    return False


def _has_h_barrier(y_top_line: float, y_bot_line: float, x_left: float, x_right: float,
                   h_barriers: List[Tuple[float, float, float, float]]) -> bool:
    for hx0, hy0, hx1, hy1, *_ in h_barriers:
        hy_mid = (hy0 + hy1) * 0.5
        if (y_top_line - 1.5) <= hy_mid <= (y_bot_line + 1.5):
            overlap_x = min(x_right, hx1) - max(x_left, hx0)
            if overlap_x >= 0.25 * max(1.0, min(x_right - x_left, hx1 - hx0)):
                return True
    return False


class SpatialClusterer:
    def __init__(
        self,
        line_y_tolerance_ratio: float = 0.35,
        para_lead_ratio: float = 1.65,
        max_char_h_gap_ratio: float = 1.65,
    ):
        self.line_y_tolerance_ratio = line_y_tolerance_ratio
        self.para_lead_ratio = para_lead_ratio
        self.max_char_h_gap_ratio = max_char_h_gap_ratio

    def cluster_characters_into_lines(
        self,
        tokens: List[CharToken],
        v_barriers: List[Tuple[float, float, float, float]] = (),
    ) -> List[TextLine]:
        if not tokens:
            return []

        # Sort by rounded baseline Y, then rounded origin X, then native stream sequence ID
        tokens.sort(key=lambda t: (round(t.origin[1] / 2.0), round(t.origin[0] / 1.5), t.seq_id))
        lines: List[TextLine] = []

        for token in tokens:
            matched = False
            for line in reversed(lines[-12:] if len(lines) >= 12 else lines):
                allowed_y_jitter = line.dominant_font_size * self.line_y_tolerance_ratio
                same_row = abs(token.origin[1] - line.baseline_y) <= allowed_y_jitter

                if same_row:
                    last_tok = max(line.tokens, key=lambda t: t.bbox[2])
                    gap = token.bbox[0] - last_tok.bbox[2]
                    max_allowed_gap = line.dominant_font_size * self.max_char_h_gap_ratio
                    if -line.dominant_font_size <= gap <= max_allowed_gap:
                        y_min = min(token.bbox[1], last_tok.bbox[1])
                        y_max = max(token.bbox[3], last_tok.bbox[3])
                        if not _has_v_barrier(last_tok.bbox[2], token.bbox[0], y_min, y_max, v_barriers):
                            line.tokens.append(token)
                            line.baseline_y = float(np.mean([t.origin[1] for t in line.tokens]))
                            matched = True
                            break

            if not matched:
                new_line = TextLine(
                    tokens=[token],
                    baseline_y=token.origin[1],
                    dominant_font_size=token.font_size,
                )
                lines.append(new_line)

        for line in lines:
            line.finalize()
        return lines

    def _attach_underlines_and_filter_barriers(
        self,
        lines: List[TextLine],
        h_segments: List[Tuple[float, float, float, float, int]],
    ) -> List[Tuple[float, float, float, float]]:
        structural_h_barriers: List[Tuple[float, float, float, float]] = []

        for seg in h_segments:
            hx0, hy0, hx1, hy1 = seg[0], seg[1], seg[2], seg[3]
            seg_w = hx1 - hx0
            hy_mid = (hy0 + hy1) * 0.5
            matched_as_underline = False

            for ln in lines:
                lx0, ly0, lx1, ly1 = ln.bbox
                line_w = max(1.0, lx1 - lx0)
                size = ln.dominant_font_size
                dy = hy_mid - ln.baseline_y
                if -0.05 * size <= dy <= 0.35 * size:
                    overlap_x = min(lx1, hx1) - max(lx0, hx0)
                    if overlap_x >= 3.0 and seg_w <= line_w + 18.0:
                        matched_as_underline = True
                        ln.underline_rects.append((hx0, hy0, hx1, hy1))
                        ln.underline_colors.append(int(seg[4]) if len(seg) > 4 else 0)
                        for tok in ln.tokens:
                            if not tok.char.isspace():
                                tx_mid = (tok.bbox[0] + tok.bbox[2]) * 0.5
                                if (hx0 - 1.0) <= tx_mid <= (hx1 + 1.0):
                                    tok.underline = True
                        break

            if not matched_as_underline:
                structural_h_barriers.append((hx0, hy0, hx1, hy1))

        for ln in lines:
            if ln.underline_rects:
                ln.finalize()

        return structural_h_barriers

    def cluster_lines_into_paragraphs(
            self,
            lines: List[TextLine],
            h_barriers: List[Tuple[float, float, float, float]] = (),
            v_barriers: List[Tuple[float, float, float, float]] = (),
            page_width: float = 612.0,
        ) -> List[ParagraphBlock]:
            if not lines:
                return []

            lines.sort(key=lambda ln: (round(ln.baseline_y, 1), ln.bbox[0]))
            paragraphs: List[ParagraphBlock] = []

            for line in lines:
                if _starts_with_bullet(line.text):
                    paragraphs.append(ParagraphBlock(lines=[line]))
                    continue

                best_para: Optional[ParagraphBlock] = None
                best_dy = 1e9

                for cand in reversed(paragraphs):
                    prev_line = cand.lines[-1]
                    delta_y = line.baseline_y - prev_line.baseline_y
                    if delta_y <= 0.0:
                        continue

                    median_size = (line.dominant_font_size + prev_line.dominant_font_size) / 2.0
                    max_lead = median_size * self.para_lead_ratio
                    if delta_y > max_lead:
                        continue

                    # STRICT ISOLATION: Never merge a line into a paragraph if there is a font size
                    # or style jump (e.g. merging a body word/name into a larger heading)
                    if abs(line.dominant_font_size - prev_line.dominant_font_size) > 0.5:
                        continue

                    # STRICT ISOLATION: If the previous line is short and looks like a header/title
                    # (e.g. ends well before the right margin), never glue a trailing word/name to it.
                    prev_w = prev_line.bbox[2] - prev_line.bbox[0]
                    if prev_w < 350.0 and (prev_line.bbox[2] < page_width - 120.0):
                        # Check if the current line sits to the right of the previous line (inline text)
                        if line.bbox[0] > prev_line.bbox[2] + 4.0:
                            continue

                    if len(cand.lines) >= 2:
                        prev_dy = cand.lines[-1].baseline_y - cand.lines[-2].baseline_y
                        if abs(delta_y - prev_dy) > max(3.5, median_size * 0.35):
                            continue

                    prev_x0, prev_y0, prev_x1, prev_y1 = prev_line.bbox
                    curr_x0, curr_y0, curr_x1, curr_y1 = line.bbox

                    overlap_x = max(0.0, min(prev_x1, curr_x1) - max(prev_x0, curr_x0))
                    left_diff = abs(curr_x0 - prev_x0)
                    has_spatial_continuity = (
                        (overlap_x > 0.0 and left_diff <= median_size * 2.5)
                        or left_diff <= median_size * 1.8
                    )
                    if not has_spatial_continuity:
                        continue

                    x_span_0 = min(prev_x0, curr_x0)
                    x_span_1 = max(prev_x1, curr_x1)
                    if _has_h_barrier(prev_line.baseline_y, line.bbox[1], x_span_0, x_span_1, h_barriers):
                        continue

                    para_max_x1 = max(l.bbox[2] for l in cand.lines)
                    para_min_x0 = min(l.bbox[0] for l in cand.lines)
                    para_w = para_max_x1 - para_min_x0
                    if len(cand.lines) >= 2 and para_w > 140.0:
                        if (para_max_x1 - prev_x1) > max(45.0, para_w * 0.22):
                            continue

                    if delta_y < best_dy:
                        best_dy = delta_y
                        best_para = cand

                if best_para is not None:
                    best_para.lines.append(line)
                else:
                    paragraphs.append(ParagraphBlock(lines=[line]))

            for para in paragraphs:
                para.finalize()

            for p in paragraphs:
                px0, py0, px1, py1 = p.bbox
                limit_x1 = page_width - 36.0
                for vx0, vy0, vx1, vy1 in v_barriers:
                    if vx0 >= px1 - 2.0 and min(py1, vy1) - max(py0, py0) > 0:
                        limit_x1 = min(limit_x1, vx0 - 3.0)
                for other in paragraphs:
                    if other is p:
                        continue
                    ox0, oy0, ox1, oy1 = other.bbox
                    if ox0 >= px1 + 4.0 and min(py1, oy1) - max(py0, oy0) > 0:
                        limit_x1 = min(limit_x1, ox0 - 8.0)
                p.container_max_x1 = max(px1, limit_x1)

            return paragraphs

    def process(self, raw_page_geometry: Dict[str, Any]) -> List[ParagraphBlock]:
        tokens: List[CharToken] = []
        seq = 0
        for block in raw_page_geometry.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    f_name = span.get("font", "Unknown")
                    f_size = float(span.get("size", 12.0))
                    f_flags = int(span.get("flags", 0))
                    for ch in span.get("chars", []):
                        tokens.append(
                            CharToken(
                                char=ch["char"],
                                bbox=tuple(ch["bbox"]),
                                origin=tuple(ch["origin"]),
                                font_name=f_name,
                                font_size=f_size,
                                flags=f_flags,
                                color=int(span.get("color") or 0),
                                seq_id=seq,
                            )
                        )
                        seq += 1

        v_barriers = raw_page_geometry.get("v_barriers", [])
        h_segments = raw_page_geometry.get("h_segments", [])
        page_width = float(raw_page_geometry.get("page_width", 612.0))

        lines = self.cluster_characters_into_lines(tokens, v_barriers=v_barriers)
        h_barriers = self._attach_underlines_and_filter_barriers(lines, h_segments)
        return self.cluster_lines_into_paragraphs(
            lines, h_barriers=h_barriers, v_barriers=v_barriers, page_width=page_width
        )
