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
  (editable, Azerbaijani intact), native connectors attached at BOTH
  ends via `begin_connect`/`end_connect` (arrows follow dragged shapes),
  edge labels as small borderless text boxes. 1 layout px = 9525 EMU;
  diagrams are uniformly scaled down (never up) to fit the slide bounds.
  **Back-edge**: ONE `bentConnector3` (MSO_CONNECTOR.ELBOW) attached
  right-side (idx 3) to right-side (idx 3), with an explicit `adj1`
  adjustment value placing the vertical rail as a RATIO of the
  horizontal span between the connection points:
  `adj1 = (rail − source_right)/(target_right − source_right) × 100000`
  (negative in the normal loop case — the bend sits outside the
  endpoints' bounding box, right of the source). Computed by
  `back_edge_rail_adj()` (shared with the DOCX renderer); the rail
  clearance itself still comes from `_rail_x` (rightmost shape + 50 px).
  Because adj is a ratio, Office recomputes the entire path — including
  the rail — when either shape is dragged. The earlier 3-stitched-
  segments approach (fixed waypoints, attached at outer ends only,
  floating middle when shapes moved) is removed.

### DOCX native shapes — `app/core/render/docx_shapes.py` (Phase 4,
### restructured in Phase 4b)

python-pptx has first-class AutoShape/connector APIs, so PPTX was built
first (Phase 3) to prove the shape model. python-docx has NO equivalent —
`docx_shapes.py` hand-builds WordprocessingML Drawing XML as strings,
parses with lxml, and injects into `document.element.body` before
`w:sectPr`. python-docx is used only for document creation/saving.

**Phase 4b root cause of the original "Word cannot open" failure:**
Phase 4 wrote `wp:positionH relativeFrom="paragraph"` on every anchor.
`ST_RelFromH` has NO "paragraph" value (it exists only in `ST_RelFromV`);
valid H values are character/column/insideMargin/leftMargin/margin/
outsideMargin/page/rightMargin. Word hard-fails on the invalid enum with
the generic open error (not the repair dialog). lxml cannot catch this —
it is schema-level, not well-formedness. The legal value sets are pinned
in `tests/test_docx_shapes.py`.

**Structure (Phase 4b): one `wpc:wpc` drawing canvas per diagram.**
A single `w:p` → `w:r` → `w:drawing` → `wp:anchor` holds one
`a:graphicData uri=".../wordprocessingCanvas"` → `wpc:wpc`. ALL shapes
and connectors are `wps:wsp` children of that canvas, positioned in the
canvas' own EMU coordinate space (origin = canvas top-left) — no
per-shape anchors. Canvas extent = graph bounds + the back-edge rail.

- **Input contract**: the SAME `LayoutedGraph` as `pptx_shapes` — both
  renderers consume one layout computation, so the next phase (document
  assembly) can target either format.
- **shape_type → prstGeom**: TERMINATOR → `roundRect`, PROCESS → `rect`,
  DECISION → `diamond`, INPUT_OUTPUT → `parallelogram`.
- **Text**: real editable runs inside `wps:txbx`/`w:txbxContent`,
  centred; font size reuses `pptx_shapes._font_size_for` (1 px = 9525
  EMU).
- **Connectors are genuinely attached — ALL of them, at BOTH ends**:
  each shape carries a stable `wps:cNvPr id` (2, 3, …; id 1 is the
  anchor's `wp:docPr`), and every connector's `wps:cNvCnPr` holds
  `a:stCxn` AND `a:endCxn` referencing those sibling ids +
  connection-site idx (0=top, 1=left, 2=bottom, 3=right) — dragging a
  shape in Word drags its arrows. This only works inside a canvas,
  which is why Phase 4's per-shape anchors could not provide it.
  Presets: `line` for same-column edges, `bentConnector2` for decision
  branches; direction via flipH/flipV (extents must be positive);
  arrowheads via `a:tailEnd type="triangle"`.
- **Back-edge (single attached connector — same fix as PPTX)**: ONE
  `bentConnector3` `wps:wsp` with `a:stCxn`/`a:endCxn` on the source's
  and target's right sides (idx 3) and the same ratio-based `adj1` from
  the shared `pptx_shapes.back_edge_rail_adj()`. No stitched segments,
  no fixed waypoints — Word recomputes the whole path on drag.
- **Anchoring decision**: `relative_from="paragraph"` (default) maps to
  positionH `relativeFrom="column"` + positionV
  `relativeFrom="paragraph"` — the whole canvas flows with its anchor
  paragraph as one unit. `"page"` pins both axes to the page (text would
  reflow underneath; not the document default).

## Phase 5: full document assembly (`app/core/assembly/`)

`assemble_docx()` / `assemble_pptx()` consume the ordered
`list[ClassifiedElement]` and produce one complete Document /
Presentation, elements in SOURCE ORDER. Diagrams run the existing
vision → layout → shapes chain (renderers untouched). A
`vision_provider` can be injected (tests mock it; default lazily
creates the Gemini provider).

- **PPTX slide strategy — "flow-fill, diagrams on their own slide"**:
  the source is a textbook page, not a deck, and DOCX inputs carry no
  page geometry (`source_page` always 1), so estimated element heights
  drive pagination: text/tables stack top-to-bottom on the current
  10×7.5 in slide and a new slide starts when the next element won't
  fit; each DIAGRAM gets a dedicated slide at its position in the
  sequence (flowcharts are large; sharing with body text would shrink
  them illegibly). Content after a diagram starts on a fresh slide.
- **Tables**: rebuilt as native tables (python-docx "Table Grid" /
  pptx `add_table`) from the flattened tab/newline text +
  `n_rows`/`n_cols` metadata.
- **Subscripts**: the extractor now captures run-level
  `metadata["runs"]` (text + `vert_align`) for paragraphs containing
  subscript/superscript runs (e.g. q81's `41x + 36x = 77x`); both
  assemblers re-emit them via a shared `_emit_runs` helper
  (`font.subscript` in docx, `baseline` rPr attr in pptx). Note: the
  sample's real native tables contain NO subscripts — the subscripted
  q87 matching question exists only as a scanned image, now handled via
  the vision content_type path below.
- **Vision fallback**: if vision errors, OR returns a diagram with no
  edges, OR returns TEXT_CONTENT that isn't a usable two-list structure,
  the assemblers embed the ORIGINAL image instead of rendering broken
  content. (Before the content_type change below, q87 fell through here.)

### Vision content_type discriminator (in-vision text vs diagram)

The classifier still routes every image to vision unchanged; the
diagram-vs-text decision now happens INSIDE the single vision response.

- **One call, discriminated envelope**: the Gemini `response_schema` is
  `VisionAnalysis` (`models/document.py`): `content_type` ∈
  {`diagram`, `text_content`} plus an optional `diagram`
  (`DiagramGraph`) and `text_content` (`TextContent`). No second API
  call. `VisionResult` gained `content_type` + `text_content`;
  `diagram_graph` is unchanged, so the diagram path is untouched.
- **TEXT_CONTENT schema** (`models/text_content.py`) — scoped to the
  two-list *matching* structure actually seen (q87): `TextContent.lists`
  → `LabeledList.lines` → `TextLine.runs` → `TextRun{text, subscript}`.
  The label prefix ("1. ", "a. ") is part of the runs. Numeral-base
  notation splits into a normal run + a `subscript=True` base run
  (65₁₀ → `"65"` + `"10"`). `TextLine.to_run_dicts()` converts to the
  SAME `{text, vert_align}` convention the extractor emits, so the
  renderers reuse their existing subscript logic. Deliberately narrow:
  only the two-list layout is supported; a single list (or all-empty
  lists) is "not usable" → image fallback.
- **Rendering** (`_add_text_content` in both assemblers, NOT routed
  through `*_shapes.py`): a native 2-column table, left = first list,
  right = second list, row count = the longer list, shorter column's
  extra rows padded with empty cells (a natural matching-question
  layout). Subscripts are real subscript runs via the shared
  `_emit_runs`. In DOCX it's a "Table Grid" table in document flow; in
  PPTX it flows inline with surrounding text (not its own slide — it's
  textual content, unlike a diagram).
- **Validated on the real sample**: live Gemini now classifies q87 as
  TEXT_CONTENT and both outputs render it as an editable 3+5-line
  2-column table with every subscript (65₁₀, 110011₂, 1B₁₆, 101₈, …)
  correct — zero embedded pictures. q80 flowchart and the three native
  tables are unaffected.
- **DOCX wrapping**: the assembler post-processes each inserted diagram
  anchor from `wp:wrapNone` to `wp:wrapTopAndBottom` so following
  paragraphs flow below the canvas (docx_shapes itself is unchanged).
- **Styling fidelity is explicitly limited**: original fonts, sizes,
  colors, and paragraph styling are NOT reproduced — output uses
  reasonable defaults (12 pt, default theme). Only text content, order,
  table structure, and sub/superscripts are preserved.
- **PPTX shape fill fix**: node shapes now set an explicit white
  `solidFill`, black 1.5 pt outline, black text, and no inherited
  shadow — previously they inherited the theme `fillRef idx="3"`
  (a GRADIENT style → the banding seen on process boxes).

`scripts/assemble_document.py` runs the whole chain on the real sample
and package-validates both outputs. Remaining later work: queue system,
frontend, batch processing.
