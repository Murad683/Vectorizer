"""The gap finder: it must find planted gaps and stay quiet on good answers.

Pages are made up (``app.eval.synth``), so there is no customer content and the
right answer is known. Nothing here talks to Gemini.
"""

import importlib.util
import io
import re
from pathlib import Path
from typing import Any

import docx
import pytest

from app.config import get_settings
from app.core.vision.base import VisionProvider
from app.eval import gaps as gap_module
from app.eval.gaps import (
    ERROR,
    REVIEW,
    block_order,
    check_assembly,
    check_expected,
    cut_figures,
    find_gaps,
    sparse_text_boxes,
)
from app.eval.replay import (
    RecordingProvider,
    ReplayProvider,
    load_results,
    page_images,
    result_key,
    run_pipeline,
    save_results,
)
from app.eval.report import Row, annotate, build_report
from app.eval.synth import (
    SynthPage,
    all_pages,
    figure_with_caption,
    options_with_pictures,
    photo_effect,
    text_lines,
)
from app.models.document import ContentType, VisionResult
from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun


def _kinds(gaps: list[gap_module.Gap], severity: str | None = None) -> list[str]:
    return [g.kind for g in gaps if severity is None or g.severity == severity]


class TestGoodAnswersStayQuiet:
    @pytest.mark.parametrize("page", all_pages(), ids=lambda p: p.name)
    def test_no_gaps_on_a_good_answer(self, page: SynthPage) -> None:
        assert find_gaps(page.png, page.good) == []

    @pytest.mark.parametrize("page", all_pages(), ids=lambda p: p.name)
    def test_a_photo_like_page_gives_no_errors(self, page: SynthPage) -> None:
        photo = photo_effect(page.png)
        assert _kinds(find_gaps(photo, page.good), ERROR) == []


class TestPlantedGaps:
    def test_picture_options_given_back_as_empty_letters(self) -> None:
        page = options_with_pictures()
        errors = _kinds(find_gaps(page.png, page.broken), ERROR)
        assert errors.count("empty_option") == 5
        assert "uncovered_ink" in errors  # the five pictures are ink nothing explains

    def test_forgotten_text_lines(self) -> None:
        page = text_lines()
        errors = _kinds(find_gaps(page.png, page.broken), ERROR)
        assert errors.count("uncovered_ink") == 2

    def test_a_forgotten_figure(self) -> None:
        page = figure_with_caption()
        assert "uncovered_ink" in _kinds(find_gaps(page.png, page.broken), ERROR)

    def test_a_letter_block_explains_the_letter_but_not_a_picture(self) -> None:
        page = options_with_pictures()
        gaps = [
            g for g in find_gaps(page.png, page.broken) if g.kind == "uncovered_ink"
        ]
        # one gap per option picture, none of them for the letters or the matrix
        assert len(gaps) == 5

    def test_a_figure_cut_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FIGURE_REFINE_ENABLED", "false")
        get_settings.cache_clear()
        try:
            page = figure_with_caption()
            through = PageAnalysis(
                blocks=[
                    PageBlock(kind=BlockKind.FIGURE, box_2d=[300, 330, 700, 650]),
                ]
            )
            found = cut_figures(page.png, through)
        finally:
            monkeypatch.delenv("FIGURE_REFINE_ENABLED")
            get_settings.cache_clear()
        assert _kinds(found) == ["figure_cut"]
        assert found[0].severity == REVIEW

    def test_a_figure_block_without_a_box(self) -> None:
        page = figure_with_caption()
        answer = PageAnalysis(blocks=[PageBlock(kind=BlockKind.FIGURE)])
        assert [g.severity for g in cut_figures(page.png, answer)] == [ERROR]

    def test_a_big_text_box_with_almost_no_text(self) -> None:
        page = figure_with_caption()
        answer = PageAnalysis(
            blocks=[
                PageBlock(
                    kind=BlockKind.TEXT,
                    style=TextStyle.PARAGRAPH,
                    runs=[TextRun(text="ok")],
                    box_2d=[150, 150, 600, 800],
                )
            ]
        )
        assert _kinds(sparse_text_boxes(page.png, answer)) == ["sparse_text_box"]

    def test_blocks_out_of_reading_order(self) -> None:
        page = text_lines()
        swapped = PageAnalysis(blocks=list(reversed(page.good.blocks)))
        assert "order" in _kinds(block_order(swapped))
        assert block_order(page.good) == []

    def test_page_edge_content_is_only_to_review(self) -> None:
        page = text_lines()
        # ink at the bottom edge of the page that the answer does not explain
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(image).rectangle((100, 760, 500, 799), fill=0)
        out = io.BytesIO()
        image.save(out, format="PNG")
        found = find_gaps(out.getvalue(), PageAnalysis(blocks=[page.good.blocks[0]]))
        assert [g.severity for g in found if g.kind == "uncovered_ink"] == [REVIEW]


