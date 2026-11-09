"""A figure box that covers only part of a figure is widened to the whole figure.

Everything is drawn with Pillow (no real page or customer content): a matrix of
nine framed pictures with a row of answer pictures and their letters below it.
"""

import io
import logging

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.config import get_settings
from app.core.assembly.figure_region import border_ink_share, refine_figure_box
from app.core.assembly.page_blocks import crop_figure

_W, _H = 600, 800
_FONT = ImageFont.load_default(size=22)

# Where the pieces are, in pixels (x0, y0, x1, y1).
_MATRIX = (130, 100, 394, 364)
_ANSWERS = (50, 420, 440, 500)  # letters included
_TEXT_ABOVE = (60, 40, 540, 90)  # both lines of the question
_TEXT_BELOW = (60, 545, 540, 585)


def _frame(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 80) -> None:
    draw.rectangle((x, y, x + size, y + size), outline=0, width=3)
    for step in range(10, size, 14):
        draw.line((x + step, y, x, y + step), fill=0, width=2)
        draw.line((x + size, y + step, x + step, y + size), fill=0, width=2)


def _page(handwriting: bool = False) -> bytes:
    image = Image.new("RGB", (_W, _H), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (_TEXT_ABOVE[0], _TEXT_ABOVE[1]), "Sual 11. Qanunauygunlugu", fill=0, font=_FONT
    )
    draw.text(
        (_TEXT_ABOVE[0], _TEXT_ABOVE[1] + 24),
        "muyyen edin ve secin.",
        fill=0,
        font=_FONT,
    )
    for row in range(3):
        for column in range(3):
            _frame(draw, 130 + column * 92, 100 + row * 92)
    for letter, x in (("A)", 80), ("B)", 220), ("C)", 360)):
        _frame(draw, x, 420)
        draw.text((x - 30, 452), letter, fill=0, font=_FONT)
    draw.text(
        (_TEXT_BELOW[0], _TEXT_BELOW[1]),
        "Sual 12. Sekilde her biri",
        fill=0,
        font=_FONT,
    )
    if handwriting:
        # Left out by the model on purpose: no text block covers it.
        draw.text((130, 520), "45 + 20 = 65", fill=60, font=_FONT)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _box(pixels: tuple[int, int, int, int]) -> list[int]:
    """Pixel box (x0, y0, x1, y1) as the model's [ymin, xmin, ymax, xmax]/1000."""
    x0, y0, x1, y1 = pixels
    return [
        round(y0 / _H * 1000),
        round(x0 / _W * 1000),
        round(y1 / _H * 1000),
        round(x1 / _W * 1000),
    ]


def _pixels(box: list[int]) -> tuple[float, float, float, float]:
    ymin, xmin, ymax, xmax = box
    return xmin * _W / 1000, ymin * _H / 1000, xmax * _W / 1000, ymax * _H / 1000


# What the model gave in the failing case: the left two columns of the matrix.
_NARROW = _box((130, 100, 306, 364))
_TEXTS = [_box(_TEXT_ABOVE), _box(_TEXT_BELOW)]


