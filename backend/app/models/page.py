"""Schema for a whole scanned/photographed page ("page mode").

A page is described as a list of blocks in reading order: printed text
(questions, answer options), simple tables, a flowchart (the existing
``DiagramGraph``) and figures that are not flowcharts (pictures, plots,
pattern matrices). The assemblers write text as editable text, the flowchart
as native shapes and cut every figure out of the source image.

The model fills ONE flat block type (``kind`` selects which field is used),
like ``VisionAnalysis`` does, because response schemas without unions are the
safest across SDK versions.
"""

from enum import Enum

from pydantic import BaseModel, Field

from app.models.diagram import DiagramGraph
from app.models.text_content import TextRun


class BlockKind(str, Enum):
    """What a page block contains."""

    TEXT = "text"
    TABLE = "table"
    FLOWCHART = "flowchart"
    FIGURE = "figure"


class TextStyle(str, Enum):
    """Role of a text block (drives spacing and slide grouping only)."""

    HEADING = "heading"  # a question line, e.g. "118. Hansı blok ...?"
    PARAGRAPH = "paragraph"
    OPTION = "option"  # an answer option line, e.g. "A) Şərt"


class PageBlock(BaseModel):
    """One block of a page. Only the fields of its ``kind`` are used."""

    kind: BlockKind
    style: TextStyle | None = Field(
        default=None, description="For text blocks: heading, paragraph or option."
    )
    runs: list[TextRun] | None = Field(
        default=None,
        description=(
            "For text blocks: the printed line(s) as runs. Sub/superscripts are "
            "separate runs with the matching flag."
        ),
    )
    rows: list[list[str]] | None = Field(
        default=None, description="For table blocks: rows of cell texts."
    )
    diagram: DiagramGraph | None = Field(
        default=None, description="For flowchart blocks: nodes and edges."
    )
    box_2d: list[int] | None = Field(
        default=None,
        description=(
            "Bounding box of this block in the source image as "
            "[ymin, xmin, ymax, xmax], integers normalised to 0-1000."
        ),
    )

    @property
    def text(self) -> str:
        """Plain text of a text block (empty for other kinds)."""
        return "".join(run.text for run in self.runs or [])


class PageAnalysis(BaseModel):
    """All blocks of one page, in reading order."""

    blocks: list[PageBlock]