class TestExpectedText:
    def test_missing_lines_and_similarity(self) -> None:
        gaps, ratio = check_expected("one two\nthree four\nfive", "one two\nfive")
        assert [g.kind for g in gaps] == ["text_missing"]
        assert 0.5 < ratio < 1.0

    def test_everything_present(self) -> None:
        gaps, ratio = check_expected("a b\nc d", "a  b\nc d")
        assert gaps == [] and ratio == 1.0


def _write(page: SynthPage, folder: Path) -> Path:
    source = folder / "source.png"
    source.write_bytes(page.png)
    return source


class TestAssemblyCheck:
    def _built(
        self, page: SynthPage, answer: PageAnalysis, tmp_path: Path
    ) -> tuple[Path, Path]:
        source = _write(page, tmp_path)
        results = {
            result_key(page.png): VisionResult(
                content_type=ContentType.PAGE, page=answer
            )
        }
        docx_path, _ = run_pipeline(source, ReplayProvider(results), tmp_path / "out")
        return source, docx_path

    def test_a_good_answer_is_fully_in_the_word_file(self, tmp_path: Path) -> None:
        page = options_with_pictures()
        _, docx_path = self._built(page, page.good, tmp_path)
        assert check_assembly(page.good, docx_path, page.png) == []

    def test_text_removed_from_the_word_file_is_found(self, tmp_path: Path) -> None:
        page = text_lines()
        _, docx_path = self._built(page, page.good, tmp_path)
        document = docx.Document(str(docx_path))
        for paragraph in document.paragraphs:
            if "product" in paragraph.text:
                paragraph._element.getparent().remove(paragraph._element)
        document.save(str(docx_path))
        found = check_assembly(page.good, docx_path, page.png)
        assert [g.kind for g in found] == ["dropped_in_assembly"]
        assert "product" in found[0].detail

    def test_a_picture_removed_from_the_word_file_is_found(
        self, tmp_path: Path
    ) -> None:
        page = figure_with_caption()
        _, docx_path = self._built(page, page.good, tmp_path)
        document = docx.Document(str(docx_path))
        for drawing in document.element.body.iter(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing"
        ):
            drawing.getparent().remove(drawing)
            break
        document.save(str(docx_path))
        found = check_assembly(page.good, docx_path, page.png)
        assert any("pictures in the Word file: 0 of 1" in g.detail for g in found)


class _Fake(VisionProvider):
    def __init__(self, answer: PageAnalysis) -> None:
        self.answer = answer
        self.calls = 0

    def analyze_diagram(
        self, image: bytes, context: str | None = None, page: bool = False
    ) -> VisionResult:
        self.calls += 1
        return VisionResult(content_type=ContentType.PAGE, page=self.answer)


