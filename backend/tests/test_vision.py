"""Vision provider tests. The Gemini API is always mocked — no real calls."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.core.vision.gemini_provider import GeminiVisionProvider, parse_mermaid


class TestParseMermaid:
    def test_fenced_block(self) -> None:
        raw = "Here you go:\n```mermaid\nflowchart TD\n  A --> B\n```\nDone."
        assert parse_mermaid(raw) == "flowchart TD\n  A --> B"

    def test_bare_flowchart(self) -> None:
        assert parse_mermaid("flowchart TD\n  A --> B") == "flowchart TD\n  A --> B"

    def test_no_mermaid(self) -> None:
        assert parse_mermaid("I cannot read this image.") is None

    def test_empty_fence(self) -> None:
        assert parse_mermaid("```mermaid\n```") is None


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> GeminiVisionProvider:
    """A GeminiVisionProvider with a fake API key and a mocked model."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    monkeypatch.setattr("app.core.vision.gemini_provider.genai.configure", MagicMock())
    monkeypatch.setattr(
        "app.core.vision.gemini_provider.genai.GenerativeModel", MagicMock()
    )
    built = GeminiVisionProvider()
    get_settings.cache_clear()
    return built


def _response_with_text(text: str) -> MagicMock:
    response = MagicMock()
    response.text = text
    return response


class TestGeminiVisionProvider:
    def test_successful_analysis(self, provider: GeminiVisionProvider) -> None:
        provider._model.generate_content.return_value = _response_with_text(
            "```mermaid\nflowchart TD\n  A([Başlanğıc]) --> B\n```"
        )
        result = provider.analyze_diagram(b"png-bytes", context="80. sual")
        assert result.error is None
        assert result.mermaid_code is not None
        assert "Başlanğıc" in result.mermaid_code
        assert result.raw_model_output.startswith("```mermaid")

    def test_context_is_included_in_prompt(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._model.generate_content.return_value = _response_with_text(
            "flowchart TD\n A-->B"
        )
        provider.analyze_diagram(b"png", context="question text here")
        parts = provider._model.generate_content.call_args.args[0]
        assert "question text here" in parts[0]

    def test_non_mermaid_response_sets_error(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._model.generate_content.return_value = _response_with_text(
            "Sorry, I can't."
        )
        result = provider.analyze_diagram(b"png-bytes")
        assert result.mermaid_code is None
        assert result.error is not None
        assert result.raw_model_output == "Sorry, I can't."

    def test_permanent_failure_sets_error_not_raise(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._model.generate_content.side_effect = ValueError("bad request")
        result = provider.analyze_diagram(b"png-bytes")
        assert result.error is not None
        assert "ValueError" in result.error

    def test_transient_failure_is_retried(
        self,
        provider: GeminiVisionProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", MagicMock())
        provider._model.generate_content.side_effect = [
            ConnectionError("transient"),
            _response_with_text("flowchart TD\n  A --> B"),
        ]
        result = provider.analyze_diagram(b"png-bytes")
        assert result.error is None
        assert provider._model.generate_content.call_count == 2

    def test_exhausted_retries_set_error(
        self,
        provider: GeminiVisionProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", MagicMock())
        provider._model.generate_content.side_effect = ConnectionError("down")
        result = provider.analyze_diagram(b"png-bytes")
        assert result.error is not None
        assert "3 attempts" in result.error
        assert provider._model.generate_content.call_count == 3


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # hide any local .env file
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GeminiVisionProvider()
    finally:
        get_settings.cache_clear()
