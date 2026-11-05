"""A page answer that goes wrong: order, vanished text, looping, crops, markup.

Offline: the Gemini client is a mock, every text is invented.
"""

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.config import get_settings
from app.core.assembly.page_blocks import crop_figure, runs_to_dicts
from app.core.vision.gemini_provider import GeminiVisionProvider
from app.core.vision.postprocess import clean_page, empty_text_blocks, order_spread
from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun


def _png(size: int = 1000) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (size, size), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _block(text: str, y0: int, x0: int, y1: int, x1: int) -> PageBlock:
    return PageBlock(
        kind=BlockKind.TEXT,
        style=TextStyle.PARAGRAPH,
        runs=[TextRun(text=text)],
        box_2d=[y0, x0, y1, x1],
    )


def _column(prefix: str, x0: int, x1: int, count: int = 4) -> list[PageBlock]:
    return [
        _block(f"{prefix}{i}", 100 + 60 * i, x0, 140 + 60 * i, x1) for i in range(count)
    ]


def _texts(blocks: list[PageBlock]) -> list[str]:
    return [b.text for b in blocks]


class TestOrderSpread:
    def test_right_page_first_becomes_left_page_first(self) -> None:
        left, right = _column("L", 130, 450), _column("R", 480, 800)
        assert _texts(order_spread(right + left)) == _texts(left + right)

    def test_the_models_order_inside_each_page_is_kept(self) -> None:
        left, right = _column("L", 130, 450), _column("R", 480, 800)
        mixed = [right[0], left[0], right[1], left[1], right[2], left[2], right[3]]
        assert _texts(order_spread(mixed)) == [
            "L0",
            "L1",
            "L2",
            "R0",
            "R1",
            "R2",
            "R3",
        ]

    def test_left_first_already_is_untouched(self) -> None:
        blocks = _column("L", 130, 450) + _column("R", 480, 800)
        assert _texts(order_spread(blocks)) == _texts(blocks)

    def test_a_single_column_is_untouched(self) -> None:
        blocks = _column("A", 130, 800, count=8)[::-1]
        assert order_spread(blocks) is blocks

    def test_a_heading_across_both_pages_means_no_gutter(self) -> None:
        heading = _block("H", 40, 130, 70, 800)
        blocks = [*_column("R", 480, 800), *_column("L", 130, 450), heading]
        assert order_spread(blocks) is blocks

    def test_an_unbalanced_page_is_untouched(self) -> None:
        blocks = [*_column("R", 480, 800, count=2), *_column("L", 130, 450, count=6)]
        assert order_spread(blocks) is blocks

    def test_few_blocks_are_untouched(self) -> None:
        blocks = [_block("R", 100, 480, 140, 800), _block("L", 100, 130, 140, 450)]
        assert order_spread(blocks) is blocks

    def test_a_block_without_a_box_is_untouched(self) -> None:
        blocks = [*_column("R", 480, 800), *_column("L", 130, 450)]
        blocks[0].box_2d = None
        assert order_spread(blocks) is blocks

    def test_clean_page_applies_it(self) -> None:
        page = PageAnalysis(blocks=_column("R", 480, 800) + _column("L", 130, 450))
        assert _texts(clean_page(page).blocks)[:4] == ["L0", "L1", "L2", "L3"]


class TestEmptyTextBlocks:
    @staticmethod
    def _raw(texts: list[str | None]) -> str:
        blocks = [
            (
                {"kind": "text", "box_2d": [1, 1, 2, 2]}
                if t is None
                else {"kind": "text", "runs": [{"text": t}], "box_2d": [1, 1, 2, 2]}
            )
            for t in texts
        ]
        blocks.append({"kind": "figure", "box_2d": [5, 5, 9, 9]})
        return json.dumps({"content_type": "page", "page": {"blocks": blocks}})

    def test_counts_blocks_without_text(self) -> None:
        assert empty_text_blocks(self._raw(["a", None, "b", None, None])) == (3, 5)

    def test_blank_runs_count_as_empty(self) -> None:
        assert empty_text_blocks(self._raw(["a", ""])) == (1, 2)

    def test_garbage_is_zero(self) -> None:
        assert empty_text_blocks("not json") == (0, 0)
        assert empty_text_blocks('{"page": null}') == (0, 0)
        assert empty_text_blocks("[]") == (0, 0)


