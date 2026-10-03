"""Tests for native DOCX shape generation (single wpc:wpc canvas)."""

import zipfile
from pathlib import Path

import docx
from docx.shared import Emu
from lxml import etree

from app.core.layout import layout_graph
from app.core.render.docx_shapes import add_diagram_to_docx
from app.models.diagram import DiagramEdge, DiagramGraph, DiagramNode, ShapeType

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
        # rect: 3 process nodes + 2 borderless label boxes (Hə, Yox).
        assert presets.count("rect") == 5
        # INPUT_OUTPUT nodes (n, n) use a:custGeom, not the "parallelogram"
        # preset — see the Bug 5 fix in parallelogram_geometry_xml().
        assert presets.count("parallelogram") == 0
        assert len(body.findall(".//wps:wsp/wps:spPr/a:custGeom", _NS)) == 2

    def test_parallelogram_connection_points_are_bbox_centered(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """Bug 5: top/bottom cxn points must sit at true bounding-box
        center, not follow the shape's slanted top/bottom edges."""
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        for cust_geom in body.findall(".//wps:wsp/wps:spPr/a:custGeom", _NS):
            cxns = cust_geom.findall("a:cxnLst/a:cxn/a:pos", _NS)
            assert len(cxns) == 4
            width = int(cust_geom.find("a:rect", _NS).get("r"))
            top_x = int(cxns[0].get("x"))
            bottom_x = int(cxns[2].get("x"))
            assert top_x == bottom_x == width // 2

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

    def test_back_edge_is_glued_and_routed_from_graphviz_path(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """Phase 24: back-edges are no longer forced onto a fixed
        right-side "rail" — they route through the same
        choose_connector_from_path() pipeline as forward edges, using
        Graphviz's own collision-free path. Connection sites and preset
        now depend on actual geometry rather than being hardcoded to
        idx=3 (right) on both ends."""
        layouted = layout_graph(flowchart_graph)
        assert sum(e.is_back_edge for e in layouted.edges) == 1
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        back_edges = [
            w
            for w in body.findall(".//wps:wsp", _NS)
            if w.find("wps:cNvPr", _NS).get("name", "").startswith("backedge")
        ]
        assert len(back_edges) == 1
        back = back_edges[0]
        assert back.find("wps:cNvCnPr/a:stCxn", _NS) is not None
        assert back.find("wps:cNvCnPr/a:endCxn", _NS) is not None

    def test_edge_label_does_not_overlap_source_decision(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """A branch label ("Hə"/"Yox") must not overlap the source
        decision's bounding box (full 2D check)."""
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        decision_shape = next(
            s
            for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
            if s.find("wps:cNvPr", _NS).get("name") == "node-n4"
        )
        d_off = decision_shape.find("wps:spPr/a:xfrm/a:off", _NS)
        d_ext = decision_shape.find("wps:spPr/a:xfrm/a:ext", _NS)
        d_left = int(d_off.get("x"))
        d_top = int(d_off.get("y"))
        d_right = d_left + int(d_ext.get("cx"))
        d_bottom = d_top + int(d_ext.get("cy"))

        for label_text in ("Hə", "Yox"):
            label_wsp = next(
                s
                for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
                if s.find("wps:cNvPr", _NS).get("name", "").startswith("label-")
                and any(t.text == label_text for t in s.findall(".//w:t", _NS))
            )
            l_off = label_wsp.find("wps:spPr/a:xfrm/a:off", _NS)
            l_ext = label_wsp.find("wps:spPr/a:xfrm/a:ext", _NS)
            l_left = int(l_off.get("x"))
            l_top = int(l_off.get("y"))
            l_right = l_left + int(l_ext.get("cx"))
            l_bottom = l_top + int(l_ext.get("cy"))
            no_overlap = (
                l_right <= d_left
                or l_left >= d_right
                or l_bottom <= d_top
                or l_top >= d_bottom
            )
            assert no_overlap, (
                f"Label '{label_text}' overlaps decision shape"
            )

    def test_unique_cnvpr_ids(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "diagram.docx")
        body = _body(path)
        ids = [c.get("id") for c in body.findall(".//wps:cNvPr", _NS)]
        assert len(ids) == len(set(ids))
        assert "1" not in ids  # 1 is reserved for the anchor's wp:docPr


class TestBackEdgeDetourDocx:
    """Phase 24: back-edges route through Graphviz's own collision-free
    path (same pipeline as forward edges) — no freeform detours needed."""

    def test_all_back_edges_glued_no_detour(
        self, collision_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """Both back-edges are native glued connectors, no detour
        polyline needed."""
        path = _build_docx(collision_graph, tmp_path / "detour.docx")
        body = _body(path)
        detours = [
            w
            for w in body.findall(".//wps:wsp", _NS)
            if w.find("wps:cNvPr", _NS)
            .get("name", "")
            .startswith("backedge-detour")
        ]
        backs = [
            w
            for w in body.findall(".//wps:wsp", _NS)
            if w.find("wps:cNvPr", _NS).get("name", "").startswith("backedge-")
        ]
        assert len(detours) == 0
        assert len(backs) == 2
        for back in backs:
            assert back.find("wps:cNvCnPr/a:stCxn", _NS) is not None
            assert back.find("wps:cNvCnPr/a:endCxn", _NS) is not None

    def test_detour_valid_xml(
        self, collision_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        import zipfile

        path = _build_docx(collision_graph, tmp_path / "detour.docx")
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".xml", ".rels")):
                    etree.fromstring(zf.read(name))

    def test_unique_ids(
        self, collision_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(collision_graph, tmp_path / "detour.docx")
        body = _body(path)
        ids = [c.get("id") for c in body.findall(".//wps:cNvPr", _NS)]
        assert len(ids) == len(set(ids))


class TestStructuralVerificationDocx:
    """Phase 19 / Symptom 4: verify that the rendered DOCX's graph
    structure (nodes, edges, labels) exactly matches the input graph."""

    def test_all_node_texts_present(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "verify.docx")
        body = _body(path)
        texts = {t.text for t in body.findall(".//wps:txbx//w:t", _NS) if t.text}
        for node in flowchart_graph.nodes:
            assert node.text in texts, f"Node '{node.text}' missing from DOCX"

    def test_correct_number_of_connectors(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "verify.docx")
        body = _body(path)
        connectors = body.findall(".//wps:wsp[wps:cNvCnPr]", _NS)
        assert len(connectors) == len(flowchart_graph.edges)

    def test_all_connectors_glued_at_both_ends(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "verify.docx")
        body = _body(path)
        connectors = body.findall(".//wps:wsp[wps:cNvCnPr]", _NS)
        shape_ids = {
            s.find("wps:cNvPr", _NS).get("id")
            for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS)
        }
        for conn in connectors:
            st = conn.find("wps:cNvCnPr/a:stCxn", _NS)
            en = conn.find("wps:cNvCnPr/a:endCxn", _NS)
            assert st is not None, "connector missing stCxn"
            assert en is not None, "connector missing endCxn"
            assert st.get("id") in shape_ids, "stCxn references unknown shape"
            assert en.get("id") in shape_ids, "endCxn references unknown shape"

    def test_all_edge_labels_present(
        self, flowchart_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        path = _build_docx(flowchart_graph, tmp_path / "verify.docx")
        body = _body(path)
        texts = {t.text for t in body.findall(".//wps:txbx//w:t", _NS) if t.text}
        for edge in flowchart_graph.edges:
            if edge.label:
                assert edge.label in texts, f"Edge label '{edge.label}' missing"

    def test_collision_graph_structure_complete(
        self, collision_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """Verify the collision_graph renders with correct node count,
        edge count, and all labels."""
        path = _build_docx(collision_graph, tmp_path / "verify_coll.docx")
        body = _body(path)
        texts = {t.text for t in body.findall(".//wps:txbx//w:t", _NS) if t.text}
        for node in collision_graph.nodes:
            assert node.text in texts, f"Node '{node.text}' missing"
        connectors = body.findall(".//wps:wsp[wps:cNvCnPr]", _NS)
        assert len(connectors) == len(collision_graph.edges)
        for edge in collision_graph.edges:
            if edge.label:
                assert edge.label in texts, f"Label '{edge.label}' missing"


class TestLabelPositioningDocx:
    """Phase 20: edge labels must never overlap each other, and must sit
    above (not on) the connector line. DOCX parity tests."""

    def test_two_sibling_labels_distinct_positions(
        self, collision_graph: DiagramGraph, tmp_path: Path
    ) -> None:
        """n4's two labeled edges (Hə→n5, Yox→n6) must produce labels
        at different positions in the DOCX output."""
        path = _build_docx(collision_graph, tmp_path / "labels.docx")
        body = _body(path)
        label_positions: dict[str, tuple[int, int]] = {}
        for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS):
            name = s.find("wps:cNvPr", _NS).get("name", "")
            if not name.startswith("label-"):
                continue
            text_el = s.find(".//w:t", _NS)
            if text_el is None or text_el.text not in ("Hə", "Yox"):
                continue
            off = s.find("wps:spPr/a:xfrm/a:off", _NS)
            label_positions[text_el.text] = (
                int(off.get("x")),
                int(off.get("y")),
            )
        assert "Hə" in label_positions and "Yox" in label_positions
        assert label_positions["Hə"] != label_positions["Yox"], (
            f"Labels at identical position: {label_positions['Hə']}"
        )

    def test_three_sibling_labels_no_overlap(self, tmp_path: Path) -> None:
        """A decision with 3 labeled edges — all labels must be mutually
        non-overlapping in the DOCX output."""
        graph = DiagramGraph(
            nodes=[
                DiagramNode(id="s", shape_type=ShapeType.TERMINATOR, text="Start"),
                DiagramNode(id="d", shape_type=ShapeType.DECISION, text="x?"),
                DiagramNode(id="a", shape_type=ShapeType.PROCESS, text="A"),
                DiagramNode(id="b", shape_type=ShapeType.PROCESS, text="B"),
                DiagramNode(id="c", shape_type=ShapeType.PROCESS, text="C"),
            ],
            edges=[
                DiagramEdge(source_id="s", target_id="d"),
                DiagramEdge(source_id="d", target_id="a", label="L1"),
                DiagramEdge(source_id="d", target_id="b", label="L2"),
                DiagramEdge(source_id="d", target_id="c", label="L3"),
            ],
        )
        path = _build_docx(graph, tmp_path / "three_labels.docx")
        body = _body(path)
        label_boxes: list[tuple[int, int, int, int]] = []
        for s in body.findall(".//wps:wsp[wps:cNvSpPr]", _NS):
            name = s.find("wps:cNvPr", _NS).get("name", "")
            if not name.startswith("label-"):
                continue
            off = s.find("wps:spPr/a:xfrm/a:off", _NS)
            ext = s.find("wps:spPr/a:xfrm/a:ext", _NS)
            x, y = int(off.get("x")), int(off.get("y"))
            cx, cy = int(ext.get("cx")), int(ext.get("cy"))
            label_boxes.append((x, y, x + cx, y + cy))
        assert len(label_boxes) == 3
        for i in range(len(label_boxes)):
            for j in range(i + 1, len(label_boxes)):
                a = label_boxes[i]
                b = label_boxes[j]
                overlaps = not (
                    a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
                )
                assert not overlaps, f"Labels {i} and {j} overlap: {a} vs {b}"

    def test_labeled_edges_have_graphviz_coordinates(
        self, collision_graph: DiagramGraph
    ) -> None:
        """After layout, every labeled edge must have label_x/label_y
        set by Graphviz so the DOCX renderer can place them."""
        layouted = layout_graph(collision_graph)
        for edge in layouted.edges:
            if edge.label:
                assert edge.label_x is not None, (
                    f"Edge {edge.source_id}->{edge.target_id} missing label_x"
                )
                assert edge.label_y is not None, (
                    f"Edge {edge.source_id}->{edge.target_id} missing label_y"
                )
