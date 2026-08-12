# PROJECT CONTEXT — Test Material Vectorizer

> **Read this file at the start of every future session/phase before writing
> code.** It records the decisions and conventions that keep the codebase
> consistent across sessions.

## Project summary

Test Material Vectorizer converts pixelated, low-quality PDF/DOCX test
materials — Azerbaijani educational content containing algorithm flowcharts,
tables, and exam questions — into clean, vector-quality Word/PowerPoint files.
A vision LLM reconstructs diagrams (e.g., as Mermaid, then rendered to vector
graphics) while plain text and tables are extracted and copied directly. The
goal is professional-grade output that teachers can edit, not lossy image
embeds.

## Architecture decisions

1. **Vision provider isolation.** All vision LLM calls must go through a
   single provider interface (future: `backend/app/core/vision_provider.py`).
   The current provider is Google Gemini, but it must be swappable without
   touching any other code. No direct Gemini SDK/API calls anywhere else.

2. **Classifier before vision.** A classifier step decides per-element whether
   content needs vision processing (diagrams, flowcharts) or can be copied
   directly (plain text, tables). This is a cost optimization — vision calls
   are expensive and must only run on elements that need reconstruction.

3. **Queue-based processing.** Jobs are processed via a queue backed by SQLite
   for the MVP phase, designed so it can migrate to a proper queue/Postgres
   later without reworking the processing pipeline. Batch API usage should be
   considered later for further cost reduction.

## Formats

- **Supported input:** PDF, DOCX, PNG/JPG. (PPTX input is explicitly deferred.)
- **Output:** DOCX (priority), PPTX (later).

## Frontend requirements (later phases)

- Modern minimalist design.
- UI language: **Azerbaijani**.
- Brand color palette:
  - `#151515` — primary dark
  - `#FEE500` — accent
  - `#FFFFFF` — white
  - `#EFEFEF` — light gray
  - `#333333` — secondary dark

## DevEx conventions (established in Phase 1 — follow, don't reinvent)

- **Structured JSON logging** with request-id correlation
  (`app/logging_config.py` + `app/middleware/request_id.py`). Every log line
  carries the request id; every response carries an `X-Request-ID` header.
- **Unified error response shape** (`app/models/errors.py`:
  `{error_code, message, request_id}`) returned by the global exception
  handler in `app/main.py`. All future error responses use this model.
- **Configuration only via `app/config.py`** (pydantic-settings, `.env`).
  No hardcoded secrets or magic numbers; prod mode fails fast on missing
  required env vars.
- **Docker for local dev** (`docker-compose.yml`, `make up`).
- **CI runs lint + test on every push/PR** (`.github/workflows/ci.yml`:
  ruff, black --check, mypy, pytest).
- **Code style:** type hints on all functions, docstrings on all public
  functions/classes, black + ruff + mypy (strict) configured in
  `backend/pyproject.toml`, enforced by pre-commit.

Future phases must follow these conventions rather than introducing new
patterns.

## Phase 2 additions (ingestion + classifier + vision provider)

### What exists now

- `app/core/ingestion/` — `Extractor` ABC with `PdfExtractor` (PyMuPDF)
  and `DocxExtractor` (python-docx); `get_extractor(path)` dispatches by
  extension. Extractors preserve reading order and skip (log) broken
  elements instead of raising.
- `app/core/classifier.py` — rule-based, zero-cost classification.
- `app/core/vision/` — `VisionProvider` ABC + `GeminiVisionProvider`
  (google-generativeai SDK, retry ×2 on transient errors, 60 s timeout);
  `get_vision_provider()` is the factory. **Only
  `vision/gemini_provider.py` may import the Gemini SDK.**
- `app/models/document.py` — `ExtractedElement` (with `ElementKind`
  image/text_block and a free-form `metadata` dict for extractor hints),
  `ClassifiedElement`, `ElementType` (DIAGRAM/TEXT/TABLE), `VisionResult`.
- `scripts/validate_vision.py` — one-shot manual pipeline validation on
  the flowchart sample.

### Classifier rule list (ordered; first match wins)

1. `rule_image_is_diagram` — any image element → DIAGRAM
2. `rule_table_text` — text with `metadata["is_table"]` → TABLE
3. `rule_plain_text` — remaining text → TEXT

New rules are added by inserting a `Rule` function
(`ExtractedElement -> ElementType | None`, None = abstain) into the chain
— e.g. a future photo-vs-diagram rule goes before `rule_image_is_diagram`.

### VisionResult error-handling pattern

Vision providers NEVER raise on API/parsing failures. They return
`VisionResult(error=...)`; upstream stages decide what to do (retry,
embed the original image as-is, flag for review). Transient API errors
are retried at most twice inside the provider; permanent errors return
immediately. A response without a parseable Mermaid block sets
`mermaid_code=None` and `error`, while keeping `raw_model_output` for
debugging.

### Notes from Phase 2 (partially superseded by Phase 3)

- Test fixtures use the REAL samples in `samples/` (flowchart PNG appears
  in the DOCX at question 80). There is no real PDF sample; the PDF test
  builds one on the fly from the real assets (see `tests/conftest.py`).

## Phase 3 additions (structured diagrams, layout engine, native PPTX)

### Architecture pivot: structured JSON, native editable shapes

