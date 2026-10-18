import numpy as np
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any


@dataclass
class CharToken:
    char: str
    bbox: Tuple[float, float, float, float]
    origin: Tuple[float, float]
    font_name: str
    font_size: float
    flags: int
    color: int = 0


@dataclass
class TextLine:
    tokens: List[CharToken] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    baseline_y: float = 0.0
    dominant_font_size: float = 0.0
    text: str = ""
    first_word_w: float = 0.0

    def _word_gap_em(self) -> float:
        """Word-space threshold in em, measured on this line. Letters inside a word share
        one gap (about 0; positive for letter-spaced text, negative for tight text), so the
        30th-percentile gap between letter pairs is the line's baseline; a word space is
        anything clearly above it. A fixed 25 % of the font size (= a full normal space)
        glued words in tight / justified / condensed lines, e.g. '56,000.00(Indian'."""
        gaps = []
        for a, b in zip(self.tokens, self.tokens[1:]):
            if a.char.isalnum() and b.char.isalnum() and a.font_size > 0:
                gaps.append((b.bbox[0] - a.bbox[2]) / a.font_size)
        gaps.sort()
        base = gaps[int(len(gaps) * 0.3)] if len(gaps) >= 3 else 0.0
        base = max(-0.2, min(0.3, base))
        return max(0.04, base + 0.045)

    def _drop_tracking_spaces(self):
        """Letter-spaced text ("P A Y   S L I P"): the extractor puts a space between EVERY
        pair of letters. When nearly all letter pairs carry a space and a few are much wider,
        the narrow ones are tracking, not words; keep only the wide ones."""
        toks = self.tokens
        letters = sum(1 for t in toks if not t.char.isspace())
        spaces = [t for t in toks if t.char == " "]
        if letters < 6 or len(spaces) < 0.7 * (letters - 1):
            return
        widths = sorted(t.bbox[2] - t.bbox[0] for t in spaces)
        median, widest = widths[len(widths) // 2], widths[-1]
        if widest < 1.8 * max(median, 0.01):
            return                       # all alike: cannot tell tracking from words, keep as is
        cut = (median + widest) / 2.0
        self.tokens = [t for t in toks if not (t.char == " " and (t.bbox[2] - t.bbox[0]) < cut)]

    def finalize(self):
        if not self.tokens:
            return
        self.tokens.sort(key=lambda t: t.bbox[0])
        self._drop_tracking_spaces()

        x0 = min(t.bbox[0] for t in self.tokens)
        y0 = min(t.bbox[1] for t in self.tokens)
        x1 = max(t.bbox[2] for t in self.tokens)
        y1 = max(t.bbox[3] for t in self.tokens)
        self.bbox = (x0, y0, x1, y1)

        self.baseline_y = float(np.median([t.origin[1] for t in self.tokens]))
        self.dominant_font_size = float(np.median([t.font_size for t in self.tokens]))

        thr = self._word_gap_em()
        chars_out = []
        first_end = None
        for i, t in enumerate(self.tokens):
            if i > 0:
                prev_t = self.tokens[i - 1]
                dx = t.bbox[0] - prev_t.bbox[2]
                size = max(prev_t.font_size, 1.0)
                # a gap is a word space when it is clearly wider than the gaps between
                # letters of THIS line; never next to a real space (that made doubles)
                if (dx > thr * size and not prev_t.char.isspace() and not t.char.isspace()
                        and abs(t.origin[1] - prev_t.origin[1]) < size * 0.5):
                    chars_out.append(" ")
                    if first_end is None:
                        first_end = prev_t.bbox[2]
                elif t.char.isspace() and first_end is None:
                    first_end = prev_t.bbox[2]
            chars_out.append(t.char)
        if first_end is None:
            first_end = self.tokens[-1].bbox[2]
        self.first_word_w = max(0.0, first_end - self.tokens[0].bbox[0])
        self.text = "".join(chars_out)


@dataclass
class ParagraphBlock:
    lines: List[TextLine] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    text: str = ""

    def finalize(self):
        if not self.lines:
            return
        self.lines.sort(key=lambda ln: ln.baseline_y)

        x0 = min(ln.bbox[0] for ln in self.lines)
        y0 = min(ln.bbox[1] for ln in self.lines)
        x1 = max(ln.bbox[2] for ln in self.lines)
        y1 = max(ln.bbox[3] for ln in self.lines)
        self.bbox = (x0, y0, x1, y1)
        # A line break is HARD only if the next word would still have fitted on the line;
        # otherwise it is a soft wrap and becomes a space, so an edited paragraph re-wraps.
        right = x1
        firsts = [ln.text.split(" ")[0] for ln in self.lines]
        top = max((firsts.count(w) for w in set(firsts)), default=0)
        bullet = sum(1 for w in firsts if w[:1] in "\u2022-\u2013*\u25cf\u25aa>" or w.rstrip(".)").isdigit())
        list_like = len(self.lines) >= 2 and (top >= max(2, 0.6 * len(self.lines)) or bullet >= 2)
        flowing = (not list_like and len(self.lines) >= 2
                   and (right - self.lines[-1].bbox[2]) > 0.02 * max(x1 - x0, 1.0))
        parts = []
        for k, ln in enumerate(self.lines):
            parts.append(ln.text)
            if k + 1 < len(self.lines):
                nxt = self.lines[k + 1]
                need = nxt.first_word_w + 0.25 * ln.dominant_font_size
                if flowing and (right - ln.bbox[2]) < need + 2.0:
                    hyphen = ln.text.endswith("-") and nxt.text[:1].islower()
                    parts.append("" if (hyphen or ln.text.endswith(" ")) else " ")
                else:
                    parts.append("\n")
        self.text = "".join(parts)


class SpatialClusterer:
    def __init__(
        self,
        line_y_tolerance_ratio: float = 0.35,
        para_lead_ratio: float = 1.6,
        max_char_h_gap_ratio: float = 2.5,
    ):
        self.line_y_tolerance_ratio = line_y_tolerance_ratio
        self.para_lead_ratio = para_lead_ratio
        self.max_char_h_gap_ratio = max_char_h_gap_ratio

    def cluster_characters_into_lines(self, tokens: List[CharToken]) -> List[TextLine]:
        if not tokens:
            return []

        tokens.sort(key=lambda t: (t.origin[1], t.bbox[0]))
        lines: List[TextLine] = []

        for token in tokens:
            matched = False
            for line in reversed(lines[-8:] if len(lines) >= 8 else lines):
                allowed_y_jitter = line.dominant_font_size * self.line_y_tolerance_ratio
                same_row = abs(token.origin[1] - line.baseline_y) <= allowed_y_jitter

                if same_row:
                    closest_char_dist = min(abs(token.bbox[0] - t.bbox[2]) for t in line.tokens)
                    max_allowed_gap = line.dominant_font_size * self.max_char_h_gap_ratio
                    if closest_char_dist <= max_allowed_gap:
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

    def cluster_lines_into_paragraphs(self, lines: List[TextLine]) -> List[ParagraphBlock]:
        if not lines:
            return []

        lines.sort(key=lambda ln: (round(ln.bbox[0] / 50.0), ln.baseline_y))
        paragraphs: List[ParagraphBlock] = []

        for line in lines:
            if not paragraphs:
                paragraphs.append(ParagraphBlock(lines=[line]))
                continue

            current_para = paragraphs[-1]
            prev_line = current_para.lines[-1]

            delta_y = line.baseline_y - prev_line.baseline_y
            median_size = (line.dominant_font_size + prev_line.dominant_font_size) / 2.0
            max_lead = median_size * self.para_lead_ratio

            prev_x0, _, prev_x1, _ = prev_line.bbox
            curr_x0, _, curr_x1, _ = line.bbox
            overlap_x = max(0.0, min(prev_x1, curr_x1) - max(prev_x0, curr_x0))
            has_spatial_continuity = (
                overlap_x > 0.0 or abs(curr_x0 - prev_x0) <= (median_size * 2.0)
            )
            font_size_continuity = abs(line.dominant_font_size - prev_line.dominant_font_size) <= 1.5

            if (0.0 < delta_y <= max_lead) and has_spatial_continuity and font_size_continuity:
                current_para.lines.append(line)
            else:
                paragraphs.append(ParagraphBlock(lines=[line]))

        for para in paragraphs:
            para.finalize()
        return paragraphs

    def process(self, raw_page_geometry: Dict[str, Any]) -> List[ParagraphBlock]:
        tokens: List[CharToken] = []
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
                            )
                        )

        lines = self.cluster_characters_into_lines(tokens)
        return self.cluster_lines_into_paragraphs(lines)
