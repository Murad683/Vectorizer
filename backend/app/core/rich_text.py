"""Sub/superscripts and formulas in diagram text.

Two inputs come from the vision model:

- **Markup in plain text** (node text, titles/captions): ``_{...}`` is a
  subscript and ``^{...}`` a superscript, e.g. ``X_{1}, X_{2}, ... X_{20}``
  or ``b^{2}``. A bare ``_`` / ``^`` followed by digits is accepted too
  (``x_1``); anything else is literal text.
- **LaTeX** for a node that IS a formula (``DiagramNode.latex``), limited
  to what school flowcharts use: ``\\frac``, ``\\sqrt``, ``_``/``^``,
  ``\\text``, relations/operators and greek letters.

Outputs: plain text (sizes, previews), run lists with a vertical-align flag
(PowerPoint, Word runs) and Office Math (OMML) XML for Word equations.
"""

import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

Run = tuple[str, str | None]  # (text, None | "sub" | "sup")

_MARKUP = re.compile(r"([_^])(?:\{([^{}]*)\}|(\d+))")


def markup_runs(text: str) -> list[Run]:
    """Split ``text`` into runs at ``_{...}`` / ``^{...}`` markup."""
    runs: list[Run] = []
    pos = 0
    for m in _MARKUP.finditer(text):
        if m.start() > pos:
            runs.append((text[pos : m.start()], None))
        body = m.group(2) if m.group(2) is not None else m.group(3)
        runs.append((body, "sub" if m.group(1) == "_" else "sup"))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], None))
    return [r for r in runs if r[0]]


def plain(text: str) -> str:
    """Text as it reads with the markup removed (``X_{1}`` -> ``X1``)."""
    return "".join(t for t, _ in markup_runs(text))


# -- LaTeX subset -------------------------------------------------------------

_SYMBOLS = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "infty": "∞", "dots": "…", "ldots": "…", "cdots": "⋯",
    "sum": "∑", "prod": "∏", "in": "∈", "lt": "<", "gt": ">",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "epsilon": "ε", "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π",
    "sigma": "σ", "Sigma": "Σ", "phi": "φ", "omega": "ω", "Omega": "Ω",
    "%": "%", "{": "{", "}": "}", "_": "_", "&": "&", "#": "#",
}  # fmt: skip
_SPACES = {"quad": "  ", "qquad": "    ", ",": " ", ";": " ", ":": " ", " ": " "}
_IGNORED = {"left", "right", "displaystyle", "mathit", "big", "Big"}


@dataclass
class _Node:
    kind: str  # text | plain | seq | frac | sqrt | script
    text: str = ""
    parts: "list[_Node] | None" = None  # seq children / frac (num, den) / sqrt body
    sub: "_Node | None" = None
    sup: "_Node | None" = None


class LatexError(ValueError):
    """The formula uses something outside the supported subset."""


