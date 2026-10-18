import pymupdf as fitz
from typing import List, Tuple
from dataclasses import dataclass


@dataclass
class ReflowedLine:
    text: str
    baseline_origin: Tuple[float, float]
    bbox: Tuple[float, float, float, float]
    width: float


@dataclass
class ReflowResult:
    lines: List[ReflowedLine]
    total_bbox: Tuple[float, float, float, float]
    height_delta: float


class TextReflowEngine:
    def __init__(self, font_name: str = "helv", font_size: float = 11.0, line_height: float = None):
        self.font = fitz.Font(font_name)
        self.font_size = font_size
        # Use typographic ratio if line_height is not explicitly passed
        self.line_height = line_height if line_height is not None else (font_size * 1.3)

    @staticmethod
    def single_line(new_text: str, original_bbox, baseline_y: float) -> "ReflowResult":
        """A one-line cell (a number, a label, a heading) is never re-wrapped: it keeps its
        position and baseline, and the compiler measures it with the real font."""
        x0, y0, x1, y1 = original_bbox
        line = ReflowedLine(text=new_text, baseline_origin=(x0, baseline_y),
                            bbox=(x0, y0, x1, y1), width=x1 - x0)
        return ReflowResult(lines=[line], total_bbox=(x0, y0, x1, y1), height_delta=0.0)

    def measure_text_width(self, text: str) -> float:
        return self.font.text_length(text, fontsize=self.font_size)

    def reflow_paragraph(
        self,
        new_text: str,
        original_bbox: Tuple[float, float, float, float],
        initial_baseline_y: float,
        page_width: float = 612.0,  # Standard letter/A4 width fallback
        right_margin: float = 50.0
    ) -> ReflowResult:
        x0, y0, x1, y1 = original_bbox
        original_width = x1 - x0
        original_height = y1 - y0

        # Max width available on the page/column before wrapping is required
        available_page_width = max(original_width, page_width - x0 - right_margin)

        # If the original block was already a wide multi-line body, respect its right margin.
        # If it was a short heading/bullet, allow it to expand across the line.
        max_allowed_width = original_width if original_width > 280.0 else available_page_width

        # Split preserving explicit line breaks (\n)
        paragraphs_or_lines = new_text.split("\n")
        final_lines: List[str] = []

        for segment in paragraphs_or_lines:
            words = [w for w in segment.split(" ") if w]
            if not words:
                final_lines.append("")
                continue

            current_line: List[str] = []
            for word in words:
                test_line = " ".join(current_line + [word]) if current_line else word
                line_w = self.measure_text_width(test_line)

                if line_w <= max_allowed_width or not current_line:
                    current_line.append(word)
                else:
                    final_lines.append(" ".join(current_line))
                    current_line = [word]

            if current_line:
                final_lines.append(" ".join(current_line))

        # Build geometry for reflowed lines
        reflowed_lines: List[ReflowedLine] = []
        current_y = initial_baseline_y
        max_line_width = 0.0

        for line_str in final_lines:
            line_w = self.measure_text_width(line_str) if line_str else 0.0
            max_line_width = max(max_line_width, line_w)

            line_y0 = current_y - (self.font_size * 0.8)
            line_y1 = current_y + (self.font_size * 0.2)

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
        new_bbox = (x0, y0, x0 + max(original_width, max_line_width), max(y1, final_y1))
        new_height = new_bbox[3] - new_bbox[1]

        return ReflowResult(
            lines=reflowed_lines,
            total_bbox=new_bbox,
            height_delta=new_height - original_height
        )
