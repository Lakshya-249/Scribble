import pymupdf as fitz
from typing import List, Tuple, Optional, Callable
from dataclasses import dataclass


@dataclass
class ReflowedLine:
    text: str
    baseline_origin: Tuple[float, float]
    bbox: Tuple[float, float, float, float]
    width: float
    is_last_in_para: bool = True
    target_width: float = 0.0


@dataclass
class ReflowResult:
    lines: List[ReflowedLine]
    total_bbox: Tuple[float, float, float, float]
    height_delta: float


class TextReflowEngine:
    def __init__(
        self,
        font_name: str = "helv",
        font_size: float = 11.0,
        line_height: Optional[float] = None,
        measure_fn: Optional[Callable[[str], float]] = None,
    ):
        try:
            self.font = fitz.Font(font_name)
        except Exception:
            self.font = fitz.Font("helv")
        self.font_size = font_size
        self.line_height = line_height if (line_height is not None and line_height > 2.0) else (font_size * 1.28)
        self._measure_fn = measure_fn

    @staticmethod
    def single_line(new_text: str, original_bbox, baseline_y: float) -> "ReflowResult":
        x0, y0, x1, y1 = original_bbox
        line = ReflowedLine(
            text=new_text,
            baseline_origin=(x0, baseline_y),
            bbox=(x0, y0, x1, y1),
            width=x1 - x0,
            is_last_in_para=True,
            target_width=x1 - x0,
        )
        return ReflowResult(lines=[line], total_bbox=(x0, y0, x1, y1), height_delta=0.0)

    def measure_text_width(self, text: str) -> float:
        if self._measure_fn is not None:
            return self._measure_fn(text)
        return self.font.text_length(text, fontsize=self.font_size)

    def reflow_paragraph(
        self,
        new_text: str,
        original_bbox: Tuple[float, float, float, float],
        initial_baseline_y: float,
        page_width: float = 612.0,
        right_margin: float = 50.0,
        container_max_x1: Optional[float] = None,
        first_line_indent: float = 0.0,
        hanging_indent: float = 0.0,
        alignment: str = "left",
        original_line_baselines: Optional[List[float]] = None,
        original_line_text_lefts: Optional[List[float]] = None,
    ) -> ReflowResult:
        x0, y0, x1, y1 = original_bbox
        original_width = x1 - x0

        max_allowed_width = container_max_x1 - x0 if container_max_x1 else original_width

        paragraphs_or_lines = new_text.split("\n")
        final_lines: List[Tuple[str, bool, bool]] = []

        for segment in paragraphs_or_lines:
            words = [w for w in segment.split(" ") if w]
            if not words:
                final_lines.append(("", True, True))
                continue

            current_line: List[str] = []
            is_first = True
            for word in words:
                test_line = " ".join(current_line + [word]) if current_line else word
                line_w = self.measure_text_width(test_line)
                left_offset = first_line_indent if is_first else hanging_indent
                line_limit = max_allowed_width - left_offset

                if line_w <= (line_limit + 1.5) or not current_line:
                    current_line.append(word)
                else:
                    final_lines.append((" ".join(current_line), False, is_first))
                    current_line = [word]
                    is_first = False

            if current_line:
                final_lines.append((" ".join(current_line), True, is_first))

        reflowed_lines: List[ReflowedLine] = []
        current_y = initial_baseline_y
        max_line_width = 0.0

        for idx, (line_str, is_last, is_first) in enumerate(final_lines):
            # LOCK TO EXACT ORIGINAL TEXT X AND Y: Never shift text left into bullets!
            if original_line_baselines and idx < len(original_line_baselines):
                current_y = original_line_baselines[idx]
                line_x0 = original_line_text_lefts[idx] if original_line_text_lefts and idx < len(original_line_text_lefts) else x0
            elif idx > 0:
                current_y += self.line_height
                line_x0 = original_line_text_lefts[-1] if original_line_text_lefts else x0
            else:
                line_x0 = original_line_text_lefts[0] if original_line_text_lefts else x0

            indent = first_line_indent if (is_first and idx == 0) else hanging_indent
            line_x0 += indent
            line_w = self.measure_text_width(line_str) if line_str else 0.0
            target_w = max(0.0, original_width - indent)

            if alignment == "center" and original_width > line_w:
                line_x0 = x0 + (original_width - line_w) * 0.5
            elif alignment == "right" and original_width > line_w:
                line_x0 = x0 + (original_width - line_w)

            max_line_width = max(max_line_width, (line_x0 - x0) + line_w)

            line_y0 = current_y - (self.font_size * 0.82)
            line_y1 = current_y + (self.font_size * 0.22)

            reflowed_lines.append(
                ReflowedLine(
                    text=line_str,
                    baseline_origin=(line_x0, current_y),
                    bbox=(line_x0, line_y0, line_x0 + line_w, line_y1),
                    width=line_w,
                    is_last_in_para=is_last,
                    target_width=target_w,
                )
            )

        final_y1 = current_y + (self.font_size * 0.22)
        new_bbox = (x0, y0, x0 + max(original_width, max_line_width), final_y1)

        height_delta = 0.0 if (original_line_baselines and len(final_lines) == len(original_line_baselines)) else (final_y1 - y1)

        return ReflowResult(
            lines=reflowed_lines,
            total_bbox=new_bbox,
            height_delta=height_delta,
        )