class _Parser:
    def __init__(self, src: str) -> None:
        self.s = src
        self.i = 0

    def parse(self) -> _Node:
        node = self._seq(stop="")
        if self.i < len(self.s):
            raise LatexError(f"unexpected {self.s[self.i]!r} at {self.i}")
        return node

    def _seq(self, stop: str) -> _Node:
        items: list[_Node] = []
        while self.i < len(self.s) and self.s[self.i] not in stop:
            ch = self.s[self.i]
            if ch in "_^":
                self.i += 1
                arg = self._arg()
                base = items.pop() if items else _Node("text", "")
                if base.kind != "script":
                    base = _Node("script", parts=[base])
                if ch == "_":
                    base.sub = arg
                else:
                    base.sup = arg
                items.append(base)
            else:
                atom = self._atom()
                if atom is not None:
                    items.append(atom)
        return _Node("seq", parts=items)

    def _arg(self) -> _Node:
        self._skip_spaces()
        if self.i >= len(self.s):
            raise LatexError("missing argument")
        if self.s[self.i] == "{":
            return self._group()
        atom = self._atom()
        if atom is None:
            raise LatexError("empty argument")
        return atom

    def _group(self) -> _Node:
        self.i += 1  # {
        node = self._seq(stop="}")
        if self.i >= len(self.s):
            raise LatexError("unclosed {")
        self.i += 1  # }
        return node

    def _skip_spaces(self) -> None:
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1

    def _atom(self) -> _Node | None:
        ch = self.s[self.i]
        if ch == "{":
            return self._group()
        if ch == "}":
            raise LatexError("unbalanced }")
        if ch != "\\":
            self.i += 1
            return _Node("text", ch)
        self.i += 1
        m = re.match(r"[A-Za-z]+|.", self.s[self.i :])
        if not m:
            raise LatexError("dangling backslash")
        name = m.group(0)
        self.i += len(name)
        if name == "frac":
            return _Node("frac", parts=[self._arg(), self._arg()])
        if name == "sqrt":
            self._skip_spaces()
            if self.i < len(self.s) and self.s[self.i] == "[":
                end = self.s.index("]", self.i)
                degree = self.s[self.i + 1 : end]
                self.i = end + 1
                return _Node("sqrt", text=degree, parts=[self._arg()])
            return _Node("sqrt", parts=[self._arg()])
        if name in ("text", "mathrm", "textrm", "operatorname"):
            arg = self._arg()
            return _Node("plain", _flat(arg))
        if name in _SYMBOLS:
            return _Node("text", _SYMBOLS[name])
        if name in _SPACES:
            return _Node("plain", _SPACES[name])
        if name in _IGNORED:
            return None
        raise LatexError(f"unsupported command \\{name}")


def _flat(node: _Node) -> str:
    """Linear plain text of a subtree (fractions as a/b, roots as √)."""
    if node.kind in ("text", "plain"):
        return node.text
    if node.kind == "seq":
        return "".join(_flat(p) for p in node.parts or [])
    if node.kind == "frac":
        num, den = node.parts or []
        return f"{_wrap(_flat(num))}/{_wrap(_flat(den))}"
    if node.kind == "sqrt":
        return "√" + _wrap(_flat((node.parts or [])[0]))
    base = _flat((node.parts or [])[0])
    return (
        base
        + (_flat(node.sub) if node.sub else "")
        + (_flat(node.sup) if node.sup else "")
    )


def _wrap(s: str) -> str:
    """Parenthesise a compound term in linear form: (-b)/(2a), but x/2."""
    return f"({s})" if len(s) > 1 and not (s.isdigit() or s.isalpha()) else s


def parse_latex(src: str) -> _Node:
    """Parse the supported LaTeX subset (raises ``LatexError``)."""
    return _Parser(src.strip().strip("$")).parse()


def latex_to_text(src: str) -> str:
    """Readable linear text of a formula, e.g. ``x = (-b)/(2a)``."""
    return _flat(parse_latex(src))


def latex_runs(src: str) -> list[Run]:
    """Linear runs with real sub/superscripts (for PowerPoint text)."""
    out: list[Run] = []

    def walk(node: _Node, vert: str | None) -> None:
        if node.kind in ("text", "plain"):
            out.append((node.text, vert))
        elif node.kind == "seq":
            for p in node.parts or []:
                walk(p, vert)
        elif node.kind == "script":
            walk((node.parts or [])[0], vert)
            if node.sub:
                walk(node.sub, "sub")
            if node.sup:
                walk(node.sup, "sup")
        else:
            out.append((_flat(node), vert))

    walk(parse_latex(src), None)
    merged: list[Run] = []
    for text, vert in out:
        if merged and merged[-1][1] == vert:
            merged[-1] = (merged[-1][0] + text, vert)
        elif text:
            merged.append((text, vert))
    return merged


