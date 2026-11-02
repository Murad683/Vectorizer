"""Page mode: a whole photographed page read as text + flowchart + figures.

Offline: the vision model is faked. All texts are invented; the images are
drawn with Pillow (the real sample photo never enters the repository).
"""

import hashlib
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw, ImageFont
from pptx.util import Inches

from app.config import get_settings
from app.core.assembly import assemble_docx, assemble_pptx
from app.core.assembly.page_blocks import FlowUnit, crop_figure, group_blocks
from app.core.classifier import classify_elements
from app.core.ingestion.image_extractor import ImageExtractor
from app.core.vision import gemini_provider
from app.core.vision.base import VisionProvider
from app.core.vision.cache import CachedVisionProvider
from app.core.vision.gemini_provider import GeminiVisionProvider
from app.core.vision.postprocess import (
    clean_page,
    filter_page,
    ink_ratio,
    parse_vision_analysis,
)
from app.models.document import (
    ContentType,
    ElementKind,
    ExtractedElement,
    VisionResult,
)
from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun
from tests.diagram_fixtures import run_fixture

_W, _H = 900, 1200
_FONT = ImageFont.load_default(size=30)

# The prompt used before page mode existed; it must stay byte-identical.
_OLD_PROMPT_SHA256 = "8911d5a1e6c5129e71891c9f7d54788ffc15183a70d388b3b2d9bc762c7ece4a"


def _box(pixels: tuple[int, int, int, int]) -> list[int]:
    """Pixel box (x0, y0, x1, y1) to the model's [ymin, xmin, ymax, xmax]/1000."""
    x0, y0, x1, y1 = pixels
    return [
        round(y0 / _H * 1000),
        round(x0 / _W * 1000),
        round(y1 / _H * 1000),
        round(x1 / _W * 1000),
    ]


def _text(style: TextStyle, text: str, pixels: tuple[int, int, int, int]) -> PageBlock:
    return PageBlock(
        kind=BlockKind.TEXT,
        style=style,
        runs=[TextRun(text=text)],
        box_2d=_box(pixels),
    )


def _page_image() -> bytes:
    """A page with dark printed text, ghost text, handwriting and a figure."""
    img = Image.new("RGB", (_W, _H), "white")
    draw = ImageDraw.Draw(img)
    draw.text((100, 100), "Sual 1. Birinci sualin metni?", fill=0, font=_FONT)
    draw.text((100, 160), "A) a < n, s = s + a", fill=0, font=_FONT)
    draw.text((100, 220), "Sual 2. Ikinci sual haqqinda?", fill=0, font=_FONT)
    draw.text((500, 700), "m*2 = 2m*2 aa", fill=(222, 222, 222), font=_FONT)  # ghost
    draw.text(
        (100, 900), "45 + 20 = 65 ok", fill=(175, 175, 175), font=_FONT
    )  # handwriting
    draw.rectangle((100, 1000, 400, 1150), outline=0, width=6)  # figure
    draw.line((100, 1000, 400, 1150), fill=0, width=6)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


_PAGE_IMAGE = _page_image()


def _page_blocks() -> list[PageBlock]:
    graph, _ = run_fixture("sum_loop")
    graph.title = None
    return [
        _text(TextStyle.HEADING, "Sual 1. Birinci sualin metni?", (100, 100, 700, 140)),
        _text(TextStyle.OPTION, "A) a < n, s = s + a", (100, 160, 500, 200)),
        _text(TextStyle.HEADING, "Sual 2. Ikinci sual haqqinda?", (100, 220, 700, 260)),
        PageBlock(
            kind=BlockKind.FLOWCHART, diagram=graph, box_2d=_box((100, 300, 600, 650))
        ),
        _text(TextStyle.OPTION, "B) a ≤ n, a := 0", (100, 700, 500, 740)),
        _text(TextStyle.OPTION, "C) a ≥ n, ə ı ş ç ğ ö ü", (100, 750, 600, 790)),
        PageBlock(kind=BlockKind.FIGURE, box_2d=_box((100, 1000, 400, 1150))),
        PageBlock(
            kind=BlockKind.TABLE,
            rows=[["1", "2"], ["a", "b"]],
            box_2d=_box((100, 820, 400, 900)),
        ),
    ]


