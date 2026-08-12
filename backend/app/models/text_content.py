"""Schema for scanned TEXT content that is not a diagram.

Scope: the two-list *matching* question structure actually seen in the
real sample (question 87 — a numbered list of base-10 numbers to match
against a lettered list of values in other numeral bases). It is NOT a
general rich-text model: it captures one or more independent labeled
lists, each a sequence of lines, each line a sequence of runs carrying a
subscript flag (for numeral-base notation such as 65₁₀ → runs
``"65"`` + subscript ``"10"``).

``TextLine.to_run_dicts`` converts a line into the SAME run-dict
convention the DOCX extractor emits for native subscript runs
(``{"text", "vert_align"}``), so the assembly renderers reuse their
existing subscript logic instead of inventing a new path.
"""

from pydantic import BaseModel, Field


class TextRun(BaseModel):
    """A run of characters with a single subscript state."""

    text: str
    subscript: bool = Field(
        default=False,
        description="True for subscript digits (e.g. the base in 65₁₀).",
    )


class TextLine(BaseModel):
    """One line of a labeled list (label text is part of the runs)."""

    runs: list[TextRun]

    def to_run_dicts(self) -> list[dict[str, str | None]]:
        """Convert to the extractor's ``metadata['runs']`` convention."""
        return [
            {"text": run.text, "vert_align": "subscript" if run.subscript else None}
            for run in self.runs
            if run.text
        ]

    @property
    def plain_text(self) -> str:
        """The line's text with subscripts inlined (for height estimates)."""
        return "".join(run.text for run in self.runs)


class LabeledList(BaseModel):
    """An independent labeled column, e.g. '1. … / 2. …' or 'a. … / b. …'."""

    lines: list[TextLine]


class TextContent(BaseModel):
    """One or more independent labeled lists extracted from an image."""

    heading: str | None = Field(
        default=None,
        description=(
            "The instruction/heading line above the lists, preserved exactly "
            "as written (including a leading question number if present in "
            "the image), e.g. '87. Uyğunluğu müəyyən edin.'. None if the "
            "image has no such line — never invent text that isn't there."
        ),
    )
    lists: list[LabeledList]

    def usable_columns(self) -> tuple[LabeledList, LabeledList] | None:
        """Return the first two non-empty lists as (left, right), or None.

        The renderers only support the two-list matching layout; a single
        list or all-empty lists are not usable and trigger the
        embed-original-image fallback.
        """
        non_empty = [lst for lst in self.lists if lst.lines]
        if len(non_empty) < 2:
            return None
        return non_empty[0], non_empty[1]
