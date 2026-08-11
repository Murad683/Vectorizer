"""Pydantic schemas for structured diagram data.

The vision model produces a ``DiagramGraph`` (topology + text only —
no geometry). The layout engine turns it into a ``LayoutedGraph`` with
concrete coordinates; renderers consume the layouted form. All
coordinates are in layout pixels with a top-left origin.
"""

from enum import Enum

from pydantic import BaseModel, Field


class ShapeType(str, Enum):
    """Flowchart symbol vocabulary found in the source materials."""

    TERMINATOR = "terminator"  # oval — Başlanğıc / Son
    PROCESS = "process"  # rectangle — assignments, actions
    DECISION = "decision"  # diamond — conditions
    INPUT_OUTPUT = "input_output"  # parallelogram — input / print


class DiagramNode(BaseModel):
    """One flowchart symbol, as identified by the vision model."""

    id: str = Field(description="Short unique id, e.g. 'n1'.")
    shape_type: ShapeType
    text: str = Field(
        description="Label exactly as written in the source, Azerbaijani preserved."
    )


class DiagramEdge(BaseModel):
    """A directed connection between two nodes."""

    source_id: str
    target_id: str
    label: str | None = Field(
        default=None, description="Branch label, e.g. 'Hə' or 'Yox'."
    )
    is_back_edge: bool = Field(
        default=False,
        description=(
            "Set by the layout engine when the target sits above the source "
            "(a loop). Renderers must route these around the main column."
        ),
    )


class DiagramGraph(BaseModel):
    """Topology of a diagram as returned by the vision provider."""

    nodes: list[DiagramNode]
    edges: list[DiagramEdge]


class LayoutedNode(DiagramNode):
    """A diagram node with geometry computed by the layout engine."""

    x: float = Field(description="Left edge, layout px.")
    y: float = Field(description="Top edge, layout px.")
    width: float
    height: float


class LayoutedGraph(BaseModel):
    """A fully positioned diagram, ready for rendering."""

    nodes: list[LayoutedNode]
    edges: list[DiagramEdge]

    @property
    def canvas_width(self) -> float:
        """Total width needed to draw the graph, including margins."""
        return max((n.x + n.width for n in self.nodes), default=0.0) + 40.0

    @property
    def canvas_height(self) -> float:
        """Total height needed to draw the graph, including margins."""
        return max((n.y + n.height for n in self.nodes), default=0.0) + 40.0

    def node_by_id(self, node_id: str) -> LayoutedNode:
        """Return the node with the given id.

        Raises:
            KeyError: if no node has that id.
        """
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)
