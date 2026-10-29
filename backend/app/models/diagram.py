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
    INPUT_OUTPUT = "input_output"  # parallelogram — input / read
    DOCUMENT = "document"  # rectangle with a wavy bottom — print / output ("Çap")
    # Not a symbol: the open end of an arrow that stops in empty space (the
    # algorithm continues outside the picture). Drawn as nothing; edges into
    # it end in a bare arrowhead at its position.
    EXIT = "exit"


class PortSide(str, Enum):
    """Which side of a node an edge exits from."""

    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


class DiagramNode(BaseModel):
    """One flowchart symbol, as identified by the vision model."""

    id: str = Field(description="Short unique id, e.g. 'n1'.")
    shape_type: ShapeType
    text: str = Field(
        description="Label exactly as written in the source, Azerbaijani preserved."
    )
    mark: int | None = Field(
        default=None,
        description=(
            "The red number drawn next to this symbol in the marked image, "
            "if the symbol has one; null otherwise."
        ),
    )
    box_2d: list[int] | None = Field(
        default=None,
        description=(
            "Bounding box of this symbol in the source image as "
            "[ymin, xmin, ymax, xmax], integers normalised to 0-1000 "
            "(relative to image height / width)."
        ),
    )


class DiagramEdge(BaseModel):
    """A directed connection between two nodes."""

    source_id: str
    target_id: str
    label: str | None = Field(
        default=None, description="Branch label, e.g. 'Hə' or 'Yox'."
    )
    source_port: PortSide | None = Field(
        default=None,
        description=(
            "Which side of the source node this edge exits from. "
            "Set by vision for decision-node branches; None = let layout decide."
        ),
    )
    is_back_edge: bool = Field(
        default=False,
        description=(
            "Set by the layout engine when the target sits above the source "
            "(a loop). Renderers must route these around the main column."
        ),
    )
    exact_route: bool = Field(
        default=False,
        description=(
            "Set by the box-based layout engine: ``edge_path`` is the final "
            "orthogonal route and must be drawn as-is (no connector presets)."
        ),
    )
    label_align_right: bool = Field(
        default=False,
        description=(
            "Set by the layout engine: the label sits LEFT of its line, so its "
            "text is right-aligned in the label box (grows away from the line)."
        ),
    )
    label_x: float | None = Field(
        default=None,
        description="Label center x in layout px, set by the layout engine.",
    )
    label_y: float | None = Field(
        default=None,
        description="Label top y in layout px, set by the layout engine.",
    )
    edge_path: list[list[float]] | None = Field(
        default=None,
        description=(
            "Ordered waypoints [[x,y], ...] in layout px extracted from "
            "Graphviz's ortho edge routing. Includes start, bend points, "
            "and end. Set by the layout engine; consumed by renderers to "
            "choose OOXML connector presets and compute adjustment values."
        ),
    )


class DiagramGraph(BaseModel):
    """Topology of a diagram as returned by the vision provider."""

    nodes: list[DiagramNode]
    edges: list[DiagramEdge]
    image_width: int | None = Field(
        default=None, description="Source image width in px. Set by code; leave null."
    )
    image_height: int | None = Field(
        default=None, description="Source image height in px. Set by code; leave null."
    )
    boxes_detected: bool = Field(
        default=False,
        description="Set by code (boxes measured from the image); leave false.",
    )
    guide_segments: list[list[int]] | None = Field(
        default=None,
        description=(
            "Set by code: the original drawing's straight connector lines as "
            "[x0, y0, x1, y1] normalised to 0-1000. Leave null."
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "A heading or caption line outside the flowchart's symbols and "
            "arrows in the source image, if any (e.g. 'Blok-sxem nəyi "
            "hesablayır?', 'Sxem 3. ... blok-sxemi.') — distinct from any "
            "node's own label. None when the image has no such line."
        ),
    )
    title_below: bool = Field(
        default=False,
        description="True when the title/caption is printed BELOW the flowchart.",
    )


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
        right = max((n.x + n.width for n in self.nodes), default=0.0)
        for e in self.edges:
            if e.exact_route and e.edge_path:
                right = max(right, max(p[0] for p in e.edge_path))
        return right + 40.0

    @property
    def canvas_height(self) -> float:
        """Total height needed to draw the graph, including margins."""
        bottom = max((n.y + n.height for n in self.nodes), default=0.0)
        for e in self.edges:
            if e.exact_route and e.edge_path:
                bottom = max(bottom, max(p[1] for p in e.edge_path))
        return bottom + 40.0

    def node_by_id(self, node_id: str) -> LayoutedNode:
        """Return the node with the given id.

        Raises:
            KeyError: if no node has that id.
        """
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)
