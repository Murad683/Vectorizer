"""Tests for native DOCX shape generation (hand-built DrawingML)."""

from pathlib import Path

import docx
from docx.shared import Emu
from lxml import etree

from app.core.layout import layout_graph
from app.core.render.docx_shapes import add_diagram_to_docx
from app.models.diagram import DiagramGraph

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": ("http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"),
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
}


def _build_docx(flowchart_graph: DiagramGraph, path: Path, **kwargs: str) -> Path:
    layouted = layout_graph(flowchart_graph)
    document = docx.Document()
    add_diagram_to_docx(
        layouted,
        document,
        offset_x_emu=Emu(0),
        offset_y_emu=Emu(0),
        max_width_emu=Emu(int(6.5 * 914400)),
        max_height_emu=Emu(int(8.5 * 914400)),
        **kwargs,  # type: ignore[arg-type]
    )
    document.save(str(path))
    return path


def _body(path: Path) -> etree._Element:
    return docx.Document(str(path)).element.body


class TestDocxShapes:
    def test_document_round_trips_and_parses(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)  # python-docx re-parses document.xml (lxml strict)
        assert body is not None

    def test_every_node_is_a_native_wsp_with_correct_preset(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        presets = [
            g.get("prst") for g in body.findall(".//wps:wsp/wps:spPr/a:prstGeom", _NS)
        ]
        # 8 node shapes with the mapped presets.
        assert presets.count("roundRect") == 2  # Başlanğıc, Son
        assert presets.count("diamond") == 1  # a < 164
        assert presets.count("parallelogram") == 2  # n, n
        # rect: 3 process nodes + 2 borderless label boxes (Hə, Yox).
        assert presets.count("rect") == 5

    def test_shape_text_is_real_editable_text(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        texts = {t.text for t in body.findall(".//wps:txbx//w:t", _NS) if t.text}
        for node in flowchart_graph.nodes:
            assert node.text in texts  # Azerbaijani preserved in w:t runs
        assert "Hə" in texts and "Yox" in texts

    def test_connector_counts_and_back_edge_rail(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        connectors = body.findall(".//wps:wsp[wps:cNvCnPr]", _NS)
        # 7 forward edges + 3 back-edge rail segments.
        assert len(connectors) == 10
        lines = [
            c
            for c in connectors
            if c.find("wps:spPr/a:prstGeom", _NS).get("prst") == "line"
        ]
        bends = [
            c
            for c in connectors
            if c.find("wps:spPr/a:prstGeom", _NS).get("prst") == "bentConnector2"
        ]
        # 5 same-column straights + 3 rail segments; 2 branch bends.
        assert len(lines) == 8
        assert len(bends) == 2

    def test_all_anchors_share_one_paragraph_and_relative_from(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        paragraphs_with_drawings = [
            p for p in body.findall("w:p", _NS) if p.findall(".//wp:anchor", _NS)
        ]
        assert len(paragraphs_with_drawings) == 1
        anchors = body.findall(".//wp:anchor", _NS)
        for anchor in anchors:
            for tag in ("wp:positionH", "wp:positionV"):
                assert anchor.find(tag, _NS).get("relativeFrom") == "paragraph"

    def test_page_relative_variant(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(
            flowchart_graph, tmp_path / "diagram_page.docx", relative_from="page"
        )
        body = _body(path)
        anchors = body.findall(".//wp:anchor", _NS)
        assert anchors
        for anchor in anchors:
            assert anchor.find("wp:positionH", _NS).get("relativeFrom") == "page"

    def test_unique_doc_pr_ids(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        ids = [d.get("id") for d in body.findall(".//wp:docPr", _NS)]
        assert len(ids) == len(set(ids))
