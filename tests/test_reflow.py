import pymupdf as fitz
from typing import List, Tuple, Dict, Any
from dataclasses import dataclass


@dataclass
class ReflowedLine:
    text: str
    baseline_origin: Tuple[float, float]  # (x, y)
    bbox: Tuple[float, float, float, float]  # (x0, y0, x1, y1)
    width: float


@dataclass
class ReflowResult:
    lines: List[ReflowedLine]
    total_bbox: Tuple[float, float, float, float]
    height_delta: float  # Difference from original paragraph height (push/pull factor)


class TextReflowEngine:
    """
    Simulates real-time typesetting inside a paragraph boundary box.
    Uses PyMuPDF font tables to measure true typographical string advance widths.
    """
    def __init__(self, font_name: str = "helv", font_size: float = 11.0, line_height: float = 15.0):
        self.font = fitz.Font(font_name)
        self.font_size = font_size
        self.line_height = line_height  # Distance between baselines

    def measure_text_width(self, text: str) -> float:
        """Computes true advance width of a string in PDF points."""
        return self.font.text_length(text, fontsize=self.font_size)

    def reflow_paragraph(
        self,
        new_text: str,
        original_bbox: Tuple[float, float, float, float],
        initial_baseline_y: float
    ) -> ReflowResult:
        x0, y0, x1, y1 = original_bbox
        max_allowed_width = x1 - x0
        original_height = y1 - y0

        words = new_text.replace("\n", " \n ").split(" ")

        lines: List[str] = []
        current_line: List[str] = []

        for word in words:
            if word == "\n":
                lines.append(" ".join(current_line))
                current_line = []
                continue

            test_line = " ".join(current_line + [word]) if current_line else word
            line_width = self.measure_text_width(test_line)

            if line_width <= max_allowed_width or not current_line:
                current_line.append(word)
            else:
                lines.append(" ".join(current_line))
                current_line = [word]

        if current_line:
            lines.append(" ".join(current_line))

        # Position calculated lines into geometry
        reflowed_lines: List[ReflowedLine] = []
        current_y = initial_baseline_y

        for i, line_str in enumerate(lines):
            line_w = self.measure_text_width(line_str)
            line_y0 = current_y - (self.font_size * 0.8)  # Approximate ascender
            line_y1 = current_y + (self.font_size * 0.2)  # Approximate descender

            reflowed_lines.append(
                ReflowedLine(
                    text=line_str,
                    baseline_origin=(x0, current_y),
                    bbox=(x0, line_y0, x0 + line_w, line_y1),
                    width=line_w
                )
            )
            current_y += self.line_height

        final_y1 = current_y - self.line_height + (self.font_size * 0.2)
        new_bbox = (x0, y0, x1, max(y1, final_y1))
        new_height = new_bbox[3] - new_bbox[1]

        return ReflowResult(
            lines=reflowed_lines,
            total_bbox=new_bbox,
            height_delta=new_height - original_height
        )


# =====================================================================
# SIMULATE USER EDITING IN THE REFLOW LOOP
# =====================================================================
if __name__ == "__main__":
    print("[*] Testing Step 4: Typesetting & Reflow Engine\n")

    # Original metrics for Block 2 from your run:
    # BBox: (50.0, 88.18, 381.34, 118.29) -> Width = 331.34 pt
    orig_bbox = (50.0, 88.18, 381.34, 118.29)
    baseline_y = 100.0  # Initial baseline

    engine = TextReflowEngine(font_name="helv", font_size=11.0, line_height=15.0)

    # 1. Simulate an edit that SHRINKS the paragraph to 1 short sentence
    short_edit = "User deleted half the paragraph."
    res_short = engine.reflow_paragraph(short_edit, orig_bbox, baseline_y)

    print("--- 1. SHORT EDIT (Underflow) ---")
    print(f"Lines rendered: {len(res_short.lines)}")
    for ln in res_short.lines:
        print(f"  Line: '{ln.text}' (width: {ln.width:.2f} pt)")
    print(f"Height Delta: {res_short.height_delta:.2f} pt\n")

    # 2. Simulate an edit where the user TYPED MUCH MORE TEXT (causing word-wrapping)
    long_edit = (
        "This is the edited paragraph where the user typed a significantly longer "
        "amount of text into the PDF editor. Notice how the reflow engine calculates "
        "line widths against the original paragraph boundaries and wraps seamlessly."
    )
    res_long = engine.reflow_paragraph(long_edit, orig_bbox, baseline_y)

    print("--- 2. LONG EDIT (Word Wrap & Overflow) ---")
    print(f"Lines rendered: {len(res_long.lines)}")
    for i, ln in enumerate(res_long.lines, 1):
        print(f"  Line {i}: '{ln.text}' (width: {ln.width:.2f} pt / max: {orig_bbox[2]-orig_bbox[0]:.2f} pt)")
    print(f"New BBox: {[round(c, 2) for c in res_long.total_bbox]}")
    print(f"Height Delta (Push factor for downstream blocks): +{res_long.height_delta:.2f} pt")
