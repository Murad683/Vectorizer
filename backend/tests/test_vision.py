"""Vision provider tests. The Gemini API is always mocked — no real calls."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.core.vision.gemini_provider import GeminiVisionProvider, parse_diagram_graph
from app.models.diagram import ShapeType

_VALID_GRAPH_JSON = json.dumps(
    {
        "nodes": [
            {"id": "n1", "shape_type": "terminator", "text": "Başlanğıc"},
            {"id": "n2", "shape_type": "decision", "text": "a < 164"},
            {"id": "n3", "shape_type": "terminator", "text": "Son"},
        ],
        "edges": [
            {"source_id": "n1", "target_id": "n2", "label": ""},
            {"source_id": "n2", "target_id": "n3", "label": "Yox"},
        ],
    }
)


class TestParseDiagramGraph:
    def test_valid_json(self) -> None:
        graph = parse_diagram_graph(_VALID_GRAPH_JSON)
        assert graph is not None
        assert len(graph.nodes) == 3
        assert graph.nodes[0].text == "Başlanğıc"
        assert graph.nodes[1].shape_type == ShapeType.DECISION

    def test_empty_labels_normalised_to_none(self) -> None:
        graph = parse_diagram_graph(_VALID_GRAPH_JSON)
        assert graph is not None
        assert graph.edges[0].label is None
        assert graph.edges[1].label == "Yox"

    def test_garbage_returns_none(self) -> None:
        assert parse_diagram_graph("I cannot read this image.") is None

    def test_missing_edges_key_returns_none(self) -> None:
        bad = json.dumps(
            {"nodes": [{"id": "n1", "shape_type": "process", "text": "x"}]}
        )
        assert parse_diagram_graph(bad) is None

    def test_wrong_shape_type_returns_none(self) -> None:
        bad = json.dumps(
            {
                "nodes": [{"id": "n1", "shape_type": "circle", "text": "x"}],
                "edges": [],
            }
        )
        assert parse_diagram_graph(bad) is None

    def test_empty_node_list_returns_none(self) -> None:
        assert parse_diagram_graph('{"nodes": [], "edges": []}') is None


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> GeminiVisionProvider:
    """A GeminiVisionProvider with a fake API key and a mocked client."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    monkeypatch.setattr("app.core.vision.gemini_provider.genai.Client", MagicMock())
    built = GeminiVisionProvider()
    get_settings.cache_clear()
    return built


def _response_with_text(text: str) -> MagicMock:
    response = MagicMock()
    response.text = text
    return response


class TestGeminiVisionProvider:
    def test_successful_analysis(self, provider: GeminiVisionProvider) -> None:
        provider._client.models.generate_content.return_value = _response_with_text(
            _VALID_GRAPH_JSON
        )
        result = provider.analyze_diagram(b"png-bytes", context="80. sual")
        assert result.error is None
        assert result.diagram_graph is not None
        assert result.diagram_graph.nodes[0].text == "Başlanğıc"
        assert result.raw_model_output == _VALID_GRAPH_JSON

    def test_context_is_included_in_prompt(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.return_value = _response_with_text(
            _VALID_GRAPH_JSON
        )
        provider.analyze_diagram(b"png", context="question text here")
        kwargs = provider._client.models.generate_content.call_args.kwargs
        prompt_part = next(p for p in kwargs["contents"] if isinstance(p, str))
        assert "question text here" in prompt_part

    def test_garbage_response_sets_error(
        self,
        provider: GeminiVisionProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", MagicMock())
        provider._client.models.generate_content.return_value = _response_with_text(
            "Sorry, I can't."
        )
        result = provider.analyze_diagram(b"png-bytes")
        assert result.diagram_graph is None
        assert result.error is not None
        assert result.raw_model_output == "Sorry, I can't."
        # Invalid JSON is retried like a transient failure before giving up.
        assert provider._client.models.generate_content.call_count == 3

    def test_permanent_failure_sets_error_not_raise(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.side_effect = ValueError("bad request")
        result = provider.analyze_diagram(b"png-bytes")
        assert result.error is not None
        assert "ValueError" in result.error
        assert provider._client.models.generate_content.call_count == 1

    def test_transient_failure_is_retried(
        self,
        provider: GeminiVisionProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", MagicMock())
        provider._client.models.generate_content.side_effect = [
            ConnectionError("transient"),
            _response_with_text(_VALID_GRAPH_JSON),
        ]
        result = provider.analyze_diagram(b"png-bytes")
        assert result.error is None
        assert provider._client.models.generate_content.call_count == 2

    def test_exhausted_retries_set_error(
        self,
        provider: GeminiVisionProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", MagicMock())
        provider._client.models.generate_content.side_effect = ConnectionError("down")
        result = provider.analyze_diagram(b"png-bytes")
        assert result.error is not None
        assert "3 attempts" in result.error
        assert provider._client.models.generate_content.call_count == 3


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # hide any local .env file
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GeminiVisionProvider()
    finally:
        get_settings.cache_clear()