class TestRefine:
    def test_a_narrow_box_grows_to_the_whole_figure(self) -> None:
        x0, y0, x1, y1 = _pixels(refine_figure_box(_page(), _NARROW, _TEXTS))
        # the third column, the answer pictures and their letters are all inside
        assert x0 <= _ANSWERS[0] + 8 and x1 >= _ANSWERS[2] - 3
        assert y0 <= _MATRIX[1] + 3 and y1 >= _ANSWERS[3] - 3

    def test_text_next_to_the_figure_is_not_taken(self) -> None:
        _, y0, _, y1 = _pixels(refine_figure_box(_page(), _NARROW, _TEXTS))
        assert y0 >= _TEXT_ABOVE[3]
        assert y1 <= _TEXT_BELOW[1]

    def test_a_box_that_reaches_into_the_text_above_is_pulled_back(self) -> None:
        # the model's top edge lies inside the last line of the question
        reaching = _box((130, 80, 306, 364))
        _, y0, _, _ = _pixels(refine_figure_box(_page(), reaching, _TEXTS))
        assert _TEXT_ABOVE[3] - 1 <= y0 <= _MATRIX[1]

    def test_a_box_that_reaches_into_the_text_below_is_pulled_back(self) -> None:
        reaching = _box((130, 100, 306, 560))
        _, _, _, y1 = _pixels(refine_figure_box(_page(), reaching, _TEXTS))
        assert _ANSWERS[3] <= y1 <= _TEXT_BELOW[1] + 1

    def test_handwriting_the_model_left_out_is_not_taken(self) -> None:
        # no text block covers it, so only its size and distance keep it out
        _, _, _, y1 = _pixels(refine_figure_box(_page(handwriting=True), _NARROW, []))
        assert y1 < 520

    def test_a_box_that_is_already_right_is_unchanged(self) -> None:
        whole = _box((50, 100, 440, 500))
        assert refine_figure_box(_page(), whole, _TEXTS) == whole

    def test_it_never_returns_a_smaller_box(self) -> None:
        refined = refine_figure_box(_page(), _NARROW, _TEXTS)
        assert refined[0] <= _NARROW[0] and refined[1] <= _NARROW[1]
        assert refined[2] >= _NARROW[2] and refined[3] >= _NARROW[3]

    def test_a_result_that_would_cover_the_page_is_refused(self) -> None:
        image = Image.new("RGB", (_W, _H), "white")
        ImageDraw.Draw(image).rectangle((5, 5, _W - 5, _H - 5), outline=0, width=4)
        ImageDraw.Draw(image).rectangle((250, 350, 350, 450), outline=0, width=4)
        out = io.BytesIO()
        image.save(out, format="PNG")
        box = _box((250, 350, 350, 450))
        assert refine_figure_box(out.getvalue(), box, []) == box

    @pytest.mark.parametrize("image", [b"not a picture", b""])
    def test_a_picture_that_cannot_be_read_leaves_the_box(self, image: bytes) -> None:
        assert refine_figure_box(image, _NARROW, _TEXTS) == _NARROW

    def test_an_empty_page_leaves_the_box(self) -> None:
        out = io.BytesIO()
        Image.new("RGB", (_W, _H), "white").save(out, format="PNG")
        assert refine_figure_box(out.getvalue(), _NARROW, []) == _NARROW

    def test_a_broken_box_is_returned_as_it_is(self) -> None:
        assert refine_figure_box(_page(), [1, 2, 3], []) == [1, 2, 3]

    def test_the_growth_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            refine_figure_box(_page(), _NARROW, _TEXTS)
        assert any("figure box widened" in r.getMessage() for r in caplog.records)


def _width(png: bytes | None) -> int:
    assert png is not None
    with Image.open(io.BytesIO(png)) as img:
        return int(img.width)


class TestInTheCut:
    def test_the_cut_figure_is_whole(self) -> None:
        png = crop_figure(_page(), _NARROW, _TEXTS)
        # wider than the narrow box (176 px + padding) by the missing column and
        # the answer pictures
        assert _width(png) >= _ANSWERS[2] - _ANSWERS[0]

    def test_the_switch_goes_back_to_the_models_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIGURE_REFINE_ENABLED", "false")
        get_settings.cache_clear()
        try:
            narrow = _width(crop_figure(_page(), _NARROW, _TEXTS))
        finally:
            monkeypatch.delenv("FIGURE_REFINE_ENABLED")
            get_settings.cache_clear()
        assert narrow < _ANSWERS[2] - _ANSWERS[0]

    def test_ink_on_the_edge_of_a_cut_is_logged(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FIGURE_REFINE_ENABLED", "false")
        get_settings.cache_clear()
        try:
            with caplog.at_level(logging.WARNING):
                # a cut straight through the matrix
                crop_figure(_page(), _box((180, 110, 260, 350)), [])
        finally:
            monkeypatch.delenv("FIGURE_REFINE_ENABLED")
            get_settings.cache_clear()
        assert any("figure_edge_ink" in r.getMessage() for r in caplog.records)


class TestBorderInk:
    def test_a_clean_border_has_no_ink(self) -> None:
        image = Image.new("RGB", (100, 100), "white")
        ImageDraw.Draw(image).rectangle((20, 20, 80, 80), outline=0, width=3)
        assert border_ink_share(image) == 0.0

    def test_ink_on_the_border_is_measured(self) -> None:
        image = Image.new("RGB", (100, 100), "white")
        ImageDraw.Draw(image).line((0, 50, 99, 50), fill=0, width=3)
        assert border_ink_share(image) > 0.0

    def test_a_tiny_picture_is_not_judged(self) -> None:
        assert border_ink_share(Image.new("RGB", (2, 2), "black")) == 0.0
