"""Per-character style tracking, so an edit keeps the look of the text around it.

The old code drew a whole block in the font/size/colour of its FIRST character. A payslip
cell, a "Label: value" line or a bold word inside a sentence therefore changed look as soon
as it was edited. Here every character of the original text remembers its own style; the
old and new text are diffed, characters that survive keep their style, and a typed
replacement inherits the style of the character it replaced.

Pure Python (no PyMuPDF), so it is easy to unit-test.
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Tuple


@dataclass(frozen=True)
class Style:
    font: str
    flags: int
    size: float
    color: int = 0


@dataclass
class Run:
    text: str
    style: Style


DEFAULT_STYLE = Style("helv", 0, 11.0, 0)


def style_of(tok) -> Style:
    return Style(tok.font_name, int(tok.flags), round(float(tok.font_size), 2), int(tok.color or 0))


def old_char_styles(block) -> List[Style]:
    """One Style per character of block.text.

    block.text was built from the tokens plus separators the extractor invented (word
    spaces, line breaks). Walk both in parallel: a matching character takes its token's
    style, an invented separator takes the style of the character before it."""
    flat = [t for ln in block.lines for t in ln.tokens]
    if not flat:
        return [DEFAULT_STYLE] * len(block.text)
    out: List[Style] = []
    j, last = 0, style_of(flat[0])
    for ch in block.text:
        if j < len(flat) and flat[j].char == ch:
            last = style_of(flat[j])
            j += 1
        elif not ch.isspace():
            # a token was dropped (tracking space etc.): resync a few tokens ahead
            for k in range(j + 1, min(j + 4, len(flat))):
                if flat[k].char == ch:
                    j = k
                    last = style_of(flat[j])
                    j += 1
                    break
        out.append(last)
    return out


def new_char_styles(old_text: str, old_styles: List[Style], new_text: str) -> List[Style]:
    """One Style per character of new_text (same length)."""
    if not old_styles:
        return [DEFAULT_STYLE] * len(new_text)
    out: List[Style] = []
    sm = SequenceMatcher(None, old_text, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(old_styles[i1:i2])
        elif tag == "replace":
            for k in range(j2 - j1):
                out.append(old_styles[min(i1 + k, i2 - 1)])
        elif tag == "insert":
            out.extend([old_styles[i1 - 1] if i1 > 0 else old_styles[0]] * (j2 - j1))
        # "delete": nothing to add
    if len(out) != len(new_text):                 # cannot happen; stay safe anyway
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