Mermaid output (Phase 2) is GONE. Diagrams must become **natively
editable shapes** in the output file (click-to-edit boxes and text), not
embedded images. The vision model now returns a structured
``DiagramGraph`` (topology + text only); geometry is computed
deterministically downstream.

### Schema (`app/models/diagram.py`)

- `DiagramNode`: id, shape_type (`ShapeType`: TERMINATOR, PROCESS,
  DECISION, INPUT_OUTPUT), text (Azerbaijani preserved exactly).
- `DiagramEdge`: source_id, target_id, label (`"Hə"`/`"Yox"`/None),
  is_back_edge (set by the layout engine, False from the vision model).
- `DiagramGraph`: nodes + edges — what the vision provider returns
  (`VisionResult.diagram_graph`, which replaced `mermaid_code`).
- `LayoutedNode` (= DiagramNode + x/y/width/height, layout px, top-left
  origin) and `LayoutedGraph` — what renderers consume.

### Vision provider (migrated to `google-genai` SDK)

The deprecated `google-generativeai` SDK was replaced with `google-genai`
in Phase 3: its TypedDict→schema conversion was broken (the model
consistently omitted the `edges` array). `google-genai` takes the
pydantic `DiagramGraph` directly as `response_schema` and works
first-try. JSON mode + schema enforcement; invalid/incomplete JSON is
retried like a transient error (same ×2 budget), then reported via
`VisionResult(error=...)` — the never-raise pattern is unchanged.

### Layout engine (`app/core/layout.py`) — rule-based, deterministic

1. Start node (no incoming edges) tops a single centre column; the main
   flow runs top-to-bottom.
2. A node with ≥2 outgoing edges (a decision) sends its first edge's
   target into a left sub-column, the second into a right sub-column,
   both one row below.
3. An edge whose target sits at/above its source's row is flagged
   `is_back_edge`; renderers route it around the columns.
4. Fixed sizes per shape (PROCESS/INPUT_OUTPUT 160×50, DECISION 180×90,
   TERMINATOR 140×50 px), widened when text won't fit at 12 pt.

### Renderers (`app/core/render/`)

- `svg_debug.py` — INTERNAL QA ONLY (future review panel). Never the
  client deliverable. Correct symbols, arrow markers, back-edge routed on
  a right-hand rail with a ↻ icon.
- `pptx_shapes.py` — the client deliverable for diagrams. Mapping:
  TERMINATOR → `MSO_SHAPE.ROUNDED_RECTANGLE`, PROCESS → `RECTANGLE`,
  DECISION → `DIAMOND`, INPUT_OUTPUT → `PARALLELOGRAM`. Real text frames
  (editable, Azerbaijani intact), native connectors attached via
  `begin_connect`/`end_connect` (arrows follow dragged shapes),
  back-edges as elbow connectors right-side to right-side, edge labels as
  small borderless text boxes. 1 layout px = 9525 EMU; diagrams are
  uniformly scaled down (never up) to fit the given slide bounds.

### DOCX native shapes — BUILT IN PHASE 4 (`app/core/render/docx_shapes.py`)

python-pptx has first-class AutoShape/connector APIs, so PPTX was built
first (Phase 3) to prove the shape model. python-docx has NO equivalent —
`docx_shapes.py` hand-builds WordprocessingML Drawing XML
(`wp:anchor` → `a:graphic` → `wps:wsp`, Word 2010+ wordprocessingShape)
as strings, parses with lxml, and injects the result into
`document.element.body` before `w:sectPr`. python-docx is used only for
document creation/saving.

- **Input contract**: the SAME `LayoutedGraph` as `pptx_shapes` — both
  renderers consume one layout computation, so the next phase (document
  assembly) can target either format.
- **shape_type → prstGeom**: TERMINATOR → `roundRect`, PROCESS → `rect`,
  DECISION → `diamond`, INPUT_OUTPUT → `parallelogram` (DrawingML preset
  names; same four symbols as the PPTX MSO_SHAPE mapping).
- **Text**: real editable runs inside `wps:txbx`/`w:txbxContent`,
  centred, font size from the same fit logic as PPTX
  (`pptx_shapes._font_size_for` is reused, 1 px = 9525 EMU).
- **Connectors**: `wps:wsp` with `wps:cNvCnPr` — `line` preset for
  same-column edges, `bentConnector2` for decision branches (exit side
  at mid-height, one bend, arrive at target top). Direction is encoded
  via flipH/flipV on `a:xfrm` (DrawingML extents must be positive).
  Arrowheads via `a:tailEnd type="triangle"`. Word connectors here are
  free-floating (no stCxn/endCxn attachment — that only works inside a
  drawing canvas, deliberately not used).
- **Back-edge**: the exact 3-segment rail route from
  `pptx_shapes._add_back_edge` (rail = rightmost shape edge + 50 px via
  the shared `_rail_x`); arrowhead only on the final segment.
- **Anchoring decision**: all drawings of a diagram live in ONE
  paragraph (one `w:r` per drawing), `relativeFrom="paragraph"` for both
  positionH and positionV. Rationale: every shape is positioned against
  the same anchor paragraph, so later edits/reflow move the whole
  diagram as one unit — shapes cannot drift apart; with
  `relativeFrom="page"` (still supported via the `relative_from` arg)
  text inserted above would slide under the pinned diagram instead.

Full document assembly (mixing text/tables/diagrams into one output) is
still pending — next phase.
