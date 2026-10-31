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
    """Return the most frequent style in the block (ignoring bullet symbol at index 0)."""
    if not styles:
        return DEFAULT_STYLE
    pool = styles[1:] if len(styles) > 2 else styles
    counts = {}
    for st in pool:
        counts[st] = counts.get(st, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _neighbour_style(old_text: str, old_styles: List[Style], i: int, dominant: Style) -> Style:
    """Style for text typed at position i of the old text: like a word processor, it continues the
    character before the caret; at the start of a word (blank before) it takes the style of the word it
    joins. The block's dominant style is only the last resort. (Using it for every insertion turned the
    inserted 'dre' of a bold 'hundred' into regular type.)"""
    n = len(old_styles)
    prev_ok = i > 0 and i - 1 < n and not old_text[i - 1].isspace()
    next_ok = i < n and i < len(old_text) and not old_text[i].isspace()
    if prev_ok:
        return old_styles[i - 1]
    if next_ok:
        return old_styles[i]
    if i > 0 and i - 1 < n:
        return old_styles[i - 1]
    return old_styles[i] if i < n else dominant


def new_char_styles(old_text: str, old_styles: List[Style], new_text: str) -> List[Style]:
    """One Style per character of new_text (same length)."""
    if not old_styles:
        return [DEFAULT_STYLE] * len(new_text)
    dominant = _dominant_style(old_styles)
    has_any_underline = any(st.underline for st in old_styles)

    out: List[Style] = []
    sm = SequenceMatcher(None, old_text, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(old_styles[i1:i2])
        elif tag == "replace":
            for k in range(j2 - j1):
                base_st = old_styles[min(i1 + k, i2 - 1)]
                # If editing an underlined link, preserve underline on replacement text
                if has_any_underline and not base_st.underline:
                    base_st = Style(base_st.font, base_st.flags, base_st.size, base_st.color, True)
                out.append(base_st)
        elif tag == "insert":
            st = _neighbour_style(old_text, old_styles, i1, dominant)
            if has_any_underline and not st.underline:
                st = Style(st.font, st.flags, st.size, st.color, True)
            out.extend([st] * (j2 - j1))
    if len(out) != len(new_text):
        return [dominant] * len(new_text)
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
