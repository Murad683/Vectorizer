"""Each diagram image costs ONE vision call per job (offline, no API).

Found in the debug dumps: every uploaded image produced two dump folders a
few seconds apart, because the worker built the DOCX and the PPTX with two
independent assemblers and each one called Gemini for every image. A
structural retry was then paid twice as well.
"""

from pathlib import Path

import pytest

from app.config import get_settings
from app.core.queue.worker import _process_job_sync
from app.core.vision.base import VisionProvider
from app.core.vision.cache import CachedVisionProvider
from app.core.vision.postprocess import topology_problems
from app.models.diagram import DiagramEdge, DiagramGraph, DiagramNode, ShapeType
from app.models.document import ContentType, VisionResult
from tests.diagram_fixtures import SAMPLES, run_fixture


class _Counting(VisionProvider):
    """Fake provider that answers with a fixed result and counts calls."""

    def __init__(self, result: VisionResult) -> None:
        self.result = result
        self.calls = 0

    def analyze_diagram(self, image: bytes, context: str | None = None) -> VisionResult:
        self.calls += 1
        return self.result.model_copy(deep=True)


def _diagram_result() -> VisionResult:
    graph, _ = run_fixture("sum_loop")
    return VisionResult(content_type=ContentType.DIAGRAM, diagram_graph=graph)


def test_same_image_is_analysed_once() -> None:
    inner = _Counting(_diagram_result())
    cached = CachedVisionProvider(inner)
    first = cached.analyze_diagram(b"image-a", context="docx context")
    second = cached.analyze_diagram(b"image-a", context="pptx context")
    cached.analyze_diagram(b"image-b")
    assert inner.calls == 2 and cached.calls == 2
    # Independent copies: one assembler cannot change the other's input.
    assert first.diagram_graph is not None and second.diagram_graph is not None
    first.diagram_graph.nodes[0].text = "changed"
    assert second.diagram_graph.nodes[0].text == "Başlanğıc"


def test_failed_analysis_is_not_paid_for_twice() -> None:
    inner = _Counting(VisionResult(error="model gave up"))
    cached = CachedVisionProvider(inner)
    assert cached.analyze_diagram(b"x").error == "model gave up"
    assert cached.analyze_diagram(b"x").error == "model gave up"
    assert inner.calls == 1


def test_no_provider_is_created_without_a_diagram() -> None:
    def factory() -> VisionProvider:
        raise AssertionError("no diagram, so no API key is needed")

    CachedVisionProvider(factory=factory)  # constructing must not create it


def test_worker_makes_one_call_per_image_for_docx_and_pptx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inner = _Counting(_diagram_result())
    monkeypatch.setattr("app.core.vision.get_vision_provider", lambda: inner)
    docx_path, pptx_path = _process_job_sync(
        str(SAMPLES / "sum_loop.jpg"), tmp_path / "job"
    )
    assert Path(docx_path).exists() and Path(pptx_path).exists()
    assert inner.calls == 1  # was 2: one per output format


def test_fragment_may_end_at_its_last_block() -> None:
    """Real test question 129 (dump 9cbf59d7): no Başlanğıc/Son, the drawing
    stops at the print block "S". The old rule called that incomplete and
    paid for a corrective retry (twice) that changed nothing."""

    def node(nid: str, shape: ShapeType, text: str) -> DiagramNode:
        return DiagramNode(id=nid, shape_type=shape, text=text)

    def edge(s: str, t: str, label: str | None = None) -> DiagramEdge:
        return DiagramEdge(source_id=s, target_id=t, label=label)

    graph = DiagramGraph(
        nodes=[
            node("n1", ShapeType.PROCESS, "S=2"),
            node("n2", ShapeType.PROCESS, "M=3"),
            node("n3", ShapeType.DECISION, "M>S"),
            node("n4", ShapeType.PROCESS, "S=S-M"),
            node("n5", ShapeType.DOCUMENT, "S"),
            node("n6", ShapeType.PROCESS, "M=M+7"),
            node("n7", ShapeType.PROCESS, "S=S+S*M"),
        ],
        edges=[
            edge("n1", "n2"),
            edge("n2", "n3"),
            edge("n3", "n4", "yox"),
            edge("n4", "n5"),
            edge("n3", "n7", "hə"),
            edge("n7", "n6"),
            edge("n6", "n3"),
        ],
    )
    assert topology_problems(graph) == []

    # Two dead ends in a fragment still mean a missing arrow.
    graph.edges = [e for e in graph.edges if e.source_id != "n6"]
    problems = topology_problems(graph)
    assert any("(n6)" in p and "0 outgoing" in p for p in problems)


def test_with_son_every_block_still_needs_its_arrow() -> None:
    graph = DiagramGraph(
        nodes=[
            DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Başlanğıc"),
            DiagramNode(id="n2", shape_type=ShapeType.PROCESS, text="a = 1"),
            DiagramNode(id="n3", shape_type=ShapeType.DOCUMENT, text="a"),
            DiagramNode(id="n4", shape_type=ShapeType.TERMINATOR, text="Son"),
        ],
        edges=[
            DiagramEdge(source_id="n1", target_id="n2"),
            DiagramEdge(source_id="n2", target_id="n4"),
            DiagramEdge(source_id="n1", target_id="n3"),
        ],
    )
    assert any("(n3)" in p for p in topology_problems(graph))


def test_tests_do_not_write_real_dumps() -> None:
    assert get_settings().debug_dump_dir == ""
