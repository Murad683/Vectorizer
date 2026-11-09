"""Made-up test pages whose content we know, with a good and a broken answer.

Nothing here is customer content. Every page is drawn with Pillow, so the
expected text is known and the answers can be written by hand: ``good`` is what
a model should say, ``broken`` is a mistake that really happened (answer options
that are pictures given back as empty "A)" blocks). The gap detectors must find
the broken ones and must stay quiet on the good ones.
"""

import io
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.models.page import BlockKind, PageAnalysis, PageBlock, TextStyle
from app.models.text_content import TextRun

WIDTH, HEIGHT = 600, 800
_FONT = ImageFont.load_default(size=22)


@dataclass
class SynthPage:
    """A drawn page, the text it should give back, and answers about it."""

    name: str
    png: bytes
    expected: str
    good: PageAnalysis
    broken: PageAnalysis


def _units(x0: int, y0: int, x1: int, y1: int) -> list[int]:
    return [
        round(y0 / HEIGHT * 1000),
        round(x0 / WIDTH * 1000),
        round(y1 / HEIGHT * 1000),
        round(x1 / WIDTH * 1000),
    ]


def _text(text: str, box: tuple[int, int, int, int], style: TextStyle) -> PageBlock:
    return PageBlock(
        kind=BlockKind.TEXT,
        style=style,
        runs=[TextRun(text=text)],
        box_2d=_units(*box),
    )


def _figure(box: tuple[int, int, int, int]) -> PageBlock:
    return PageBlock(kind=BlockKind.FIGURE, box_2d=_units(*box))


def _frame(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 80) -> None:
    draw.rectangle((x, y, x + size, y + size), outline=0, width=3)
    for step in range(10, size, 14):
        draw.line((x + step, y, x, y + step), fill=0, width=2)
        draw.line((x + size, y + step, x + step, y + size), fill=0, width=2)


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def options_with_pictures() -> SynthPage:
    """A matrix question whose five answer options are pictures with letters."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    question = "11. Find the rule and choose the picture for the question mark."
    draw.text((60, 40), "11. Find the rule and choose", fill=0, font=_FONT)
    draw.text((60, 64), "the picture for the question mark.", fill=0, font=_FONT)
    for row in range(3):
        for column in range(3):
            _frame(draw, 130 + column * 92, 100 + row * 92)
    draw.text((410, 300), "?", fill=0, font=_FONT)
    option_boxes = []
    for letter, x, y in (
        ("A)", 80, 420),
        ("B)", 220, 420),
        ("C)", 360, 420),
        ("D)", 80, 520),
        ("E)", 220, 520),
    ):
        _frame(draw, x, y)
        draw.text((x - 30, y + 32), letter, fill=0, font=_FONT)
        option_boxes.append((letter, (x - 34, y - 4, x + 86, y + 84)))
    follow = "12. Which link joins the two rings?"
    draw.text((60, 640), follow, fill=0, font=_FONT)

    q_block = _text(question, (60, 36, 540, 90), TextStyle.HEADING)
    follow_block = _text(follow, (60, 636, 540, 668), TextStyle.HEADING)
    good = PageAnalysis(blocks=[q_block, _figure((100, 96, 470, 614)), follow_block])
    broken = PageAnalysis(
        blocks=[
            q_block,
            _figure((120, 96, 410, 372)),
            *[_text(letter, box, TextStyle.OPTION) for letter, box in option_boxes],
            follow_block,
        ]
    )
    return SynthPage(
        "options_with_pictures",
        _png(image),
        f"{question}\n{follow}",
        good,
        broken,
    )


def text_lines() -> SynthPage:
    """Five lines of text; the broken answer forgets two of them."""
    lines = [
        "1. The sum of x and y is twenty.",
        "2. The product of x and y is ninety-six.",
        "3. Which of the numbers is larger?",
        "4. Choose the right answer below.",
        "5. Only one answer is correct.",
    ]
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    blocks = []
    for index, line in enumerate(lines):
        y = 80 + index * 60
        draw.text((60, y), line, fill=0, font=_FONT)
        blocks.append(_text(line, (56, y - 4, 540, y + 30), TextStyle.PARAGRAPH))
    return SynthPage(
        "text_lines",
        _png(image),
        "\n".join(lines),
        PageAnalysis(blocks=blocks),
        PageAnalysis(blocks=[blocks[0], blocks[1], blocks[4]]),
    )


def figure_with_caption() -> SynthPage:
    """A framed figure under a question; the broken answer has no figure block."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    question = "7. What does the figure show?"
    draw.text((60, 60), question, fill=0, font=_FONT)
    draw.ellipse((150, 160, 450, 460), outline=0, width=4)
    draw.line((150, 310, 450, 310), fill=0, width=3)
    draw.line((300, 160, 300, 460), fill=0, width=3)
    draw.polygon([(220, 380), (300, 250), (380, 380)], outline=0, width=3)
    q_block = _text(question, (56, 56, 540, 92), TextStyle.HEADING)
    return SynthPage(
        "figure_with_caption",
        _png(image),
        question,
        PageAnalysis(blocks=[q_block, _figure((150, 160, 470, 460))]),
        PageAnalysis(blocks=[q_block]),
    )


def all_pages() -> list[SynthPage]:
    """Every made-up page."""
    return [options_with_pictures(), text_lines(), figure_with_caption()]


def photo_effect(png: bytes, seed: int = 1) -> bytes:
    """Make a clean page look like a phone photo: slight turn, shadow, noise, blur."""
    rng = random.Random(seed)
    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
    image = image.rotate(
        rng.uniform(-2.0, 2.0), fillcolor=(255, 255, 255), expand=False
    )
    shade = Image.linear_gradient("L").resize(image.size).point(lambda v: 255 - v // 4)
    image = Image.composite(image, Image.new("RGB", image.size, (200, 200, 200)), shade)
    noise = Image.effect_noise(image.size, 12).convert("RGB")
    image = Image.blend(image, noise, 0.04).filter(ImageFilter.GaussianBlur(0.8))
    return _png(image)
