from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Tuple


@dataclass(frozen=True)
class Style:
    font: str
    flags: int
    size: float
    color: int = 0
    underline: bool = False


@dataclass
class Run:
    text: str
    style: Style


DEFAULT_STYLE = Style("helv", 0, 11.0, 0, False)


def style_of(tok) -> Style:
    return Style(
        tok.font_name,
        int(tok.flags),
        round(float(tok.font_size), 3),
        int(tok.color or 0),
        bool(getattr(tok, "underline", False)),
    )


def old_char_styles(block) -> List[Style]:
    """One Style per character of block.text."""
    flat = [t for ln in block.lines for t in ln.tokens if not t.char.isspace()]
    if not flat:
        all_toks = [t for ln in block.lines for t in ln.tokens]
        return [style_of(all_toks[0]) if all_toks else DEFAULT_STYLE] * len(block.text)

    out: List[Style] = []
    j, last = 0, style_of(flat[0])
    for ch in block.text:
        if ch.isspace():
            out.append(last)
            continue
        if j < len(flat) and flat[j].char == ch:
            last = style_of(flat[j])
            j += 1
        else:
            for k in range(j + 1, min(j + 12, len(flat))):
                if flat[k].char == ch:
                    j = k
                    last = style_of(flat[j])
                    j += 1
                    break
        out.append(last)
    return out


def _dominant_style(styles: List[Style]) -> Style:
    if not styles:
        return DEFAULT_STYLE
    counts = {}
    for st in styles:
        counts[st] = counts.get(st, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def new_char_styles(old_text: str, old_styles: List[Style], new_text: str) -> List[Style]:
    """One Style per character of new_text (same length)."""
    if not old_styles:
        return [DEFAULT_STYLE] * len(new_text)
    dominant = _dominant_style(old_styles)
    out: List[Style] = []
    sm = SequenceMatcher(None, old_text, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(old_styles[i1:i2])
        elif tag == "replace":
            for k in range(j2 - j1):
                out.append(old_styles[min(i1 + k, i2 - 1)])
        elif tag == "insert":
            if i1 == 0 or i1 == len(old_text):
                st = dominant
            else:
                st = old_styles[i1 - 1] if i1 > 0 else dominant
            out.extend([st] * (j2 - j1))
    if len(out) != len(new_text):
        return [old_styles[0]] * len(new_text)
    return out


def _group(text: str, styles: List[Style]) -> List[Run]:
    runs: List[Run] = []
    for ch, st in zip(text, styles):
        if runs and runs[-1].style == st:
            runs[-1].text += ch
        else:
            runs.append(Run(ch, st))
    return runs


def line_runs(line_text: str, full_text: str, styles: List[Style], cursor: int,
              default: Style = DEFAULT_STYLE) -> Tuple[List[Run], int]:
    """Runs for one output line that is a slice of full_text, searching from `cursor`."""
    if not line_text:
        return [], cursor
    k = full_text.find(line_text, cursor)
    if k < 0 or k + len(line_text) > len(styles):
        return [Run(line_text, styles[0] if styles else default)], cursor
    return _group(line_text, styles[k:k + len(line_text)]), k + len(line_text)
