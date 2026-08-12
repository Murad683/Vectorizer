"""Renderer tests: debug SVG integration + native PPTX shape generation."""

import html
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

from pptx import Presentation
from pptx.shapes.connector import Connector
from pptx.util import Inches

from app.core.classifier import classify_elements
from app.core.ingestion.docx_extractor import DocxExtractor
from app.core.layout import layout_graph
from app.core.render.pptx_shapes import add_diagram_to_slide
from app.core.render.svg_debug import render_svg
from app.models.diagram import DiagramGraph
from app.models.document import ElementType, VisionResult


class TestSvgDebugRenderer:
    def test_full_pipeline_produces_valid_svg(
        self,
        sample_docx: Path,
        flowchart_png_bytes: bytes,
        flowchart_graph: DiagramGraph,
        tmp_path: Path,
    ) -> None:
        """Extract → classify → (mocked) vision → layout → SVG, on the
        real sample. Saved to a file for manual visual inspection."""
        elements = DocxExtractor().extract(sample_docx)
        classified = classify_elements(elements)
        diagram = next(
            c
            for c in classified
            if c.element_type == ElementType.DIAGRAM
            and c.element.raw_content == flowchart_png_bytes
        )
        mock_provider = MagicMock()
        mock_provider.analyze_diagram.return_value = VisionResult(
            diagram_graph=flowchart_graph
        )
        with patch("app.core.vision.get_vision_provider", return_value=mock_provider):
            from app.core.vision import get_vision_provider

            image = diagram.element.raw_content
            assert isinstance(image, bytes)
            result = get_vision_provider().analyze_diagram(image)

        assert result.diagram_graph is not None
        layouted = layout_graph(result.diagram_graph)
        svg = render_svg(layouted)

        out_file = tmp_path / "flowchart_debug.svg"
        out_file.write_text(svg, encoding="utf-8")

        root = ET.fromstring(svg)  # valid XML or this raises
        assert root.tag.endswith("svg")
        for node in flowchart_graph.nodes:
            assert html.escape(node.text) in svg
        # One drawn symbol per node (rect/polygon), plus the background rect.
        ns = "{http://www.w3.org/2000/svg}"
        symbols = root.findall(f".//{ns}rect") + root.findall(f".//{ns}polygon")
        assert len(symbols) - 1 == len(flowchart_graph.nodes)

    def test_back_edge_and_labels_present(self, flowchart_graph: DiagramGraph) -> None:
        svg = render_svg(layout_graph(flowchart_graph))
        assert "Hə" in svg
        assert "Yox" in svg
        assert "&#8635;" in svg  # loop icon marks the routed back-edge


class TestPptxShapes:
    def _build_pptx(self, flowchart_graph: DiagramGraph, path: Path) -> Path:
        layouted = layout_graph(flowchart_graph)
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
        add_diagram_to_slide(
            layouted,
            slide,
            offset_x_emu=Inches(0.5),
            offset_y_emu=Inches(0.2),
            max_width_emu=Inches(9),
            max_height_emu=Inches(7),
        )
        prs.save(str(path))
        return path

    def test_shapes_are_native_and_text_editable(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = self._build_pptx(flowchart_graph, tmp_path / "diagram.pptx")
        prs = Presentation(str(path))  # reopen from disk
        slide = prs.slides[0]
        autoshapes = [
            s for s in slide.shapes if s.has_text_frame and not s.is_placeholder
        ]
        texts = {s.text_frame.text for s in autoshapes if s.text_frame.text}
        for node in flowchart_graph.nodes:
            assert node.text in texts  # Azerbaijani text survived round-trip

    def test_connectors_are_attached_to_shapes(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = self._build_pptx(flowchart_graph, tmp_path / "diagram.pptx")
        prs = Presentation(str(path))
        slide = prs.slides[0]
        connectors = [s for s in slide.shapes if isinstance(s, Connector)]
        # One connector per edge — the back-edge is a SINGLE bent
        # connector, not stitched segments.
        assert len(connectors) == len(flowchart_graph.edges)
        # Every connector is attached at BOTH ends (a:stCxn / a:endCxn).
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        for connector in connectors:
            assert connector._element.find(".//a:stCxn", ns) is not None
            assert connector._element.find(".//a:endCxn", ns) is not None

    def test_back_edge_is_single_bent_connector_with_rail_adj(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = self._build_pptx(flowchart_graph, tmp_path / "diagram.pptx")
        prs = Presentation(str(path))
        slide = prs.slides[0]
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        bent = [
            s
            for s in slide.shapes
            if isinstance(s, Connector)
            and s._element.find(".//a:prstGeom", ns).get("prst") == "bentConnector3"
            # back-edge: both ends on the shapes' right side (idx 3)
            and s._element.find(".//a:stCxn", ns).get("idx") == "3"
            and s._element.find(".//a:endCxn", ns).get("idx") == "3"
        ]
        assert len(bent) == 1
        gd = bent[0]._element.find(".//a:prstGeom/a:avLst/a:gd", ns)
        assert gd is not None and gd.get("name") == "adj1"
        # Ratio-based rail: negative places the vertical segment right of
        # the source, outside the endpoints' bounding box.
        assert int(gd.get("fmla").split()[1]) < 0

    def test_edge_labels_become_text_boxes(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = self._build_pptx(flowchart_graph, tmp_path / "diagram.pptx")
        prs = Presentation(str(path))
        slide = prs.slides[0]
        all_text = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        assert "Hə" in all_text
        assert "Yox" in all_text