_IMAGE = _png(64)


def _reply(payload: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.text = json.dumps(payload)
    response.candidates = []
    return response


def _page(texts: list[str | None]) -> dict[str, Any]:
    blocks = [
        {
            "kind": "text",
            "style": "paragraph",
            "box_2d": [20 + 30 * i, 100, 40 + 30 * i, 700],
            **({} if t is None else {"runs": [{"text": t}]}),
        }
        for i, t in enumerate(texts)
    ]
    return {"content_type": "page", "page": {"blocks": blocks}}


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> GeminiVisionProvider:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    monkeypatch.setattr("app.core.vision.gemini_provider.genai.Client", MagicMock())
    monkeypatch.setattr("app.core.vision.gemini_provider.detect", lambda _image: None)
    monkeypatch.setattr("app.core.vision.gemini_provider.time.sleep", lambda _s: None)
    return GeminiVisionProvider()


def _calls(provider: GeminiVisionProvider) -> int:
    return int(provider._client.models.generate_content.call_count)


class TestVanishedText:
    def test_many_empty_blocks_are_asked_again_and_the_better_answer_wins(
        self, provider: GeminiVisionProvider
    ) -> None:
        full = [f"Sual {i}" for i in range(10)]
        provider._client.models.generate_content.side_effect = [
            _reply(_page(["Sual 0", None, None, None, "Sual 4", "Sual 5"])),
            _reply(_page(full)),
        ]
        result = provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 2
        assert result.page is not None
        assert [b.text for b in result.page.blocks] == full

    def test_a_single_empty_block_is_not_worth_a_request(
        self, provider: GeminiVisionProvider
    ) -> None:
        texts: list[str | None] = [f"Sual {i}" for i in range(24)]
        texts[3] = None
        provider._client.models.generate_content.return_value = _reply(_page(texts))
        provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 1

    def test_a_complete_answer_costs_one_request(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.return_value = _reply(
            _page(["A", "B", "C"])
        )
        provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 1

    def test_still_incomplete_is_kept_and_logged(
        self, provider: GeminiVisionProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        bad = _page(["A", None, None, "D"])
        provider._client.models.generate_content.side_effect = [
            _reply(bad),
            _reply(bad),
        ]
        with caplog.at_level(logging.WARNING):
            result = provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 2
        assert result.page is not None and len(result.page.blocks) == 2
        assert "page_incomplete empty=2 of 4" in caplog.text


def _cut_reply() -> MagicMock:
    response = MagicMock()
    response.text = '{"content_type": "page", "page": {"blocks": [{"kind": "te'
    response.candidates = [SimpleNamespace(finish_reason="FinishReason.MAX_TOKENS")]
    return response


@dataclass
class _Detection:
    """Stands in for ``Detection``: the debug dump needs a dataclass."""

    shapes: list[int]


def _detection(shapes: int) -> _Detection:
    return _Detection(shapes=list(range(shapes)))


class TestAnswerCutAtTheCeiling:
    def test_a_cut_answer_is_not_asked_three_times(
        self, provider: GeminiVisionProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider._client.models.generate_content.return_value = _cut_reply()
        result = provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 1
        assert result.page is None and result.error
        assert "ceiling" in result.error

    def test_a_cut_page_with_symbols_gets_one_old_prompt_try(
        self, provider: GeminiVisionProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.core.vision.gemini_provider.detect", lambda _image: _detection(3)
        )
        monkeypatch.setattr(
            "app.core.vision.gemini_provider.annotate", lambda *_a: _png(64)
        )
        provider._client.models.generate_content.return_value = _cut_reply()
        seen = []

        def rescue(page: PageAnalysis, *_a: object) -> PageAnalysis:
            seen.append(page)
            return PageAnalysis(blocks=[_block("Xilas", 10, 10, 30, 300)])

        monkeypatch.setattr(provider, "_rescue_flowchart", rescue)
        result = provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 1  # the rescue itself is mocked
        assert len(seen) == 1 and seen[0].blocks == []
        assert result.page is not None and result.page.blocks[0].text == "Xilas"

    def test_a_cut_page_with_few_symbols_gives_up(
        self, provider: GeminiVisionProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.core.vision.gemini_provider.detect", lambda _image: _detection(2)
        )
        monkeypatch.setattr(
            "app.core.vision.gemini_provider.annotate", lambda *_a: _png(64)
        )
        provider._client.models.generate_content.return_value = _cut_reply()
        monkeypatch.setattr(
            provider, "_rescue_flowchart", MagicMock(side_effect=AssertionError)
        )
        assert provider.analyze_diagram(_IMAGE, page=True).error

    def test_broken_json_that_was_not_cut_still_gets_three_attempts(
        self, provider: GeminiVisionProvider
    ) -> None:
        broken = MagicMock()
        broken.text = "{ not json"
        broken.candidates = []
        provider._client.models.generate_content.return_value = broken
        provider.analyze_diagram(_IMAGE, page=True)
        assert _calls(provider) == 3

    def test_the_old_single_diagram_request_has_the_ceiling_too(
        self, provider: GeminiVisionProvider
    ) -> None:
        assert provider._config.max_output_tokens == 12000
        assert provider._config.temperature is None


def _size(png: bytes | None) -> tuple[int, int]:
    assert png is not None
    with Image.open(BytesIO(png)) as img:
        return img.size


class TestFigureCrop:
    BOX = [400, 300, 600, 700]  # ymin, xmin, ymax, xmax

    def test_without_neighbours_the_usual_padding(self) -> None:
        assert _size(crop_figure(_png(), self.BOX)) == (424, 224)

    def test_padding_stops_at_a_text_line_just_above(self) -> None:
        above = [370, 300, 395, 700]  # five units of gap
        assert _size(crop_figure(_png(), self.BOX, [above])) == (424, 217)

    def test_padding_stops_at_a_text_line_just_below(self) -> None:
        below = [604, 300, 640, 700]  # four units of gap
        assert _size(crop_figure(_png(), self.BOX, [below])) == (424, 216)

    def test_a_neighbour_at_the_side_stops_the_side_padding(self) -> None:
        left = [380, 100, 620, 297]  # three units of gap
        assert _size(crop_figure(_png(), self.BOX, [left])) == (415, 224)

    def test_a_far_or_unrelated_block_changes_nothing(self) -> None:
        far = [100, 300, 150, 700]
        beside_but_not_aligned = [10, 0, 30, 100]
        assert _size(crop_figure(_png(), self.BOX, [far, beside_but_not_aligned])) == (
            424,
            224,
        )


class TestMarkupInRuns:
    def test_superscript_markup_becomes_a_superscript_run(self) -> None:
        runs = [TextRun(text="(n!)^{2}-9n!+18=0")]
        assert runs_to_dicts(runs) == [
            {"text": "(n!)", "vert_align": None},
            {"text": "2", "vert_align": "superscript"},
            {"text": "-9n!+18=0", "vert_align": None},
        ]

    def test_subscript_markup_becomes_a_subscript_run(self) -> None:
        assert runs_to_dicts([TextRun(text="12·P_{n}")]) == [
            {"text": "12·P", "vert_align": None},
            {"text": "n", "vert_align": "subscript"},
        ]

    def test_flagged_runs_and_plain_text_are_untouched(self) -> None:
        runs = [
            TextRun(text="65"),
            TextRun(text="10", subscript=True),
            TextRun(text="A) 48"),
        ]
        assert runs_to_dicts(runs) == [
            {"text": "65", "vert_align": None},
            {"text": "10", "vert_align": "subscript"},
            {"text": "A) 48", "vert_align": None},
        ]
