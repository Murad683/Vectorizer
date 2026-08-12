"""Tests for native DOCX shape generation (single wpc:wpc canvas)."""

import zipfile
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
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
}

# ST_RelFromH does NOT include "paragraph" — writing it there is what
# broke the Phase 4 output. Keep the legal value sets pinned in tests.
_VALID_REL_H = {
    "character",
    "column",
    "insideMargin",
    "leftMargin",
    "margin",
    "outsideMargin",
    "page",
    "rightMargin",
}
_VALID_REL_V = {
    "bottomMargin",
    "insideMargin",
    "line",
    "margin",
    "outsideMargin",
    "page",
    "paragraph",
    "topMargin",
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


class TestDocxCanvas:
    def test_every_package_part_parses(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """Every XML part of the archive must be valid XML (lxml strict)."""
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".xml", ".rels")):
                    etree.fromstring(zf.read(name))  # raises on any error

    def test_single_anchor_with_single_canvas(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        anchors = body.findall(".//wp:anchor", _NS)
        assert len(anchors) == 1
        canvases = body.findall(".//wpc:wpc", _NS)
        assert len(canvases) == 1
        # All shapes live inside the canvas, none outside.
        assert len(body.findall(".//wpc:wpc//wps:wsp", _NS)) == len(
            body.findall(".//wps:wsp", _NS)
        )

    def test_relative_from_values_are_schema_legal(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        for variant, expect_h, expect_v in (
            ("paragraph", "column", "paragraph"),
            ("page", "page", "page"),
        ):
            path = _build_docx(
                flowchart_graph,
                tmp_path / f"diagram_{variant}.docx",
                relative_from=variant,
            )
            anchor = _body(path).find(".//wp:anchor", _NS)
            rel_h = anchor.find("wp:positionH", _NS).get("relativeFrom")
            rel_v = anchor.find("wp:positionV", _NS).get("relativeFrom")
            assert rel_h == expect_h and rel_h in _VALID_REL_H
            assert rel_v == expect_v and rel_v in _VALID_REL_V

    def test_every_node_is_a_native_wsp_with_correct_preset(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        presets = [
            g.get("prst") for g in body.findall(".//wps:wsp/wps:spPr/a:prstGeom", _NS)
        ]
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

    def test_connectors_are_attached_via_cxn(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        connectors = body.findall(".//wps:wsp[wps:cNvCnPr]", _NS)
        # One connector per edge — back-edge is a single bent connector.
        assert len(connectors) == 8
        shape_ids = {
            s.find("wps:cNvPr", _NS).get("id")
            for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
        }
        # EVERY connector is attached at both ends.
        for connector in connectors:
            st = connector.find("wps:cNvCnPr/a:stCxn", _NS)
            en = connector.find("wps:cNvCnPr/a:endCxn", _NS)
            assert st is not None and en is not None
            assert st.get("id") in shape_ids
            assert en.get("id") in shape_ids

    def test_back_edge_is_single_bent_connector_clearing_shapes(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        back_edges = [
            w
            for w in body.findall(".//wps:wsp", _NS)
            if w.find("wps:cNvPr", _NS).get("name", "").startswith("backedge")
        ]
        # A SINGLE bent connector, attached at both ends on the right side.
        assert len(back_edges) == 1
        back = back_edges[0]
        assert back.find(".//a:prstGeom", _NS).get("prst") == "bentConnector3"
        assert back.find("wps:cNvCnPr/a:stCxn", _NS).get("idx") == "3"
        assert back.find("wps:cNvCnPr/a:endCxn", _NS).get("idx") == "3"
        # adj1 is ratio-based and negative: the vertical rail lies outside
        # the endpoints' bounding box (right of the source shape). Verify
        # the resulting absolute rail x clears every node shape.
        gd = back.find(".//a:prstGeom/a:avLst/a:gd", _NS)
        assert gd.get("name") == "adj1"
        adj = int(gd.get("fmla").split()[1])
        assert adj < 0
        xfrm_off = back.find(".//a:off", _NS)
        xfrm_ext = back.find(".//a:ext", _NS)
        x, cx = int(xfrm_off.get("x")), int(xfrm_ext.get("cx"))
        # flipH: local x=adj*cx maps to real x = right_edge - adj*cx.
        rail_x = (x + cx) - adj / 100000 * cx
        node_rights = [
            int(s.find(".//a:off", _NS).get("x"))
            + int(s.find(".//a:ext", _NS).get("cx"))
            for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
            if s.find("wps:cNvPr", _NS).get("name", "").startswith("node-")
        ]
        assert rail_x > max(node_rights)

    def test_unique_cnvpr_ids(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        ids = [c.get("id") for c in body.findall(".//wps:cNvPr", _NS)]
        assert len(ids) == len(set(ids))
        assert "1" not in ids  # 1 is reserved for the anchor's wp:docPr