class TestRecordAndReplay:
    def test_a_recorded_answer_comes_back_without_a_request(
        self, tmp_path: Path
    ) -> None:
        page = text_lines()
        source = _write(page, tmp_path)
        fake = _Fake(page.good)
        recorder = RecordingProvider(fake)
        run_pipeline(source, recorder, tmp_path / "live")
        assert fake.calls == 1
        save_results(tmp_path / "results.json", recorder.results)

        loaded = load_results(tmp_path / "results.json", page_images(source))
        replay = ReplayProvider(loaded)
        docx_path, _ = run_pipeline(source, replay, tmp_path / "again")
        assert fake.calls == 1  # nothing new was asked
        assert "product" in "\n".join(
            p.text for p in docx.Document(str(docx_path)).paragraphs
        )

    def test_an_image_that_was_never_recorded_is_an_error(self) -> None:
        result = ReplayProvider({}).analyze_diagram(b"unknown", page=True)
        assert result.error

    def test_a_bare_page_answer_file_is_understood(self, tmp_path: Path) -> None:
        page = text_lines()
        source = _write(page, tmp_path)
        bare = tmp_path / "answer.json"
        bare.write_text(page.good.model_dump_json(), encoding="utf-8")
        loaded = load_results(bare, page_images(source))
        assert loaded[result_key(page.png)].page is not None

    def test_a_single_answer_for_a_many_page_source_is_refused(
        self, tmp_path: Path
    ) -> None:
        bare = tmp_path / "answer.json"
        bare.write_text(text_lines().good.model_dump_json(), encoding="utf-8")
        with pytest.raises(ValueError, match="one answer"):
            load_results(bare, [b"a", b"b"])


class TestReport:
    def test_the_report_marks_errors_and_names_the_kinds(self) -> None:
        page = options_with_pictures()
        found = find_gaps(page.png, page.broken)
        row = Row("options", "run2", 1, page.png, page.broken, found)
        report = build_report([row, Row("options", "run1", 1, page.png, page.good, [])])
        assert "empty_option" in report and "uncovered_ink" in report
        assert "data:image/png;base64," in report
        assert annotate(page.png, page.broken, found)[:4] == b"\x89PNG"
        assert row.errors == len([g for g in found if g.severity == ERROR])


def _script() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "gap_check.py"
    spec = importlib.util.spec_from_file_location("gap_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCommandLine:
    def test_a_corpus_inside_the_repository_is_refused(self) -> None:
        script = _script()
        with pytest.raises(SystemExit):
            script.ensure_outside_repo(script.REPO / "samples")

    def test_synth_then_check_finds_the_planted_gaps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = _script()
        corpus = tmp_path / "corpus"
        assert script.main.__module__  # the module loads
        args = type(
            "A", (), {"corpus": str(corpus), "photo": False, "fake_runs": True}
        )()
        assert script.cmd_synth(args) == 0
        check = type("A", (), {"corpus": str(corpus), "only": None})()
        assert script.cmd_check(check) == 0
        output = capsys.readouterr().out
        lines = {
            (m.group(1), m.group(2)): int(m.group(3))
            for m in re.finditer(r"^(\w+)\s+(run\d)\s+\d+\s+(\d+)", output, re.M)
        }
        assert lines[("options_with_pictures", "run1")] == 0
        assert lines[("options_with_pictures", "run2")] > 0
        assert lines[("text_lines", "run2")] > 0
        assert list((corpus / "reports").glob("*/index.html"))

    def test_capture_sends_nothing_without_yes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = _script()
        corpus = tmp_path / "corpus"
        script.cmd_synth(
            type("A", (), {"corpus": str(corpus), "photo": False, "fake_runs": False})()
        )
        fake = _Fake(text_lines().good)
        monkeypatch.setattr(script, "get_vision_provider", lambda budget: fake)
        base = {
            "corpus": str(corpus),
            "runs": 2,
            "only": "text_lines",
            "force": False,
            "no_rescue": True,
        }
        assert script.cmd_capture(type("A", (), {**base, "yes": False})()) == 2
        assert fake.calls == 0
        assert script.cmd_capture(type("A", (), {**base, "yes": True})()) == 0
        assert fake.calls == 2
        runs = sorted((corpus / "text_lines" / "runs").iterdir())
        assert [r.name for r in runs] == ["run1", "run2"]
        assert (runs[0] / "results.json").exists() and (runs[0] / "usage.json").exists()
        # a second call records only what is missing
        assert script.cmd_capture(type("A", (), {**base, "yes": True})()) == 0
        assert fake.calls == 2