# -- Office Math (OMML) ------------------------------------------------------

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def latex_to_omml(src: str, half_points: int) -> str:
    """``<m:oMath>`` for a formula; ``half_points`` is the font size x 2.

    The caller declares the ``m`` and ``w`` namespace prefixes.
    """
    rpr = (
        '<w:rPr><w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/>'
        f'<w:sz w:val="{half_points}"/><w:szCs w:val="{half_points}"/></w:rPr>'
    )

    def run(text: str, upright: bool = False) -> str:
        sty = '<m:rPr><m:sty m:val="p"/></m:rPr>' if upright else ""
        if not upright:
            text = text.replace("-", "−")  # a real minus sign
        return f'<m:r>{sty}{rpr}<m:t xml:space="preserve">{escape(text)}</m:t></m:r>'

    def emit(node: _Node) -> str:
        if node.kind == "text":
            return run(node.text) if node.text else ""
        if node.kind == "plain":
            return run(node.text, upright=True)
        if node.kind == "seq":
            # Adjacent characters share one run (Word's own equation layout).
            out, chars = [], ""
            for p in node.parts or []:
                if p.kind == "text":
                    chars += p.text
                    continue
                if chars:
                    out.append(run(chars))
                    chars = ""
                out.append(emit(p))
            if chars:
                out.append(run(chars))
            return "".join(out)
        if node.kind == "frac":
            num, den = node.parts or []
            return f"<m:f><m:num>{emit(num)}</m:num><m:den>{emit(den)}</m:den></m:f>"
        if node.kind == "sqrt":
            body = emit((node.parts or [])[0])
            if node.text:
                return (
                    f"<m:rad><m:deg>{run(node.text)}</m:deg><m:e>{body}</m:e></m:rad>"
                )
            return (
                '<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/>'
                f"<m:e>{body}</m:e></m:rad>"
            )
        base = f"<m:e>{emit((node.parts or [])[0])}</m:e>"
        if node.sub and node.sup:
            return (
                f"<m:sSubSup>{base}<m:sub>{emit(node.sub)}</m:sub>"
                f"<m:sup>{emit(node.sup)}</m:sup></m:sSubSup>"
            )
        if node.sub:
            return f"<m:sSub>{base}<m:sub>{emit(node.sub)}</m:sub></m:sSub>"
        return (
            f"<m:sSup>{base}<m:sup>{emit(node.sup or _Node('text'))}</m:sup></m:sSup>"
        )

    return f"<m:oMath>{emit(parse_latex(src))}</m:oMath>"


def has_fraction(src: str) -> bool:
    """True if the formula stacks lines (needs about two text lines)."""
    return "\\frac" in src


_SPACED = set("=+-−·×÷±≤≥<>≠≈→,")


def _math_width(node: _Node) -> float:
    """Typeset width of a formula in average character widths: a fraction
    is as wide as its wider line, scripts are smaller, and Word's equation
    layout puts space around operators and relations."""
    if node.kind == "text":
        return sum(1.8 if ch in _SPACED else 0.95 for ch in node.text)
    if node.kind == "plain":
        return float(len(node.text))
    if node.kind == "seq":
        return sum(_math_width(p) for p in node.parts or [])
    if node.kind == "frac":
        num, den = node.parts or []
        return max(_math_width(num), _math_width(den)) + 1.0
    if node.kind == "sqrt":
        return _math_width((node.parts or [])[0]) + 1.6
    scripts = [s for s in (node.sub, node.sup) if s is not None]
    extra = max((_math_width(s) for s in scripts), default=0.0)
    return _math_width((node.parts or [])[0]) + 0.8 * extra


def display_lines(text: str, latex: str | None = None) -> tuple[int, int]:
    """(characters in the longest line, line count) as the node displays.

    A stacked fraction is about two text lines tall and, being stacked,
    narrower than its linear spelling.
    """
    if latex:
        try:
            tree = parse_latex(latex)
        except LatexError:
            pass
        else:
            return max(1, round(_math_width(tree))), 2 if has_fraction(latex) else 1
    lines = [plain(line) for line in text.split("\n")]
    return max(len(line) for line in lines), len(lines)


def display_text(text: str, latex: str | None = None) -> str:
    """Linear, markup-free text of a node (previews, logs)."""
    if latex:
        try:
            return latex_to_text(latex)
        except LatexError:
            pass
    return "\n".join(plain(line) for line in text.split("\n"))