class _Fixed(VisionProvider):
    def __init__(self, result: VisionResult) -> None:
        self.result = result
        self.page_flags: list[bool] = []

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        self.page_flags.append(page)
        return self.result.model_copy(deep=True)


def _page_result() -> VisionResult:
    return VisionResult(
        content_type=ContentType.PAGE, page=PageAnalysis(blocks=_page_blocks())
    )


def _classified(image: bytes = _PAGE_IMAGE, whole_page: bool = True):
    return classify_elements(
        [
            ExtractedElement(
                source_page=1,
                element_kind=ElementKind.IMAGE,
                raw_content=image,
                metadata={"image_ext": "png", "whole_page": whole_page},
            )
        ]
    )


_EXPECTED_TEXTS = [
    "Sual 1. Birinci sualin metni?",
    "A) a < n, s = s + a",
    "Sual 2. Ikinci sual haqqinda?",
    "B) a ≤ n, a := 0",
    "C) a ≥ n, ə ı ş ç ğ ö ü",
]


class TestPromptAndProvider:
    def test_single_diagram_prompt_is_unchanged(self) -> None:
        digest = hashlib.sha256(gemini_provider._PROMPT.encode("utf8")).hexdigest()
        assert digest == _OLD_PROMPT_SHA256

    def test_page_prompt_reuses_the_flowchart_rules(self) -> None:
        assert gemini_provider._FLOWCHART_RULES in gemini_provider._PAGE_PROMPT
        assert gemini_provider._FLOWCHART_RULES in gemini_provider._PROMPT
        assert "READING ORDER" in gemini_provider._PAGE_PROMPT

    @pytest.fixture()
    def provider(self, monkeypatch: pytest.MonkeyPatch) -> GeminiVisionProvider:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
        get_settings.cache_clear()
        monkeypatch.setattr("app.core.vision.gemini_provider.genai.Client", MagicMock())
        built = GeminiVisionProvider()
        get_settings.cache_clear()
        return built

    @staticmethod
    def _reply(payload: dict[str, Any]) -> MagicMock:
        response = MagicMock()
        response.text = json.dumps(payload)
        return response

    @staticmethod
    def _page_payload(blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "content_type": "page",
            "diagram": None,
            "text_content": None,
            "page": {"blocks": blocks},
        }

    @staticmethod
    def _text_block(text: str, box: list[int]) -> dict[str, Any]:
        return {
            "kind": "text",
            "style": "paragraph",
            "runs": [{"text": text, "subscript": False, "superscript": False}],
            "box_2d": box,
        }

    def test_page_request_uses_the_page_prompt(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.return_value = self._reply(
            self._page_payload([self._text_block("Salam dunya", [80, 100, 120, 700])])
        )
        result = provider.analyze_diagram(_PAGE_IMAGE, page=True)
        assert result.content_type == ContentType.PAGE
        assert result.page is not None and result.page.blocks[0].text == "Salam dunya"
        prompt = next(
            p
            for p in provider._client.models.generate_content.call_args.kwargs[
                "contents"
            ]
            if isinstance(p, str)
        )
        assert "READING ORDER" in prompt

    def test_normal_request_still_uses_the_old_prompt(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.return_value = self._reply(
            {
                "content_type": "diagram",
                "diagram": {
                    "nodes": [
                        {"id": "n1", "shape_type": "terminator", "text": "A"},
                        {"id": "n2", "shape_type": "terminator", "text": "B"},
                    ],
                    "edges": [{"source_id": "n1", "target_id": "n2"}],
                },
                "text_content": None,
            }
        )
        provider.analyze_diagram(b"png-bytes")
        prompt = next(
            p
            for p in provider._client.models.generate_content.call_args.kwargs[
                "contents"
            ]
            if isinstance(p, str)
        )
        assert "READING ORDER" not in prompt

    def test_faint_text_in_the_answer_is_filtered_out(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.return_value = self._reply(
            self._page_payload(
                [
                    self._text_block(
                        "Sual 1. Birinci sualin metni?", _box((100, 100, 700, 140))
                    ),
                    self._text_block("m*2 = 2m*2 aa", _box((500, 700, 800, 740))),
                ]
            )
        )
        result = provider.analyze_diagram(_PAGE_IMAGE, page=True)
        assert result.page is not None
        assert [b.text for b in result.page.blocks] == ["Sual 1. Birinci sualin metni?"]

    def test_flowchart_with_a_missing_arrow_gets_one_corrective_call(
        self, provider: GeminiVisionProvider
    ) -> None:
        def chart(edges: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "kind": "flowchart",
                "diagram": {
                    "nodes": [
                        {"id": "n1", "shape_type": "terminator", "text": "Başlanğıc"},
                        {"id": "n2", "shape_type": "decision", "text": "a < 5"},
                        {"id": "n3", "shape_type": "terminator", "text": "Son"},
                    ],
                    "edges": edges,
                },
                "box_2d": [100, 100, 600, 600],
            }

        incomplete = [
            {"source_id": "n1", "target_id": "n2"},
            {"source_id": "n2", "target_id": "n3", "label": "Yox"},
        ]
        complete = [*incomplete, {"source_id": "n2", "target_id": "n3", "label": "Hə"}]
        provider._client.models.generate_content.side_effect = [
            self._reply(self._page_payload([chart(incomplete)])),
            self._reply(self._page_payload([chart(complete)])),
        ]
        result = provider.analyze_diagram(_PAGE_IMAGE, page=True)
        assert provider._client.models.generate_content.call_count == 2
        assert result.page is not None
        assert len(result.page.blocks[0].diagram.edges) == 3  # type: ignore[union-attr]

    def test_page_costs_one_request_when_the_chart_is_complete(
        self, provider: GeminiVisionProvider
    ) -> None:
        provider._client.models.generate_content.return_value = self._reply(
            self._page_payload([self._text_block("Salam", [80, 100, 120, 700])])
        )
        provider.analyze_diagram(_PAGE_IMAGE, page=True)
        assert provider._client.models.generate_content.call_count == 1


class TestParseAndClean:
    def test_garbage_page_is_rejected(self) -> None:
        raw = json.dumps({"content_type": "page", "page": {"blocks": []}})
        assert parse_vision_analysis(raw) is None
        raw = json.dumps({"content_type": "page", "page": None})
        assert parse_vision_analysis(raw) is None

    def test_clean_page_drops_unusable_blocks(self) -> None:
        graph, _ = run_fixture("sum_loop")
        edgeless = graph.model_copy(deep=True)
        edgeless.edges = []
        page = PageAnalysis(
            blocks=[
                PageBlock(kind=BlockKind.TEXT, runs=[TextRun(text="  ")]),
                PageBlock(kind=BlockKind.TABLE, rows=[["", " "]]),
                PageBlock(kind=BlockKind.FIGURE),  # no box: cannot be cut out
                PageBlock(
                    kind=BlockKind.FLOWCHART,
                    diagram=edgeless,
                    box_2d=[100, 100, 500, 500],
                ),
                _text(TextStyle.PARAGRAPH, "Qalan", (10, 10, 200, 50)),
            ]
        )
        cleaned = clean_page(page)
        assert [b.kind for b in cleaned.blocks] == [BlockKind.FIGURE, BlockKind.TEXT]
        assert cleaned.blocks[0].box_2d == [100, 100, 500, 500]  # was the chart

    def test_boxes_are_clamped(self) -> None:
        block = PageBlock(kind=BlockKind.FIGURE, box_2d=[-20, 10, 1200, 500])
        assert clean_page(PageAnalysis(blocks=[block])).blocks[0].box_2d == [
            0,
            10,
            1000,
            500,
        ]


class TestGhostFilter:
    def test_measured_ratios(self) -> None:
        with Image.open(io.BytesIO(_PAGE_IMAGE)) as img:
            gray = img.convert("L")
        printed = ink_ratio(gray, _box((100, 100, 700, 140)))
        ghost = ink_ratio(gray, _box((500, 700, 800, 740)))
        hand = ink_ratio(gray, _box((100, 900, 600, 940)))
        assert printed > 0.7 and ghost < 0.2 and hand < 0.4

    def test_printed_kept_ghost_handwriting_and_cutoff_dropped(self) -> None:
        page = PageAnalysis(
            blocks=[
                _text(
                    TextStyle.HEADING,
                    "Sual 1. Birinci sualin metni?",
                    (100, 100, 700, 140),
                ),
                _text(TextStyle.PARAGRAPH, "m*2 = 2m*2 aa", (500, 700, 800, 740)),
                _text(TextStyle.PARAGRAPH, "45 + 20 = 65 ok", (100, 900, 600, 940)),
                _text(TextStyle.PARAGRAPH, "yarım sətir", (0, 100, 120, 140)),
                PageBlock(kind=BlockKind.FIGURE, box_2d=_box((500, 700, 800, 740))),
            ]
        )
        kept = filter_page(page, _PAGE_IMAGE)
        assert [b.kind for b in kept.blocks] == [BlockKind.TEXT, BlockKind.FIGURE]
        assert kept.blocks[0].text == "Sual 1. Birinci sualin metni?"

    def test_long_text_touching_the_edge_is_kept(self) -> None:
        long_text = "Bu uzun cümlə şəklin kənarına toxunur, amma tam çap mətnidir. " * 2
        page = PageAnalysis(
            blocks=[_text(TextStyle.PARAGRAPH, long_text, (0, 100, 890, 140))]
        )
        assert len(filter_page(page, _PAGE_IMAGE).blocks) == 1

    def test_unreadable_image_leaves_the_page_unchanged(self) -> None:
        page = PageAnalysis(blocks=_page_blocks())
        assert filter_page(page, b"not an image") is page


class TestGrouping:
    def test_chart_takes_its_question_and_options(self) -> None:
        items = group_blocks(_page_blocks())
        units = [i for i in items if isinstance(i, FlowUnit)]
        assert len(units) == 1
        assert [b.text for b in units[0].before] == ["Sual 2. Ikinci sual haqqinda?"]
        assert [b.text for b in units[0].after] == [
            "B) a ≤ n, a := 0",
            "C) a ≥ n, ə ı ş ç ğ ö ü",
        ]
        # the first question stays an ordinary block, in order
        assert items[0].text == "Sual 1. Birinci sualin metni?"  # type: ignore[union-attr]
        assert len(items) == 5  # 2 text + unit + figure + table

    def test_options_stop_at_the_next_question(self) -> None:
        blocks = _page_blocks()
        blocks.insert(
            5, _text(TextStyle.HEADING, "Sual 3. Növbəti sual?", (1, 1, 2, 2))
        )
        unit = next(i for i in group_blocks(blocks) if isinstance(i, FlowUnit))
        assert [b.text for b in unit.after] == ["B) a ≤ n, a := 0"]

    def test_chart_without_a_question_line_has_no_before(self) -> None:
        graph, _ = run_fixture("sum_loop")
        blocks = [
            PageBlock(kind=BlockKind.FLOWCHART, diagram=graph, box_2d=[1, 1, 9, 9])
        ]
        unit = group_blocks(blocks)[0]
        assert isinstance(unit, FlowUnit) and unit.before == [] and unit.after == []


class TestDocx:
    def test_text_sequence_symbols_and_structure(self) -> None:
        document = assemble_docx(_classified(), vision_provider=_Fixed(_page_result()))
        texts = [p.text for p in document.paragraphs if p.text]
        assert texts == _EXPECTED_TEXTS
        assert len(document.tables) == 1
        assert len(document.inline_shapes) == 1  # the cropped figure
        body = document.element.body
        ns = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
        assert len(body.findall(f".//{ns}anchor")) == 1  # the flowchart

    def test_the_chart_sits_between_its_question_and_its_options(self) -> None:
        document = assemble_docx(_classified(), vision_provider=_Fixed(_page_result()))
        ns = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
        order: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                text = "".join(
                    t.text or "" for t in child.iter() if t.tag.endswith("}t")
                )
                if child.findall(f".//{ns}anchor"):
                    order.append("CHART")
                elif text:
                    order.append(text)
        assert order.index("Sual 2. Ikinci sual haqqinda?") + 1 == order.index("CHART")
        assert order.index("CHART") + 1 == order.index("B) a ≤ n, a := 0")

    def test_figure_fits_the_page_width(self) -> None:
        document = assemble_docx(_classified(), vision_provider=_Fixed(_page_result()))
        section = document.sections[0]
        picture = document.inline_shapes[0]
        assert (
            picture.width
            <= section.page_width - section.left_margin - section.right_margin
        )

    def test_page_mode_asks_for_a_page(self) -> None:
        provider = _Fixed(_page_result())
        assemble_docx(_classified(), vision_provider=provider)
        assert provider.page_flags == [True]


class TestPptx:
    def _prs(self):
        return assemble_pptx(_classified(), vision_provider=_Fixed(_page_result()))

    def test_same_text_in_the_same_order_as_the_docx(self) -> None:
        found: list[str] = []
        for slide in self._prs().slides:
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text in _EXPECTED_TEXTS:
                    found.append(shape.text_frame.text)
        assert found == _EXPECTED_TEXTS

    def test_question_chart_and_options_share_a_slide(self) -> None:
        prs = self._prs()
        chart_slides = [
            s
            for s in prs.slides
            if any(
                sh.has_text_frame
                and sh.text_frame.text == "Sual 2. Ikinci sual haqqinda?"
                for sh in s.shapes
            )
        ]
        assert len(chart_slides) == 1
        texts = {
            sh.text_frame.text for sh in chart_slides[0].shapes if sh.has_text_frame
        }
        assert {"B) a ≤ n, a := 0", "C) a ≥ n, ə ı ş ç ğ ö ü"} <= texts

    def test_nothing_leaves_the_slide_and_text_does_not_overlap(self) -> None:
        prs = self._prs()
        for slide in prs.slides:
            boxes = []
            for shape in slide.shapes:
                assert shape.left >= 0 and shape.top >= 0
                assert shape.left + shape.width <= prs.slide_width
                assert shape.top + shape.height <= prs.slide_height + Inches(0.05)
                if shape.has_text_frame and shape.text_frame.text in _EXPECTED_TEXTS:
                    boxes.append(shape)
            for i, a in enumerate(boxes):
                for b in boxes[i + 1 :]:
                    assert not (
                        a.left < b.left + b.width
                        and b.left < a.left + a.width
                        and a.top < b.top + b.height
                        and b.top < a.top + a.height
                    )

    def test_figure_is_a_picture(self) -> None:
        pictures = [
            sh for s in self._prs().slides for sh in s.shapes if sh.shape_type == 13
        ]
        assert len(pictures) == 1


class TestSwitchAndCache:
    def test_switch_off_restores_the_old_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAGE_MODE_ENABLED", "false")
        get_settings.cache_clear()
        graph, _ = run_fixture("sum_loop")
        provider = _Fixed(
            VisionResult(content_type=ContentType.DIAGRAM, diagram_graph=graph)
        )
        assemble_docx(_classified(), vision_provider=provider)
        assert provider.page_flags == [False]

    def test_embedded_images_are_not_read_as_pages(self) -> None:
        graph, _ = run_fixture("sum_loop")
        provider = _Fixed(
            VisionResult(content_type=ContentType.DIAGRAM, diagram_graph=graph)
        )
        assemble_docx(_classified(whole_page=False), vision_provider=provider)
        assert provider.page_flags == [False]

    def test_page_and_diagram_answers_are_cached_separately(self) -> None:
        inner = _Fixed(_page_result())
        cached = CachedVisionProvider(inner)
        cached.analyze_diagram(_PAGE_IMAGE, page=True)
        cached.analyze_diagram(_PAGE_IMAGE, page=True)
        cached.analyze_diagram(_PAGE_IMAGE)
        assert inner.page_flags == [True, False]
        assert cached.calls == 2

    def test_extractor_marks_a_standalone_image_as_a_page(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.png"
        path.write_bytes(_PAGE_IMAGE)
        element = ImageExtractor().extract(path)[0]
        assert element.metadata["whole_page"] is True

    def test_crop_figure(self) -> None:
        png = crop_figure(_PAGE_IMAGE, _box((100, 1000, 400, 1150)))
        assert png is not None
        with Image.open(io.BytesIO(png)) as img:
            assert abs(img.width - 300) <= 3 and abs(img.height - 150) <= 3
        assert crop_figure(_PAGE_IMAGE, None) is None
        assert crop_figure(b"junk", [0, 0, 500, 500]) is None
