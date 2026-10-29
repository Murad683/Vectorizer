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

### TEXT_CONTENT heading + borderless table (post-Phase-5 fix)

Comparing q87's rendered output against the original scan (`table_87.png`)
found two remaining gaps, now fixed:

- **Missing heading.** `TextContent` (`models/text_content.py`) gained an
  optional `heading: str | None` field — the instruction line above the
  two lists (e.g. "87. Uyğunluğu müəyyən edin."), preserved exactly
  including the leading question number. The Gemini prompt
  (`vision/gemini_provider.py`) was extended to fill it, explicitly told
  to leave it `None` rather than invent text — still one vision call, no
  schema/call added elsewhere. Both assemblers emit it as a plain
  paragraph/text box directly above the table, using the same styling as
  other TEXT elements (`_add_heading` in `pptx_assembler.py`; a plain
  `document.add_paragraph()` call in `docx_assembler.py`), only when
  present.
- **Borderless table, TEXT_CONTENT only.** The matching-question table has
  no visible grid in the source (students draw connecting lines by hand),
  so its borders are now stripped: DOCX sets `w:tblBorders` to `nil` on
  all six edges (`_remove_table_borders`, inserted into `tblPr` in schema
  order — before `w:tblLook`, after `w:tblW`); PPTX sets each cell's four
  `a:lnL/lnR/lnT/lnB` to `noFill` (`_remove_pptx_table_borders`, elements
  built in reverse and inserted at index 0 so the final child order
  matches the `CT_TableCellProperties` schema). This is scoped to
  `_add_text_content` only — the three native data tables (Marka, ASCII,
  olimpiada) are built by separate `_add_table` functions and keep
  `"Table Grid"` borders untouched. (Word's non-printing "Table
  Gridlines" view aid may still show faint lines while editing even with
  borders set to nil — normal Word behavior, doesn't print/export.)
- **Validated on the real sample**: q87's heading paragraph now sits
  immediately before its table in document/shape order in both formats,
  the table's borders are nil/noFill, the three data tables are
  unaffected, subscripts/alignment/empty-cell padding are unchanged, and
  package validation still passes on both outputs.

### PPTX table styling is a SEPARATE layer from border lines (post-fix #2)

The border-line fix above was not sufficient in PPTX: q87's table still
rendered with a colored header row and banded rows, identical to the real
data tables. Root cause — PPTX tables carry border lines
(`a:lnL`/`a:lnR`/`a:lnT`/`a:lnB` per cell) and table *styling*
(`a:tableStyleId` + `firstRow`/`bandRow` flags on `a:tblPr`) as two
independent OOXML mechanisms; zeroing the former does nothing to the
latter. `pptx_assembler.py` now also clears the styling layer for
TEXT_CONTENT tables, via `_remove_pptx_table_style`:

- `firstRow="0"` and `bandRow="0"` set explicitly on `a:tblPr` (disables
  the style's header-color and row-banding behavior).
- `a:tableStyleId` repointed from the default style GUID
  (`{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}`, shared with the three real
  data tables) to PowerPoint's built-in **"No Style, No Grid"** GUID
  (`{2D5ABB26-0587-4C30-8999-92F81FD0307C}`) — a preset with no fills or
  borders defined, resolved by PowerPoint itself at render time (there is
  no `tableStyles.xml` part in the file; python-pptx's default template
  ships none either, confirming these are client-resolved built-ins, not
  something we can inspect locally).

Scoped strictly to `_add_text_content`'s table — `_add_table` (the three
real data tables) is untouched, confirmed by diffing `a:tblPr`
attrs/`tableStyleId` between the pre- and post-fix PPTX: the data tables
on slides 2, 5, and the first table on slide 6 are byte-identical
(`firstRow=1`, `bandRow=1`, default style GUID); only q87's table on
slide 6 changed. DOCX `word/document.xml` is byte-identical before/after
this fix — confirming it's PPTX-only, as intended (DOCX has no
equivalent style-GUID/banding mechanism; `w:tblBorders=nil` was already
sufficient there).

`scripts/assemble_document.py` runs the whole chain on the real sample
and package-validates both outputs.

## Phase 6: background processing queue (`app/core/queue/`, `app/api/routes/jobs.py`)

### What it does

Wraps the existing pipeline (ingestion → classification → vision →
layout → assembly) in a persistent, retryable background job queue.
Clients upload a file, get a job id back immediately, then poll for
status — no request blocks on the full pipeline. The pipeline code
itself is unchanged; this phase only wraps it.

### Architecture

- **SQLite-backed, in-process** — no external broker (Redis/Celery).
  The `jobs` table stores all state; a single in-process asyncio task
  polls for pending jobs and runs them. This matches the Phase 1
  decision: "SQLite for MVP, designed so it can migrate to
  Postgres/a real queue later without reworking the processing pipeline."
- **Sequential processing** — one job at a time. The pipeline is
  synchronous (blocking Gemini calls, file I/O), run via
  `asyncio.to_thread` so the FastAPI event loop stays responsive.
  Concurrency can be added later once the single-job path is proven
  correct.
- **No new external dependencies** — only `python-multipart` (FastAPI
  file-upload parsing) and `aiofiles` (async file streaming for
  `FileResponse`).

### Job state machine (`app/core/queue/models.py`)
::

    PENDING ──→ PROCESSING ──→ COMPLETED
       ↑              │
       └──────────────┘  (retry: count < max)
                      │
                      └──→ FAILED  (retries exhausted)

- `PENDING → PROCESSING`: claimed atomically by the worker via
  `claim_next_pending_job` (``BEGIN IMMEDIATE`` transaction — two
  workers cannot claim the same row).
- `PROCESSING → COMPLETED`: pipeline finished, both output paths set.
- `PROCESSING → PENDING`: pipeline raised, `retry_count` incremented,
  `retry_count < max_retries` (default 2) — the job re-enters the queue
  and will be retried on the next poll cycle.
- `PROCESSING → FAILED`: pipeline raised, retries exhausted — terminal
  state, `error_message` populated with the exception + traceback.

### Job fields

id (uuid), status, input_filename, input_path, output_docx_path
(nullable), output_pptx_path (nullable), error_message (nullable),
retry_count (default 0), max_retries (default 2), created_at,
updated_at.

### Store (`app/core/queue/store.py`)

`JobStore` — synchronous SQLite wrapper (WAL mode, 5 s busy timeout).
Functions: `create_job`, `get_job`, `list_jobs` (optional status
filter), `update_job` (arbitrary field updates),
`claim_next_pending_job` (atomic oldest-PENDING claim).

**Superseded by Phase 9**: the SQLite backend described here was
replaced with PostgreSQL. All method signatures are unchanged — see
the Phase 9 section below for the current implementation.

### Worker (`app/core/queue/worker.py`)

An `asyncio.Task` started in the FastAPI lifespan, polling every 2 s.
Claims a job, runs `_process_job_sync` (the full pipeline: extract →
classify → assemble_docx + assemble_pptx) in a thread, updates the job
to COMPLETED or retries/FAILs on exception. Catches all exceptions to
prevent the loop from crashing.

### API endpoints (`app/api/routes/jobs.py`)

- **`POST /jobs`**: accepts a file upload (PDF/DOCX), validates format
  and `MAX_UPLOAD_SIZE_MB`, saves the file, creates a PENDING job,
  returns `{job_id, status}` (201).
- **`GET /jobs/{id}`**: returns the job's current status, timestamps,
  `available_formats` (if COMPLETED), or `error_message` (if FAILED).
- **`GET /jobs/{id}/download?format=docx|pptx`**: streams the output
  file (200). Returns 404 if the job doesn't exist, 409 if the job
  hasn't completed yet, 400 if the format is invalid.

All error responses use the existing `ErrorResponse` shape
(`error_code`, `message`, `request_id`).

### Lifecycle wiring (`app/main.py`)

The FastAPI `lifespan` context manager creates the `JobStore` (stored
on `app.state.job_store`), launches the worker task, and tears both
down on shutdown. The `data/` directory holds `jobs.db`, `uploads/`,
and per-job output directories.

### Validated on the real sample

Upload via `POST /jobs` → returns immediately with PENDING →
`GET /jobs/{id}` shows COMPLETED after ~10 s → both DOCX and PPTX
downloaded via `/download`, package-validated (every XML part parses),
same correct output as `scripts/assemble_document.py`. Retry logic
tested: a mocked pipeline failure retries `max_retries` times, then
lands in FAILED with a populated `error_message`. Oversized/unsupported
uploads rejected before any job is created.

Remaining later work: frontend, batch processing.

## Phase 7: JWT authentication & user management (`app/core/auth/`, `app/api/routes/auth.py`, `app/api/routes/users.py`)

### What it does

Protects all job endpoints behind JWT authentication and adds role-based
access control (admin, member). Jobs are now owned by the user who created
them; members can only access their own jobs, admins can access any job.

### Architecture

- **JWT (HS256, 24 h expiry)** via PyJWT. Token payload: `sub` (user id),
  `role`, `iat`, `exp`. Secret from `JWT_SECRET` env var (required in
  prod).
- **Password hashing** via passlib with bcrypt backend (passlib 1.7.4 +
  bcrypt 4.2.1 — bcrypt 5.x is incompatible with passlib).
- **No external dependencies** beyond PyJWT, passlib, bcrypt — no OAuth
  provider, no session store.

### User model (`app/core/auth/models.py`)

`User`: id (uuid), email (unique), hashed_password, role (`Role.ADMIN` |
`Role.MEMBER`), is_active (default True), created_at.

### Store (`app/core/auth/store.py`)

`UserStore` — same pattern as `JobStore` (SQLite, WAL mode, 5 s busy
timeout). UNIQUE constraint on email at DB level (catches races).
Functions: `create_user`, `get_user_by_email`, `get_user_by_id`,
`list_users`, `has_any_admin`.

**Superseded by Phase 9**: the SQLite backend described here was
replaced with PostgreSQL. All method signatures are unchanged — see
the Phase 9 section below for the current implementation.

### Security (`app/core/auth/security.py`)

`hash_password`, `verify_password` (passlib CryptContext),
`create_access_token`, `decode_access_token` (PyJWT).

### Dependencies (`app/core/auth/dependencies.py`)

- `get_current_user`: extracts JWT from `Authorization: Bearer <token>`,
  decodes, verifies user exists and is active. 401 on missing/invalid/
  expired token or deactivated user.
- `require_admin`: 403 if user.role != ADMIN.
- `_AuthError`: exception class wrapping a `JSONResponse` — allows
  dependencies to short-circuit; caught by a global exception handler in
  `main.py`.

### API endpoints

- **`POST /auth/login`** (`app/api/routes/auth.py`): email + password →
  JWT. Same generic 401 message for wrong email vs wrong password (no
  information leakage). Inactive users also get 401.
- **`POST /users`** (`app/api/routes/users.py`, admin-only): create user
  with email, password, optional role. 409 on duplicate email. Response
  never includes password hash.
- **`GET /users`** (`app/api/routes/users.py`, admin-only): list all
  users, no password hashes.

### Job ownership (`app/core/queue/models.py`, `app/api/routes/jobs.py`)

`Job` gained `owner_user_id: UUID | None`. `POST /jobs` sets it from the
authenticated user. `GET /jobs/{id}` and `GET /jobs/{id}/download` check
ownership — non-admin users get 403 on jobs they don't own.

### Bootstrap admin (`app/main.py`)

`_bootstrap_admin` runs on startup: if `has_any_admin()` is False and
`ADMIN_EMAIL` + `ADMIN_PASSWORD` env vars are set, creates an admin user.
Idempotent — does nothing once any admin exists, so subsequent restarts
don't duplicate the account.

### Config additions (`app/config.py`, `.env`)

`jwt_secret`, `admin_email`, `admin_password` — all required in prod
mode (validated by `_require_secrets_in_prod`).

### Validated

- Bootstrap admin created on first startup, not duplicated on restart.
- Admin login returns valid JWT; JWT used to create a member user.
- Member can create and access own jobs; gets 403 on other users' jobs
  and on `POST /users` / `GET /users`.
- No token / invalid token / expired token → 401 on all job endpoints.
- Wrong password → 401 with generic message (same as wrong email).
- Full test suite: 108 tests, 0 failures, 0 regressions on Phase 1–6.

## Phase 8: Frontend UI (`frontend/src/`)

### Design decisions

- **Palette usage:** `#151515` (dark) as the login screen background and
  primary text. `#FEE500` (accent) used in exactly two deliberate places:
  the login "Daxil ol" button (the single primary action) and the
  vectorize-shape animation. `#FFFFFF` as main content background.
  `#FAFAFA` as subtle surface (hover states, badges). `#EFEFEF` as
  borders. `#333333` as secondary text. `#888888` as muted/helper text.
- **Typography:** Inter (Google Fonts, weights 400/500/600), 14px base
  implied by Tailwind defaults, tight tracking. Headings use 500–600
  weight, same family. No decorative fonts.
- **Layout:** Single-column, centered (max-width 672px / `max-w-2xl`).
  Top nav bar with logo + user dropdown. No sidebar — this is a
  single-purpose tool, not a complex admin dashboard.
- **Signature element:** A CSS `clip-path` animation (`pixel-to-vector`)
  that morphs a jagged, pixelated polygon into a clean hexagon — embodying
  the product's core transformation (rough scan → clean vector). Used on
  the login screen (accent-colored, 64px), the nav bar logo (accent,
  20px), and the empty state (border-colored, 48px, subtle).
- **No dark mode:** The tool is used in daytime work settings. A single
  light theme keeps things simple and consistent.
  **Superseded by Phase 11** — dark mode was added as the default
  theme; see that section for the current token/typography system.

### Frontend structure

```
src/
├── api/client.ts          # Fetch wrapper, token management, typed API methods
├── context/AuthContext.tsx # Auth state (user, login, logout) via React Context
├── pages/
│   ├── LoginPage.tsx       # Email/password login with error handling
│   ├── DashboardPage.tsx   # Upload zone + job list with polling
│   └── UsersPage.tsx       # Admin-only user management
├── components/
│   ├── UploadZone.tsx      # Drag-and-drop + click-to-browse file upload
│   └── JobList.tsx         # Job rows with status badges + download buttons
├── App.tsx                 # Hash-based router (login → dashboard | users)
├── main.tsx                # React root
└── index.css               # Tailwind v4 import + custom animations
```

### Auth / session handling

- **JWT stored in memory only** (React state via `AuthContext`). Not
  persisted to `localStorage` or cookies — avoids XSS risk. On page
  refresh the user re-authenticates. For a small team on persistent
  browser tabs with 24h token lifetime, this is the right tradeoff.
- **401 auto-redirect:** The API client (`client.ts`) has a global
  `onUnauthorized` callback that calls `logout()`, which clears the
  in-memory token and user state, causing React to re-render the login
  page. Any authenticated API call that returns 401 triggers this.
- **JWT payload decoded client-side** (base64 `atob`) to extract `role`
  for UI gating — no extra `/me` endpoint needed.

### Routing

Hash-based (`window.location.hash`), no external router library. Three
states: unauthenticated → `LoginPage`; authenticated → `DashboardPage`;
authenticated admin + `#users` → `UsersPage`. Members navigating to
`#users` see the dashboard (the `UsersPage` component is never rendered,
not just hidden).

### Polling

`DashboardPage` polls `GET /jobs` every 4 seconds via `setInterval`.
This keeps in-progress jobs' statuses live. The interval is fixed (not
adaptive) — sufficient for the team size and job throughput.

### API proxy

Vite dev server proxies `/api/*` → `http://localhost:8000/*` (strips
`/api` prefix). The API client prefixes all paths with `/api`.

### Backend addition (deviation from spec)

Phase 6 had no `GET /jobs` list endpoint — only `GET /jobs/{id}`. The
dashboard needs to list jobs, so a `GET /jobs` endpoint was added to
`app/api/routes/jobs.py`: returns all jobs for admins, only owned jobs
for members. Uses the existing `JobStore.list_jobs()` and
`_job_response()`. This is the only backend change in Phase 8.

### Azerbaijani UI copy

All labels, buttons, status text, error messages, and empty states are
in Azerbaijani. Key terms: "Daxil ol" (log in), "Çıxış" (log out),
"Yükləmələr" (uploads), "Gözləyir" (pending), "Emal olunur"
(processing), "Hazırdır" (ready), "Xəta" (error), "İstifadəçilər"
(users). Error messages are actionable: the empty state invites the
first upload; login failures show a generic credential error; file
validation errors name the constraint (format, size limit).

### Validated

- Full flow: bootstrap admin login → create member → member login →
  upload real sample → status moves pending → processing → completed →
  download both DOCX and PPTX (200, correct files).
- Member cannot see `#users` page or admin menu items (not rendered in
  DOM).
- Member dropdown shows "İstifadəçi" role, no admin options.
- Admin dropdown shows "Admin" role with "İstifadəçiləri idarə et" link.
- Invalid/expired token → 401 → redirected to login.
- Wrong password → generic error in Azerbaijani.
- Zero console errors during full flow.
- Backend test suite: 108 tests, 0 failures, 0 regressions.
- **Visual quality caveat:** The UI has been functionally validated but a
  human needs to look at it to judge whether it reads as professional.
  Screenshots were not available in the test environment.

## Phase 9: SQLite → PostgreSQL migration (`app/core/queue/store.py`, `app/core/auth/store.py`, `migrations/`)

### What it does

Replaces the SQLite persistence layer under `JobStore` and `UserStore`
with PostgreSQL, running as its own `postgres` service in
docker-compose. Pure persistence-layer swap: every method signature on
both stores is unchanged, so nothing outside these two files needed to
change except one exception-type catch (see below). This is the
migration point flagged from Phase 1 onward ("SQLite for MVP, designed
to migrate to Postgres later without reworking the pipeline") —
undertaken now, pre-launch, because SQLite's file-based storage risks
data loss across container restarts/redeploys if a volume isn't
mounted exactly right, and Postgres is the team's operational standard.

### Why psycopg (v3) + hand-written SQL, not an ORM

The codebase already had a consistent style before this phase: no ORM
anywhere, hand-rolled SQL in the SQLite-era stores, Pydantic models for
validation/serialization only. Introducing SQLAlchemy's ORM here for
two simple tables (`jobs`, `users`) would add an abstraction layer the
rest of the project doesn't use, for no real benefit at this scale.
`psycopg[binary]==3.3.4` (sync driver — matches the existing
synchronous store methods and the single-event-loop-thread execution
model, see below) with hand-written parameterized SQL keeps the same
shape as the code it replaces. Alembic is used for migrations (see
below) — it's the standard choice and requires SQLAlchemy as its own
internal dependency for the engine/connection abstraction, but no
SQLAlchemy ORM models exist anywhere in the app; migrations are plain
SQL via `op.execute()`.

### Connection model — one long-lived connection per store, autocommit

Each store (`JobStore`, `UserStore`) opens **one** `psycopg.connect(...,
autocommit=True)` connection at construction (mirroring the old
SQLite `check_same_thread=False` single-connection pattern) and keeps
it for the store's lifetime. This is safe because every store call is
synchronous with no `await` inside it — on a single-worker Uvicorn
event loop, only one such call ever executes at a time regardless of
how many requests are in flight, so there's no concurrent-access race
on the connection object. `autocommit=True` means every method is its
own implicit transaction; no manual `BEGIN`/`COMMIT`/`ROLLBACK` is
needed anywhere, including after an error (e.g. a duplicate-email
`UniqueViolation` doesn't leave the connection in an aborted-transaction
state the way it would under an explicit multi-statement transaction).
The connection sets `SET TIME ZONE 'UTC'` once so all returned
`TIMESTAMPTZ` values come back timezone-aware in UTC, matching the old
code's explicit `datetime.now(timezone.utc)` semantics.

**Known gap, not fixed in this phase**: because it's a single
long-lived connection with no pool and no reconnect/retry logic, a
Postgres restart severs it — the running backend process needs its own
restart afterward to reconnect (confirmed during validation: after
restarting the `postgres` container mid-session, requests to the
already-running `backend` container failed until `backend` was
restarted too). This matches the old SQLite behavior's total absence of
any resilience concern (a file can't "go down"), so it isn't a
regression, but it's a real operational point if the client wants
Postgres blips to be transparent to the running backend — that would
need connection pooling (e.g. `psycopg_pool`) with retry, which is
additional infrastructure beyond a straight persistence swap and was
out of scope here.

### `claim_next_pending_job` — SKIP LOCKED, not a translated `BEGIN IMMEDIATE`

The SQLite version used `BEGIN IMMEDIATE` (a whole-database write lock)
to make claiming atomic. The Postgres equivalent is NOT a literal
translation of that locking strategy — it uses the standard Postgres
job-queue pattern, `SELECT ... FOR UPDATE SKIP LOCKED`, inside a single
`UPDATE ... FROM ... RETURNING` statement:

```sql
WITH next_job AS (
    SELECT id FROM jobs
    WHERE status = 'pending'
    ORDER BY created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs
SET status = 'processing', updated_at = %s
FROM next_job
WHERE jobs.id = next_job.id
RETURNING jobs.*
```

Concurrent claimers skip rows another in-flight claim has already
locked, rather than blocking on them or locking the whole table — the
first to reach a given row wins it, everyone else moves on to the
next-oldest unlocked PENDING row (or gets `None`). Being a single SQL
statement, it's atomic under `autocommit=True` with no explicit
transaction block needed. Verified directly:
`tests/test_queue.py::test_concurrent_claims_never_double_claim` opens
8 independent `JobStore` connections (real concurrency, not the
single-connection-per-process model the app itself uses) racing via
real OS threads to claim 8 seeded jobs — every job is claimed by
exactly one worker, all 8 are claimed, matches the seeded set.

### Migrations (`alembic.ini`, `migrations/`)

Alembic manages schema as trackable, repeatable migrations instead of
ad-hoc `CREATE TABLE IF NOT EXISTS` calls. `migrations/env.py` builds
the connection URL from `Settings.database_url` (rewritten to the
`postgresql+psycopg://` SQLAlchemy dialect string) rather than a
hardcoded `sqlalchemy.url` in `alembic.ini`, so credentials live in one
place; `DATABASE_URL` / `TEST_DATABASE_URL` env vars override it (used
by the test suite and docker-compose's `backend` command).
`migrations/versions/0001_initial_schema.py` creates `jobs` and `users`
as raw SQL (`UUID`/`TIMESTAMPTZ`/`BOOLEAN` native Postgres types,
indexes on `jobs(status, created_at)` for the claim query and
`jobs(owner_user_id)` for ownership filtering) — same column set as the
old SQLite schema, no data migration needed (see below).

Migrations run automatically as part of `docker-compose up`: the
`backend` service's command is
`sh -c "alembic upgrade head && uvicorn ..."`, gated behind
`depends_on: postgres: condition: service_healthy` so it never races
Postgres's own startup.

### Config (`app/config.py`)

Added `postgres_host` (default `localhost`), `postgres_port` (default
`5432`), `postgres_user`, `postgres_password`, `postgres_db` — all
required in prod mode via the existing `_require_secrets_in_prod`
pattern. `Settings.database_url` is a computed property returning a
plain `postgresql://` conninfo string, used directly by
`psycopg.connect()` in both stores and by `main.py`'s lifespan.

### docker-compose services

- **`postgres`**: official `postgres:16-alpine` image, named volume
  (`postgres_data`) for persistence, `env_file: ./backend/.env` (no
  credentials hardcoded in the compose file — `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB` come from there), healthcheck via
  `pg_isready`. Host-side port is **5434**, not 5432 — this machine
  already had a native Postgres service bound to 5432, and remapping
  avoided that collision rather than requiring it be stopped.
- **`backend`**: `depends_on: postgres: condition: service_healthy` (a
  real health-gate, not just container-start-order) plus an
  `environment:` override (`POSTGRES_HOST: postgres`,
  `POSTGRES_PORT: "5432"`) — this overrides `backend/.env`'s host-side
  values (`localhost:5434`, for running the backend directly via
  venv/uvicorn on the host) so the containerized backend reaches
  Postgres by compose service name on its normal internal port.
- **`postgres-test`** (profile `test`): ephemeral database for the test
  suite — `postgres:16-alpine`, `tmpfs` data dir (no persistence
  needed), host port **5433**, throwaway credentials (`test`/`test`)
  inlined directly in the compose file rather than sourced from
  `backend/.env` since they're not real secrets. Started with
  `docker compose --profile test up -d postgres-test`.

### Test-time Postgres

Chose a **docker-compose test profile** (`postgres-test`) over
testcontainers or requiring a developer's own always-on Postgres,
because it needed no new test dependency and matches the project's
existing docker-compose-centric workflow. `tests/conftest.py` adds:
`TEST_DATABASE_URL` (env-overridable, defaults to the `postgres-test`
service), a session-scoped autouse fixture that runs
`alembic upgrade head` against it once, and `job_store`/`user_store`
function-scoped fixtures that `TRUNCATE jobs, users RESTART IDENTITY
CASCADE` before yielding a fresh store — cheap per-test isolation
without spinning a new database per test. `test_queue.py`,
`test_jobs_api.py`, and `test_auth.py`'s local `store`/`_stores`
fixtures were updated to depend on these shared fixtures instead of
constructing SQLite files under `tmp_path`; CI
(`.github/workflows/ci.yml`) adds a native GitHub Actions `postgres`
service container with matching credentials/port for the same purpose.

### Data continuity — no migration script

Pre-launch: no real production data exists in the client's hands yet,
so this migration starts fresh rather than porting rows out of the old
SQLite files. **This assumption was not re-confirmed with the user
before implementing** — flagging it explicitly per the phase brief's
own instruction to check first if unsure; if any SQLite data from
earlier local runs needs preserving, say so and a one-off export/import
script can be written before the old `.db` files are deleted.

### Removed

`sqlite3` imports and all `CREATE TABLE IF NOT EXISTS`/`db_path`-based
code paths are gone from both stores; `app/api/routes/users.py`'s
duplicate-email catch changed from `sqlite3.IntegrityError` to
`psycopg.errors.UniqueViolation` (the necessary, minimal consequence of
the persistence swap — the exception type a store raises on a UNIQUE
violation depends on which database's driver raised it). `app/main.py`
no longer creates a `data/` directory for `.db` files (uploads/outputs
still live under `data/`, unrelated to this phase).

### Validated

- `docker-compose up -d postgres backend frontend`: `backend` correctly
  waited for `postgres`'s healthcheck (`Container ... Waiting` →
  `Healthy` → `backend` starts) before running migrations and serving;
  confirmed via `docker logs` showing the Alembic log lines before
  Uvicorn's startup line.
- Full test suite (109 tests — 108 existing + 1 new concurrency test)
  passes against the `postgres-test` service, 3 consecutive full runs
  clean. Zero SQLite dependency remains in `app/`.
- Full Phase 8 end-to-end flow re-run against the dockerized stack:
  bootstrap admin login → admin creates a member → member logs in →
  uploads the real sample → job moves pending → processing → completed
  → both DOCX and PPTX downloaded and confirmed valid Office packages
  (`word/document.xml` / `ppt/slides/slideN.xml` present).
- **Bootstrap admin idempotency confirmed via container restart, not
  just a unit test**: restarted the `postgres` container, then the
  `backend` container: `docker logs` showed no "Bootstrap admin user
  created" line on the second startup (only the migration + normal
  startup lines) — `has_any_admin()` correctly found the
  already-persisted admin and skipped creation.
- **Durability concretely proven**: after restarting the `postgres`
  container (then `backend`, per the connection-model gap noted
  above), the previously created admin, member, and completed job (with
  working downloads) were all still present and correct — the exact
  failure mode SQLite's file-based storage risked is what this
  migration eliminates.
- `claim_next_pending_job`'s new SKIP LOCKED logic verified both by the
  existing single-claim/oldest-first tests and the new real-concurrency
  test (8 threads, 8 independent connections, 8 jobs, zero double-claims).
- `ruff`/`black`/`mypy` clean on every file this phase created or
  substantively rewrote (`config.py`, both stores, `main.py`,
  `migrations/env.py` and the initial migration); pre-existing lint/type
  debt in untouched files (route handlers' `Depends()` pattern,
  `pptx_assembler.py`/`docx_assembler.py` missing annotations,
  `security.py` missing type stubs) was left alone as out of scope for
  a persistence-only phase.

### Known deviations from the spec

- **`GET /jobs` list endpoint** (added in Phase 8, not this phase) is
  unaffected by this migration but is called out here for continuity —
  no new deviation introduced in Phase 9 itself.
- **Frontend↔backend container networking gap — fixed as a follow-up,
  see below**: `frontend/vite.config.ts`'s dev-server proxy targeted
  `http://localhost:8000`, which only resolved when the frontend ran
  directly on the host (how Phase 8 was validated). Flagged here as
  out of scope for the Postgres migration itself; resolved in the
  dedicated follow-up documented in its own section further down.
- **Host Postgres port remapped to 5434** (not 5432) to avoid colliding
  with a pre-existing native Postgres service already bound to 5432 on
  this machine — noted above under docker-compose services, mentioned
  again here since it's a deviation from the "standard env vars" a
  reader might expect port 5432 to mean.

## Fix: Vite dev proxy target made configurable (`frontend/vite.config.ts`)

### What it does

Resolves the Phase 9 networking gap above: the Vite dev server's `/api`
proxy target is now read from `VITE_API_PROXY_TARGET` (via Vite's
`loadEnv`, since `vite.config.ts` runs in Node, not the browser, so
`import.meta.env` isn't available there) instead of being hardcoded to
`http://localhost:8000`.

- **Default** (unset): `http://localhost:8000` — unchanged behavior for
  host-run dev (`npm run dev` / `make dev`), zero required config.
- **docker-compose**: the `frontend` service now sets
  `VITE_API_PROXY_TARGET: http://backend:8000` directly in
  `docker-compose.yml`'s `environment:` block — `backend` is the
  compose service name, resolved via Docker's internal DNS, correct for
  container-to-container traffic (unlike `localhost`, which inside the
  frontend container refers to the container itself). `frontend` also
  gained `depends_on: backend: condition: service_started` so it isn't
  racing backend's own startup.
- Also added `changeOrigin: true` to the proxy config — standard Vite
  practice when proxying to a different host, keeps Host-header
  behavior correct in the container case.
- `frontend/.env.example` documents the variable, following the same
  pattern as `backend/.env.example` (committed template, real
  `frontend/.env` gitignored — already anticipated in the root
  `.gitignore` from Phase 1, never actually created since nothing
  needed it until now). No `frontend/.env` file was created — the
  default covers host-run dev, and docker-compose sets the var itself.

### Validated

- **Host-run dev** (`uvicorn` + `vite` directly on the host, no
  `frontend/.env` present): logged in through the browser at
  `localhost:5173`, dashboard loaded showing prior data — proxy
  correctly fell back to `localhost:8000` with zero config.
- **Full dockerized stack** (`docker-compose up -d postgres backend
  frontend`, rebuilt to pick up the change): logged in as the bootstrap
  admin through the browser at `localhost:5173` (routed through the
  frontend container → `backend:8000` via Docker DNS), created a new
  member user, logged in as that member, uploaded the real sample via
  the API (job appeared in the dashboard within one poll cycle showing
  PENDING → PROCESSING → COMPLETED live), and downloaded both DOCX and
  PPTX by clicking the actual buttons in the browser (`GET
  .../download?format=docx` and `...=pptx`, both 200, confirmed via
  network request inspection) — the first time this full flow has been
  exercised through the browser against the fully containerized stack
  rather than via direct API calls.
- Zero console errors on a fresh tab load against the dockerized stack.
- Backend test suite unaffected (109 tests, unchanged) — this was a
  frontend-only networking config fix, no backend/auth/queue/pipeline
  code touched.

## Phase 10: Dashboard redesign — history sidebar, job & user deletion

### What it does

Restructures the dashboard from a single flat page (upload zone on top,
job list below) into a two-pane layout: a persistent left sidebar
listing past uploads, and a main panel that shows either the upload
zone (home state, no job selected) or the selected job's detail — the
same structural pattern chat apps use for history vs. active
conversation. Adds the ability to delete a job and to
deactivate/delete a user, both previously missing.

### Layout structure (`frontend/src/pages/DashboardPage.tsx`)

```
┌─ header (unchanged: logo, user menu) ──────────────┐
├─ sidebar (w-72) ─┬─ main panel (max-w-2xl, centred) ┤
│  + Yeni yükləmə   │                                  │
│  ─────────────    │  selectedJob === null:           │
│  job entry 1       │    <UploadZone/>                 │
│  job entry 2  ←──  │  selectedJob !== null:           │
│  job entry 3       │    <JobDetail/>                  │
│  (scrolls)         │  (panel swap, no page reload)    │
└────────────────────┴──────────────────────────────────┘
```

- **Sidebar scope — admins see all jobs, same as Phase 8**: the
  requirement asked for a decision on whether admins see everyone's
  jobs in the sidebar or just their own. Kept the existing `GET /jobs`
  semantics unchanged (all jobs for admins, own jobs for members,
  established in Phase 8) rather than restricting the sidebar to
  "my jobs only" for admins too — that would have been a behavior
  regression from what's already shipped, and building a separate
  admin-only "all jobs" view was more scope than this phase asked for.
  No job-owner column was added to sidebar entries (not requested).
- **Selected state**: sidebar entry gets `bg-surface` (the same
  understated hover-surface token already used everywhere else) —
  no new color introduced, matches "Follow the token/typography/layout
  decisions already established in Phase 8" exactly.
- **Compact status indicator**: a small colored dot (`StatusDot` in
  `components/StatusBadge.tsx`) + short Azerbaijani label + relative
  timestamp (`formatRelativeTime` in `lib/jobUtils.ts`: "indi", "5
  dəq", "3 saat", "2 gün", falling back to a short date past a week).
- **Auto-select after upload**: `UploadZone`'s `onUploaded` callback
  now passes back the created `job_id` (previously fire-and-forget);
  `DashboardPage.handleUploaded` sets it as `selectedJobId` and
  refetches the job list — the new job becomes the active main-panel
  item immediately, no empty-zone confusion.
- **Empty sidebar state**: matches the existing empty-state visual
  language (the `vectorize-shape` signature element, muted/border
  colored) with copy pointing at the upload button rather than a bare
  "no data" message.

### New components

- `components/Sidebar.tsx` — the job list + delete-on-hover with a
  two-button inline confirm ("Silinsin? Bəli / Xeyr") replacing the
  trash icon, rather than a modal — kept lightweight and consistent
  with the minimalist direction.
- `components/JobDetail.tsx` — the main panel's selected-job view:
  filename, status, download buttons (completed), error message
  (failed), a spinner+message row (pending/processing), and its own
  delete affordance with the same two-step confirm pattern ("Silinsin?
  Bu geri qaytarıla bilməz." / "Bəli, sil" / "Ləğv et").
- `components/StatusBadge.tsx` — the existing pill badge plus a new
  `StatusDot` export (compact variant for the sidebar).
- `lib/jobUtils.ts` — `STATUS_LABELS`, `formatDate`,
  `formatRelativeTime`, `downloadJobFile`, extracted from the now-
  deleted `components/JobList.tsx` (superseded by Sidebar + JobDetail)
  so both new components share one source of truth instead of
  duplicating formatting/download logic.

### Job deletion (`DELETE /jobs/{id}`, `app/api/routes/jobs.py`)

Owner-or-admin only, reusing the exact `_check_ownership` helper the
GET/download endpoints already use (404 if missing, 403 if not
owner/admin). Deletes the input file and both output files from disk
(best-effort — logs a warning and continues on `OSError` rather than
failing the whole delete if a file is already gone), then deletes the
DB row. Returns 204. `JobStore` gained `delete_job(job_id) -> bool` and
`list_jobs()` gained an optional `owner_user_id` filter (used by the
user-deletion job-count check below; kept backward compatible —
existing no-arg and `status=`-only calls are unaffected).

### User deactivation (`PATCH /users/{id}`, `app/api/routes/users.py`)

Admin-only, body `{"is_active": bool}`. Deactivating sets `is_active =
false`; both `POST /auth/login` and `get_current_user` already checked
this field since Phase 7, so deactivation blocks future logins AND
immediately invalidates any live session on that user's very next
authenticated request (no token caching — `get_current_user` re-fetches
the user by id every call). Job history is untouched — this endpoint
never writes to the `jobs` table. `UserStore` gained
`update_user(user_id, **fields)`, mirroring `JobStore.update_job`'s
arbitrary-field-update pattern.

### User deletion (`DELETE /users/{id}`, `app/api/routes/users.py`)

Admin-only. Blocked with `409 user_owns_jobs` if
`job_store.list_jobs(owner_user_id=user_id)` returns anything — the
error message names the job count and says to deactivate instead.
Zero-job users delete cleanly (204). `UserStore` gained
`delete_user(user_id) -> bool`.

### Self-guard (both endpoints)

Both `PATCH /users/{id}` and `DELETE /users/{id}` reject
`user_id == <the authenticated admin's own id>` with `400
cannot_modify_self`, checked before any other lookup — prevents an
admin from locking themselves out. The frontend also proactively
disables both buttons on the admin's own row (with an explanatory
`title` tooltip) rather than only relying on the backend rejecting the
request after the fact.

### `job_count` on the user response (deviation from spec, minimal)

The spec asked for the delete button to be "disabled... when the user
still owns jobs" — doing that proactively (not just showing the 409
error after a failed click) requires knowing each user's job count
client-side. `GET /users` (and the `PATCH` response) now include a
`job_count: int` field, computed via `len(job_store.list_jobs
(owner_user_id=u.id))` per user. This is the only backend response-
shape change beyond the two new endpoints — not a pipeline, auth, or
queue-logic change, just an additional field needed for the frontend
requirement as stated.

### Confirmation pattern

Both job and user deletion use the same lightweight interaction:
clicking "Sil" swaps it for an inline "Silinsin? Bəli / Xeyr" (or
"Bəli, sil / Ləğv et" in the larger JobDetail panel) rather than a
browser `confirm()` dialog or a modal — no accidental double-click
deletes, no new component class needed, and it matches the existing
minimalist, no-modal design language already established in Phase 8.

### Validated

- Full flow: uploaded via direct API call (see note below), watched it
  appear at the top of the sidebar and progress
  Gözləyir → Emal olunur → Hazırdır live via existing polling with zero
  manual refresh; clicked to another sidebar entry and back — panel
  swapped instantly, no page reload; deleted it via the sidebar's
  inline confirm — entry vanished from the sidebar.
- Deleted the **currently open** job from the main panel's own delete
  button — main panel correctly returned to the upload/home state and
  the sidebar count dropped, confirming the "if the open job gets
  deleted, return home" requirement.
- Member (`dockernet@test.com`) logged in via the browser: sidebar
  correctly scoped to only their own job, no admin menu item present.
- Confirmed via the backend test suite: a member gets 403 deleting
  another user's job (`test_member_cannot_delete_other_job`); an admin
  can delete a member's job (`test_admin_can_delete_member_job`).
- Confirmed via the browser: deactivating a user immediately flips
  their row to "Deaktiv" / "Aktivləşdir"; confirmed via direct login
  call that the deactivated user gets the generic 401
  `invalid_credentials` (same message as wrong password — no
  information leakage that the account exists but is disabled);
  confirmed their job history (`GET /jobs` as admin) is untouched;
  reactivated and confirmed login succeeds again (200).
- Confirmed via the browser: a user who owns a job shows a disabled
  "Sil" button with the explanatory tooltip
  ("N yükləməyə sahibdir — əvvəlcə deaktiv edin"); created a fresh
  zero-job user, confirmed its "Sil" button was enabled, deleted it
  successfully via the inline confirm.
- Confirmed via the browser: the admin's own row shows both
  Deaktiv/Sil buttons disabled with self-guard tooltips ("Öz
  hesabınızı deaktiv edə/silə bilməzsiniz").
- Backend test suite: 127 tests (109 existing + 18 new — job deletion
  ownership/disk-cleanup, user deactivation/login-blocking/job-history-
  untouched/self-guard, user deletion/409-when-owns-jobs/self-guard),
  0 failures, 0 regressions.
- `ruff`/`black` clean on every file this phase created or
  substantively edited; pre-existing `Depends()`-pattern lint debt in
  route handlers (documented since Phase 9) left alone, unrelated to
  this phase.

### Known limitation in validation

The actual drag-and-drop/click-to-browse upload interaction (as
opposed to the resulting sidebar/panel behavior, which was fully
verified) could not be exercised end-to-end through the Browser-pane
tooling used for validation in this environment — there is no way to
attach a real OS file to a native `<input type="file">` from these
tools (same limitation noted in Phase 8's and Phase 9's validation
sections). The upload was instead triggered via a direct API call,
which exercises everything downstream of `onUploaded(jobId)` except
the literal callback invocation from `UploadZone` itself — that wiring
(`api.uploadFile` → `onUploaded(res.job_id)` →
`setSelectedJobId(jobId)`) is a single synchronous line, reviewed but
not click-tested.

### Visual quality caveat

Every interaction (panel swap, live polling, delete confirmations,
deactivate/reactivate, self-guards, empty states) was functionally
verified through the browser. Whether the redesigned two-pane layout
actually *looks and feels* right — spacing, sidebar width, the
understated selected-state, whether it reads as a natural evolution of
Phase 8's minimalist design rather than a bolted-on admin sidebar — is
a visual judgment call a human needs to make. I can't declare that on
my own authority.

## Phase 11: Design refresh — dark mode, typography, depth (`frontend/src/`)

### What it does

A pure visual/theming pass over the Phase 10 structure — no layout,
routing, or functional changes. Adds true dark/light theming (dark
default), a display serif typeface for hero moments, a single
consistently-scoped accent role, and depth via background layering and
soft shadows instead of the previously flat, single-light-theme,
Inter-everywhere look. Explicit reference point: Claude's and
ChatGPT's own interfaces, for *feel* (warmth, restraint, typographic
contrast, accent discipline) — not layout, iconography, or brand
assets, none of which were copied.

### Theme tokens (`frontend/src/index.css`)

Raw palette values live on plain CSS custom properties, swapped by a
`[data-theme]` attribute on `<html>` — not Tailwind's built-in `dark:`
variant, and not a bare `prefers-color-scheme` media query, since
neither can express "stored choice overrides system preference."
`@theme inline` (Tailwind v4) registers each token as a live
`var()` reference rather than resolving it to a static value at build
time, so `bg-canvas`, `text-ink`, etc. correctly repaint when the
attribute changes at runtime with no rebuild.

**Light** (`:root`, and `:root[data-theme="light"]`):

| token | value | role |
|---|---|---|
| `canvas` / `panel` | `#FFFFFF` | app background / elevated surfaces |
| `sidebar` | `#FAFAFA` | sidebar background (subtly distinct from canvas — the depth cue) |
| `surface` / `surface-hover` | `#F5F5F5` / `#ECECEC` | hover fills, chip backgrounds |
| `border` / `border-strong` | `#EAEAEA` / `#D9D9D9` | hairlines / emphasized borders |
| `ink` / `ink-secondary` | `#151515` / `#3A3A3A` | primary / secondary text |
| `muted` | `#6B6B6B` | tertiary text (bumped from Phase 8's `#888` — that was ~3.5:1 on white, failing WCAG AA; `#6B6B6B` is 5.33:1) |

**Dark** (`:root[data-theme="dark"]`) — warm dark grays anchored on the
brand's `#151515`, not pure black and not a neutral-gray inversion;
`panel`/`surface` carry a faint warm (slightly brown/olive) undertone
rather than blue-gray, and `ink` is a warm off-white (`#F3F0E9`), not
`#FFFFFF` — avoids the harsh white-on-black look a naive "invert
everything" dark mode produces:

| token | value |
|---|---|
| `canvas` | `#151515` (the brand dark, used directly — the "natural anchor point" the brief asked for) |
| `panel` | `#1C1B19` |
| `sidebar` | `#111110` (darker than canvas — sidebar reads as sitting behind the main panel) |
| `surface` / `surface-hover` | `#211F1D` / `#2A2825` |
| `border` / `border-strong` | `#2C2A27` / `#3D3A36` |
| `ink` / `ink-secondary` | `#F3F0E9` / `#B9B6AC` |
| `muted` | `#858177` (4.70:1 on canvas — passes AA) |

`success`/`error`/`processing` status colors are also theme-pairs
(saturated for light, slightly softened for dark — `#4ADE80`/`#F87171`/
`#60A5FA` — to avoid a neon-on-dark look). All contrast pairs checked
against WCAG AA (4.5:1); every text/background combination in both
themes clears it (dark-mode muted-on-canvas is the tightest at 4.70:1).

**Accent is theme-invariant**: `#FEE500` with `#151515` text-on-accent
(`accent-ink`), unchanged across both themes — one consistent brand
color rather than one that shifts with the theme.

### Accent usage rule — one recurring role, not one-off placements

Per the brief's explicit ask ("spend the accent consistently in ONE
clear recurring role"), accent yellow is used for exactly:

1. **Primary action buttons** — login submit, "Yarat" (create user).
   Solid `bg-accent` fill with `accent-ink` text. Secondary actions
   (download buttons, "Yeni yükləmə", cancel/dismiss) stay bordered/
   neutral, never accent-filled — this is what keeps the accent
   meaningful instead of diluted.
2. **The sidebar's selected-item indicator** — a 3px rounded left bar
   (`Sidebar.tsx`), the one concrete idea named in the brief. Paired
   with a quiet `bg-surface` tint on the row itself; the bar is what
   actually reads as "selected," the tint is secondary reinforcement.
3. **The brand mark** — the `vectorize-shape` logo animation (unchanged
   from Phase 8) and the admin-role badge in user management (a small
   accent-tinted pill — a "status accent," per the brief's third
   example).

Explicitly NOT accent: job status colors (success/error/processing —
those signal job state, a different axis of meaning than "primary/
mine," and mixing them would blur both), general icon hover colors,
focus rings beyond the existing subtle `accent/30` ring already present
since Phase 8.

### Typography — Fraunces for hero moments, Inter for everything else

**Fraunces** (Google Fonts, variable, optical-size axis `9..144`,
weights 400/500/600) is the display/heading typeface, used only at
true hero moments:
- The home-state greeting (`HomeGreeting.tsx`, new) — "Bugün nəyi
  vektorlaşdıraq?" ("What shall we vectorize today?" — adapted from
  ChatGPT's "What can I help with?" to this product's actual function,
  not a generic AI-assistant phrase grafted onto a file-conversion
  tool), at `text-3xl`/`text-4xl`.
- The login screen's "Vectorizer" wordmark, `text-4xl`.

Everywhere else — sidebar entries, job filenames, buttons, labels,
the persistent header wordmark, form fields, status badges — stays
Inter, unchanged from Phase 8. This is a deliberate two-tier hierarchy
(display serif for emotional/brand moments, clean sans for working UI),
not a wholesale font swap; using Fraunces for e.g. the header logo or a
job's filename would have been noise, not hierarchy.

**Glyph coverage was verified before committing to this choice**:
Azerbaijani's schwa characters (ə U+0259, Ə U+018F) fall inside Latin
Extended-B, and Fraunces's served `latin-ext` `@font-face` block
(`unicode-range: U+0100-02BA, ...`) covers that range — confirmed by
directly inspecting the served CSS from `fonts.googleapis.com`, not
just trusting a language-support list. A font that silently fell back
to the browser default for `ə`/`Ə` in an Azerbaijani-language product
would have been a real, easy-to-miss quality bug.

### Home-state greeting (`components/HomeGreeting.tsx`, new)

Sits above `UploadZone` in `DashboardPage`'s home state (no job
selected) — previously the upload dropzone sat alone on empty space.
Fraunces headline + a short Inter subtitle explaining what the tool
does, replacing dead space with a genuine "hero" moment per the brief.

### Theme detection & persistence (`context/ThemeContext.tsx`, new)

- **Resolution order**: `localStorage['vectorizer-theme']` if present
  (`'dark'` or `'light'`) → else `window.matchMedia('(prefers-color-
  scheme: light)').matches` → light if explicitly preferred → **dark**
  otherwise (covers both "prefers dark" and "no preference expressed,"
  satisfying "dark mode as the default" while still respecting an
  explicit light-preferring system, per the brief's two — slightly
  overlapping — requirements).
- **No flash of wrong theme**: the exact same resolution logic runs
  synchronously in an inline `<script>` in `index.html`'s `<head>`,
  setting `data-theme` on `<html>` before first paint (i.e. before
  React mounts or any CSS-driven color is committed to the screen).
  `ThemeProvider`'s `getInitialTheme()` re-derives the identical value
  on mount so React state and the DOM attribute start in sync.
- **Live system-preference following**: only while no explicit choice
  is stored — a `matchMedia('(prefers-color-scheme: light)')` change
  listener updates the theme live, but only runs its effect when
  `localStorage` has no stored value; once the user toggles, that
  choice is sticky and the listener is skipped entirely.
- **Toggle** (`components/ThemeToggle.tsx`, new): a sun/moon icon
  button, present directly in the header on every screen (dashboard,
  users) and fixed top-right on the login screen — not buried in a
  dropdown, matching how the brief's reference apps expose it as an
  always-visible control rather than a settings-menu item.

### Depth system

- **Elevation via background layering, not heavy shadows**: sidebar
  (`bg-sidebar`) vs. main panel (`bg-canvas`) are different shades in
  both themes — that contrast alone reads as depth without needing a
  shadow on the boundary. Dropdown menus and the create-user form panel
  additionally sit on `bg-panel` (lighter than canvas in dark mode) with
  a `shadow-md`/`shadow-sm` pair (soft, low-opacity — barely visible in
  light mode, a real but restrained lift in dark mode where elevation
  needs more contrast to read).
- **Rounded-corner scale**, applied consistently rather than ad hoc:
  `rounded-lg` (buttons, inputs, small chips), `rounded-xl` (panels,
  dropdown menus, the upload dropzone), `rounded-full` (avatars, status
  badges, the sidebar accent bar). This was already roughly Phase 8's
  pattern; Phase 11 just applies it uniformly to every new/re-themed
  element instead of per-component ad hoc choices.
- **Avatar circles** (header, both Dashboard and Users pages): kept
  the existing initials-in-circle from Phase 8/10, refined with
  `border-strong` (vs. the previous plain `border`) for a touch more
  definition against the now-varied background tones.

### Sidebar polish

Beyond the accent selection bar: entry padding increased (`py-3` →
`py-3.5`) for more comfortable spacing per the brief; hover state now
uses a dedicated `surface-hover` token (previously reused `surface` for
both the selected AND hover states, which made them hard to
distinguish); the delete-confirm overlay uses `bg-sidebar/95` instead
of a hardcoded `bg-white/95` so it's correct in dark mode too (this was
the one spot in Phase 10's code with a literal color instead of a
token — everything else was already token-based, which is exactly why
this phase could be a pure CSS-variable swap rather than a rewrite of
component logic).

### Validated

- **Dark mode confirmed as the true default**: fresh browser session
  (no stored preference), `document.documentElement.getAttribute
  ('data-theme')` → `"dark"`, computed `body` background `rgb(21, 21,
  21)` (`#151515`) — verified via direct DOM/computed-style inspection,
  not just visual inspection.
- **Fraunces genuinely loaded and applied**, not silently falling back:
  `document.fonts` shows 9 loaded Fraunces faces; the home-greeting and
  login-wordmark `<h1>`s both compute `font-family: Fraunces, Georgia,
  serif` with the Fraunces face actually rendering (confirmed via
  `document.fonts`, and visually via Azerbaijani schwa characters
  rendering correctly in the greeting text, not as tofu/fallback
  glyphs).
- **Toggle + persistence**: toggled light → dark → light across the
  login screen, dashboard, and users page; each toggle immediately
  updated `data-theme`, computed colors, and `localStorage
  ['vectorizer-theme']`; reloaded the page after setting light mode —
  `data-theme` was still `"light"` on the very first paint (the inline
  pre-paint script), no flash of dark-then-light.
- **Accent role verified programmatically**: the sidebar's selected-
  item bar computed to `3px` wide, `rgb(254, 229, 0)` (`#FEE500`)
  exactly, `border-radius` fully rounded — confirmed via
  `getComputedStyle`, not just visual read.
- **Contrast checked numerically** (WCAG relative-luminance formula,
  computed in-browser against the actual rendered RGB values): muted
  text on canvas 4.70:1 (dark) / 5.33:1 (light), secondary text 9.00:1
  (dark) / 11.37:1 (light), accent-ink on accent 14.28:1 — every pair
  clears the 4.5:1 AA threshold for normal text.
- **Full Phase 10 functional regression re-run against the new theme**:
  created a user (dark mode, via the now-accent-filled "Yarat" button),
  deactivated them and confirmed login blocked (401
  `invalid_credentials` via direct API check), deleted them (zero-job
  case, inline confirm), selected a sidebar job (accent bar appeared,
  panel swapped to `JobDetail`), toggled themes mid-session with the
  job still selected (selection survived the toggle), deleted that job
  from the sidebar's own delete-with-confirm (job removed, panel
  returned to the home/greeting state) — every Phase 10 interaction
  works identically under the new theme system.
- Zero console errors throughout every step above.
- TypeScript (`tsc -b`) and production build (`vite build`) both clean.
- Backend test suite: 127 tests, 0 failures — confirmed unaffected, as
  expected for a frontend-only phase (`git status` shows zero backend
  files touched).

### Deviations from the spec

None identified — dark-as-default with a persisted toggle, system-
preference-respecting initial detection, the single accent role, a
real display typeface with verified Azerbaijani glyph coverage, the
home greeting, and depth via layering/shadows were all implemented as
specified, without touching Phase 10's structural layout or any
backend code.

### Visual quality caveat

Every theme-switching, contrast, and functional-regression claim above
was verified programmatically or through direct browser interaction —
not just eyeballed. Whether the result actually *reads* as closer to
the Claude/ChatGPT reference — whether the warmth, restraint, and
typographic contrast land the way the brief intends, versus merely
being "correct but still not quite premium" — is a subjective design
judgment a human needs to make by actually looking at it. I've explained
the reasoning behind each choice above so that judgment has something
concrete to evaluate against, but I'm not declaring the visual result
"done" on my own authority.

## Phase 12: sidebar history limit + view-all/search, refresh-token session fix

### What it does

Two independent fixes from user testing: (1) the sidebar now shows only
the 12 most recent jobs with a "Hamısına bax" (view all) entry point to
a full paginated, backend-searched history page; (2) a page reload no
longer logs the user out, via an httpOnly refresh-token cookie — the
access token still lives only in frontend memory (Phase 8's decision is
unchanged), the cookie is what survives the reload. Admin job-visibility
scoping was explicitly out of scope and is untouched.

### Backend — job list pagination + search (`app/api/routes/jobs.py`,
### `app/core/queue/store.py`)

`GET /jobs` gained `limit` (default 20, capped at 100), `offset`
(default 0), and `search` query params, and its response shape changed
from a bare array to `{items: [...], total: N}` — `total` reflects the
filtered-but-unpaginated count, for frontend page-count math. This is a
breaking response-shape change; the only consumer (the frontend) was
updated in the same phase.

`JobStore.list_jobs()` gained `search`/`limit`/`offset` params (a
case-insensitive `input_filename ILIKE %search%`, plain — no
`pg_trgm`/similarity index added, per the phase brief's own "don't
over-engineer" steer at this data volume) and a new `count_jobs()`
sibling for the `total`; both share a `_filter_clause()` helper so the
WHERE-building logic isn't duplicated between them. Existing
no-search/no-pagination call sites (`users.py`'s `job_count`
computation, the worker, etc.) are unaffected — all new params default
to `None`/unfiltered.

Ownership scoping is unchanged in effect, only relocated: the route
used to fetch *all* jobs and filter to the caller's own in Python; it
now passes `owner_user_id` straight into the SQL `WHERE` clause (`None`
for admins → no filter, the caller's id for members) — same visible
behavior, now enforced in the same query as the pagination/search
filters instead of after the fact.

### Backend — refresh-token cookie flow (`app/core/auth/`,
### `app/api/routes/auth.py`, `migrations/versions/0002_refresh_tokens.py`)

- **New `refresh_tokens` table**: `id`, `user_id` (FK → `users`, `ON
  DELETE CASCADE`), `token_hash` (SHA-256 hex, unique — the raw token is
  never persisted, only set as the cookie value), `issued_at`,
  `expires_at`, `revoked`. `RefreshTokenStore` (`app/core/auth/store.py`)
  follows the same psycopg/hand-written-SQL pattern as `JobStore`/
  `UserStore`: `create`, `get_valid_by_hash` (excludes revoked/expired
  rows so callers never have to re-check), `revoke_by_hash`.
- **Access-token lifetime shortened 24h → 30 min**
  (`security.ACCESS_TOKEN_LIFETIME`) — a deviation from Phase 7's
  original 24h flat lifetime, but the one this phase's own brief asked
  for explicitly ("keep the access-token lifetime as short as it
  currently is... but keep it short, e.g. 15–30 min") now that the
  refresh cookie is what carries the session across a reload, not the
  access token. Refresh tokens live 14 days
  (`security.REFRESH_TOKEN_LIFETIME`) and are **rotated on every use** —
  `POST /auth/refresh` revokes the token it just validated and issues a
  brand new one, so a stolen-then-replayed cookie only works once before
  the legitimate rotation invalidates it.
- **JWT payload gained an `email` claim** (`create_access_token(user_id,
  role, email)`) — needed so the frontend can render the logged-in
  user's email after a silent refresh/bootstrap, where (unlike the
  original login flow) there's no form input to read it from.
- **`POST /auth/login`**: unchanged response body shape
  (`{access_token, token_type}`), but now also sets the refresh cookie
  (`httponly=True`, `samesite="lax"`, `secure=settings.cookie_secure`,
  `path="/"`, `max_age=14 days`). The raw refresh token is never in the
  JSON body.
- **`POST /auth/refresh`** (new): reads the cookie, hashes it, looks it
  up, checks the owning user is still active (deactivation revokes live
  sessions immediately, matching the existing Phase 10 access-token
  behavior), rotates, returns a new access token + sets the new cookie.
  401 (with the cookie cleared) on missing/invalid/expired/revoked/
  tampered token or a deactivated owner.
- **`POST /auth/logout`** (new): revokes the refresh token server-side
  and clears the cookie. Deliberately does **not** require a valid
  Bearer access token — logout has to work even when the (now
  short-lived) access token has already expired, which is common by the
  time a user manually logs out.
- **`Settings.cookie_secure`** (`app/config.py`): `True` only in
  `environment=prod`. In dev the cookie omits `Secure` so it still gets
  stored when the stack is exercised over plain HTTP on a LAN IP (not
  just `localhost`, which most browsers already treat as a secure
  context and would work with `Secure` regardless).
- **Cookie `path="/"`**, not scoped to `/auth` — the frontend calls
  through the Vite dev proxy at `/api/*`, which strips the `/api`
  prefix before the backend ever sees the request, so a backend-side
  `Path=/auth` wouldn't reliably match the browser-side request path
  it needs to attach to. `path="/"` sidesteps that coupling; the cookie
  being `httponly` means JS still can't read it regardless of scope.

### Frontend — refresh flow wiring (`api/client.ts`,
### `context/AuthContext.tsx`)

- **`bootstrapAuth()`** (new, exported from `client.ts`): called once by
  `AuthProvider` on mount. Silently `POST /auth/refresh`s; on success
  decodes the returned access token into the in-memory `AuthUser` state,
  on failure leaves the user logged out. `AuthContext` gained an
  `initializing` flag so `App.tsx`'s router can show a blank loading
  screen for that one round-trip instead of flashing the login page
  before the refresh check resolves.
- **Automatic 401-retry-once interceptor** inside `request()`: any
  authenticated call that 401s (access token expired mid-session)
  triggers one `POST /auth/refresh` attempt, and only if that succeeds
  does the original request get retried once with the new token; if
  refresh also fails, `onUnauthorized` fires (clears state → login
  screen), same as before this phase for the terminal case. Concurrent
  401s are coalesced onto a single in-flight refresh call
  (`_refreshPromise`) rather than firing one refresh per stalled
  request. `/auth/*` endpoints themselves are excluded from the
  retry-on-401 path to avoid infinite recursion.
- **`credentials: 'include'`** added to every `fetch` call in
  `client.ts` (explicit, even though same-origin requests through the
  Vite proxy already default to sending cookies) — matches the phase
  brief's explicit ask to double-check this, and makes the dependency
  on the cookie visible in the code rather than relying on a fetch
  default that's easy to misremember.
- **`logout()`** now also calls `POST /auth/logout` (best-effort, local
  state is cleared regardless of whether the network call succeeds) so
  the server-side revoke actually happens instead of just the token
  being dropped client-side.
- Access token itself is still **never** written to `localStorage`,
  `sessionStorage`, or a JS-readable cookie — confirmed via dev tools
  Application tab and `document.cookie` (empty — the refresh cookie is
  httpOnly and invisible to JS) during manual validation below.

### Frontend — sidebar limit + history page (`components/Sidebar.tsx`,
### `pages/JobHistoryPage.tsx`, `pages/DashboardPage.tsx`, `App.tsx`)

- **Sidebar** now fetches `GET /jobs?limit=12&offset=0` (was: fetch
  everything). A "Hamısına bax (N)" entry appears at the bottom only
  when `total > jobs.length` — i.e. it's absent for small job counts
  rather than always shown, since an always-visible link to an
  identical list would be noise.
- **`JobHistoryPage`** (new): full history behind the `#history` hash.
  Debounced (400 ms) search input wired to the backend `search` param
  — confirmed via the network tab that a single request fires per
  pause in typing, not one per keystroke. Page-number pagination
  (Əvvəlki/Növbəti, 20 per page) rather than infinite scroll — chosen
  because the app had no existing infinite-scroll pattern to be
  consistent with, and explicit page controls are simpler to reason
  about for a moderate job-history size. Clicking a row navigates to
  `#job/<id>` rather than managing shared selection state across pages.
- **`DashboardPage`** now accepts an optional `initialJobId` prop
  (parsed from `#job/<id>` by the router) and — this was a required
  refactor, not just plumbing — fetches the **selected** job via
  `GET /jobs/{id}` independently of the (now 12-item-capped) sidebar
  list, polling it every 4s while open. Previously `selectedJob` was
  derived via `jobs.find(...)` against the sidebar's own fetched array,
  which broke the moment the sidebar stopped holding every job: a job
  opened from the history page (page 2, or matched by search) is very
  often not one of the 12 most recent, so it would have silently failed
  to open. The `#job/<id>` hash is consumed once (`history.replaceState`
  clears it) so it doesn't stay sticky across ordinary sidebar clicks.
- **`App.tsx`** router gained the `#history` and `#job/<id>` hash
  branches alongside the existing `#users` one, plus the `initializing`
  loading-screen gate described above.

### Validated

- **Backend**: full suite 142 tests (127 existing + 15 new — pagination
  boundaries/limit-cap/search-match/no-match/search+pagination combined/
  ownership-under-search for `GET /jobs`, and login-sets-cookie/
  refresh-succeeds/refresh-without-cookie-401/tampered-cookie-401/
  rotation-invalidates-old-token/expired-token-401/deactivated-user-401/
  logout-clears-and-revokes for the refresh flow), 0 failures, 0
  regressions. `ruff`/`black`/`mypy` clean on every file this phase
  created or substantively edited (pre-existing `Depends()`-pattern
  lint debt, documented since Phase 9, left alone).
- **Frontend**: `tsc -b` and `vite build` both clean.
- **Manual, through the actual Docker-composed stack** (rebuilt
  `backend`+`frontend` images, migration `0001→0002` applied
  automatically on `backend` startup, confirmed via `docker compose
  logs`): logged in, confirmed `document.cookie` is empty and
  `localStorage`/`sessionStorage` are empty (the refresh cookie is
  httpOnly and invisible to JS, as intended) while the network tab
  shows `refresh_token` actually being set; **hard-reloaded the page —
  session persisted, dashboard rendered directly with no login-screen
  flash**, `POST /auth/refresh` returned 200 on load; created 14 extra
  jobs via direct API calls, confirmed the sidebar capped at 12 with
  "Hamısına bax (15)" showing the true total; opened the history page,
  confirmed all 15 listed; searched for "informatika" and confirmed
  exactly one debounced network request narrowed the list to the one
  match; clicked that result and confirmed it opened in the dashboard's
  detail panel (job-detail-on-click preserved) with the `#job/` hash
  cleared afterward; logged out and confirmed `POST /auth/logout`
  fired, the login screen returned, and a subsequent `POST
  /auth/refresh` correctly 401'd (cookie revoked, not just cleared
  client-side); cleaned up the test jobs afterward via the API.
- **Not separately re-verified this phase** (unchanged, and already
  covered by the untouched ownership-scoping code path plus the new
  `test_ownership_scoping_respected_under_search` test): admin-sees-all
  vs. member-sees-own job visibility. No admin-scoping code was touched.

### Deviations from the spec

- **Access-token lifetime**: changed from Phase 7's 24h to 30 min. This
  is what the phase brief itself asked for ("keep the access-token
  lifetime as short as it currently is... e.g. 15–30 min"), called out
  here because it's a change to a value set in an earlier phase, not
  because it's a deviation from *this* phase's instructions.
- **Refresh-token rotation, not delete-on-use**: implemented as
  revoke-and-reissue (row kept with `revoked=TRUE`) rather than deleting
  the row outright — the brief listed this as the recommended option
  ("recommended: rotate... to limit replay risk"), taken as the
  concrete choice between the two it explicitly left open.
- **History page uses page-number pagination**, not infinite scroll —
  the brief left this as an explicit "pick whichever is more consistent
  with existing UI patterns" choice; the app had no existing scroll-
  pagination pattern to match, so explicit Əvvəlki/Növbəti controls were
  chosen for predictability.
- **Sidebar "view all" entry is conditional** (`total > limit`), not
  always shown — also an explicitly open choice in the brief; shown
  only when there's more to see, since an always-present link to an
  identical list adds visual noise for the common case of few jobs.
- No other deviations identified — admin job-visibility scoping was
  left completely untouched, the access token is still frontend-
  memory-only, and backend search is the sole filtering mechanism (no
  client-side filtering was added anywhere).

## Phase 13: sidebar/dashboard visual refresh (v4)

### What it does

A pure CSS/JSX presentation pass over the Phase 10–12 sidebar and
dashboard main panel — no backend changes, no data-flow/API/auth/
routing changes. Four fixes from a design-tool-approved direction: a
styled sidebar scrollbar, date-grouped sidebar sections, a vertically
centered home/empty state, and a visually quieter "Yeni yükləmə"
button. Every color used is an existing Phase 11 token or the brand
accent at a different opacity — no new hues introduced.

### Custom sidebar scrollbar (`index.css`)

`.sidebar-scroll` (applied to the sidebar's scrollable job-list `div`
in `Sidebar.tsx`): `scrollbar-width: thin` + `scrollbar-color:
rgba(254,229,0,0.35) transparent` for Firefox, matching
`::-webkit-scrollbar{width:6px}` / `::-webkit-scrollbar-track{background:
transparent}` / `::-webkit-scrollbar-thumb{background-color:
rgba(254,229,0,0.35); border-radius:999px}` /
`::-webkit-scrollbar-thumb:hover{background-color: rgba(254,229,0,0.75)}`
for Chrome/Edge/Safari. Accent yellow at 35% opacity at rest, 75% on
hover — visible but not a "solid loud yellow bar." The track is fully
transparent rather than a near-match color, so it reads as no track at
all against `--sidebar`. No JS logic added for "only show when
overflowing" — that's native `overflow-y: auto` behavior, already in
place from Phase 10, and was the reason no extra code was needed here.

### Date-grouped sidebar sections (`lib/jobUtils.ts`, `components/Sidebar.tsx`)

New `groupJobsByRecency()` in `jobUtils.ts` — a pure display transform
over the already-fetched, already-limited (Phase 12's 12-item sidebar
page) job array; it does not re-fetch or change what's fetched. Bucket
boundaries: **Bu gün** (same calendar day as now, in the browser's
local time zone), **Bu həftə** (the 6 calendar days before today — a
7-day window together with "Bu gün"), **Daha əvvəl** (everything
older). Empty buckets are omitted from the returned array entirely, so
`Sidebar.tsx` never renders an empty header — it just maps the group
list. Headers render as `text-[11px] font-medium text-muted uppercase
tracking-wider` — Inter (default), not Fraunces, matching the existing
functional-label pattern already used for e.g. `UsersPage`'s
"İstifadəçilər" section header. Group labels are stored in natural
case in code (`'Bu gün'`) and rendered uppercase via the `uppercase`
utility class, not as hardcoded caps strings — consistent with how the
rest of the app handles this pattern.

### Vertically centered home/empty state (`pages/DashboardPage.tsx`)

Scoped strictly to the home state (`selectedJob === null`) — the
job-detail view keeps its original `max-w-2xl mx-auto px-6 py-12`
top-anchored wrapper untouched, per the brief's explicit "don't force-
center job detail" instruction. The home state's wrapper is now `<div
className="min-h-full flex items-center justify-center px-6 py-12">`
around the existing `max-w-2xl w-full` content block.  `min-h-full`
(not `h-full`) was the deliberate choice: it guarantees at least the
full available height for centering math while still letting the
container grow taller than the viewport and scroll (inherited
`overflow-y-auto` on `<main>`) rather than clip, if a future viewport
were ever small enough that the content didn't fit — verified this
doesn't actually trigger at realistic sizes (see Validated below).

### Toned-down "Yeni yükləmə" button (`components/Sidebar.tsx`)

**Deviation, noted explicitly**: the brief's premise was that this
button "may be a solid, high-contrast `#FEE500` filled button" — it
was not; Phase 11's own accent-usage rule already scoped `bg-accent`
fills to exactly two roles (primary submit actions, the sidebar
selected-indicator/brand mark), and "Yeni yükləmə" was already a
bordered/neutral secondary button (`text-ink-secondary`, `border-
border`, no fill) going into this phase. Rather than skip the
requirement on that basis, the button was quieted one step further
from its already-secondary baseline: default text color changed from
`text-ink-secondary` to `text-muted` (brightening to `text-ink-
secondary` only on hover), and the plus icon tinted `text-accent/70`
instead of inheriting the surrounding text color — a small nod to the
brand accent without a solid fill, and consistent with the low-opacity-
accent language introduced by the scrollbar thumb in this same phase.
Contrast re-verified (see Validated) rather than assumed.

### Validated

- **Backend**: full suite re-run unchanged as instructed (no backend
  file touched this phase) — 142 tests, 0 failures.
- **Frontend**: `tsc -b` and `vite build` both clean.
- **Grouping, with real varied-recency data**: created 3 jobs via
  direct API calls, then backdated two of them directly in Postgres (3
  days ago, 20 days ago) to force non-trivial bucket membership rather
  than relying on same-day coincidence. Read `aside.innerText` in the
  browser and confirmed the exact expected structure: **Bu gün** →
  today's 2 jobs (the freshly created one + the pre-existing
  Phase-9-era sample job), **Bu həftə** → the 3-days-ago job, **Daha
  əvvəl** → the 20-days-ago job — headers present only where a bucket
  was non-empty.
- **Scrollbar**: `getComputedStyle` on `.sidebar-scroll` confirmed
  `scrollbar-width: thin` and `scrollbar-color: rgba(254,229,0,0.35)
  rgba(0,0,0,0)` applied. With only a handful of jobs the list's
  `scrollHeight` did not exceed `clientHeight` (no scrollbar, as
  intended); after uploading enough extra jobs to force overflow,
  `scrollHeight > clientHeight` confirmed true, so the styled scrollbar
  becomes the one shown. Checked in Chrome (the tool's embedded
  browser is Chromium-based); Firefox was not separately available in
  this environment to check `scrollbar-width`/`scrollbar-color`
  rendering directly, so that engine's actual paint is unverified
  (**flagged explicitly, not silently skipped** — the CSS properties
  used are the documented cross-browser API and Firefox does support
  both, but the visual result wasn't eyeballed there).
- **Vertical centering, at multiple heights**: measured the home
  state's content-box gap above vs. below programmatically (not just
  eyeballed) at three viewport heights — 720px (172.0px / 172.0px),
  1000px (312.0px / 312.0px), and a tighter 640px (132.0px / 132.0px)
  — symmetric in all three, and `scrollHeight > clientHeight` was false
  at every size tested (no forced/unnecessary scroll). Confirmed the
  job-detail view still renders through its original unwrapped
  `max-w-2xl mx-auto px-6 py-12` container, unaffected by this change.
- **Button de-emphasis + contrast**: computed-style contrast checked
  against the actual rendered `--sidebar` background in both themes
  (WCAG relative-luminance formula, same method as Phase 11): light
  theme muted-on-sidebar 5.11:1, dark theme 4.86:1 — both clear the
  4.5:1 AA threshold for normal text. One verification wrinkle worth
  recording: a first pass of this check, taken immediately after
  toggling the theme, read back the *pre-toggle* color — traced via
  `element.getAnimations()` to CSS transitions stuck at `localTime: 0`
  because this tool's browser tab isn't actually compositing frames
  (the same limitation the screenshot tool surfaces explicitly); a
  freshly-created off-DOM element with the identical class resolved
  correctly immediately, confirming the CSS itself was correct and the
  stuck reading was purely a non-visible-tab rendering artifact, not an
  app bug — forcing the queued transitions to finish (`.finish()`)
  produced the correct, now-reported value.
- **Zero regressions**: re-confirmed through the browser after all
  changes — job selection (click sidebar entry → detail panel opens),
  delete-with-confirmation (sidebar inline confirm), live polling
  (interval requests continued firing), "Hamısına bax"/view-all +
  search (Phase 12's history page unaffected, still opens and filters
  correctly), and the refresh-token session persisting across a hard
  reload (Phase 12) all still work exactly as before. All test jobs
  created for this phase's manual verification were deleted via the
  API afterward.

### Deviations from the spec

- **"Yeni yükləmə" button's starting point**: as detailed above, it was
  already a bordered/neutral secondary button, not the accent-filled
  button the brief described — quieted one further step rather than
  left unchanged, but the premise itself didn't match the Phase 11
  code.
- **Bucket boundary exact values**: "Bu həftə" was implemented as the 6
  calendar days before today (so, combined with "Bu gün," a rolling
  7-day window) — the brief left the exact boundary as the
  implementer's call; "Daha əvvəl" was used as the third bucket label
  for everything older, one of the brief's own suggested names.
- **Scrollbar opacity values**: 35% at rest / 75% on hover, within the
  brief's suggested "~30–40%, full opacity on hover" range but not
  literally 100% on hover — 75% was chosen to stay legible against
  `--sidebar` without reading as a "solid loud yellow bar" even in the
  hover state, per the brief's own stated goal for this element.
- **Firefox scrollbar rendering not visually verified** in this
  environment (no Firefox instance available to the tooling used) —
  called out above, not silently assumed correct.

## Phase 14: mobile responsiveness + hideable sidebar, scrollbar removed

### What it does

Direct follow-up user feedback on Phase 13: remove the sidebar
scrollbar entirely (rather than the styled one from Phase 13), make
the sidebar hideable on demand (not just narrower), and make the whole
frontend mobile-responsive. No backend or data-flow changes — visual/
layout only, and per explicit instruction this phase's manual
verification skipped re-testing already-covered functional flows
(upload, polling, auth) and focused on the new responsive/visibility
behavior itself.

### Scrollbar: styled → fully hidden (`index.css`)

Phase 13's `.sidebar-scroll` (thin, accent-tinted thumb) is replaced by
`.no-scrollbar`: `scrollbar-width: none` + `-ms-overflow-style: none`
for Firefox/legacy Edge, `::-webkit-scrollbar { display: none }` for
Chromium/Safari. The list stays scrollable via wheel/touch/keyboard —
only the visible scrollbar chrome is gone. Applied to the same sidebar
job-list `div` in `Sidebar.tsx` that carried the old class.

### Hideable sidebar, both desktop and mobile (`components/Sidebar.tsx`,
### `pages/DashboardPage.tsx`)

A single `sidebarOpen` boolean (owned by `DashboardPage`, toggled by a
new hamburger-icon button in the header, left of the logo) drives two
different visual behaviors depending on viewport, both via one Tailwind
class string on the `<aside>` — no separate mobile/desktop components:

- **Desktop (`md:` / ≥768px)**: the sidebar is a normal in-flow flex
  child; toggling animates its `width` between `18rem` (288px, `w-72`)
  and `0` (`md:w-0 md:border-r-0`, `overflow-hidden` clips the content
  during the collapse) — the main panel reclaims the freed width. This
  is the literal "if someone doesn't want to see it, they should be
  able to hide it" ask, and applies on desktop too, not only as a
  mobile accommodation.
- **Mobile (&lt;768px)**: the sidebar becomes a `fixed` overlay drawer
  (`inset-y-0 left-0 z-40`), toggling a `transform: translateX(...)`
  between `-translate-x-full` (off-screen) and `translate-x-0`, over a
  semi-transparent backdrop (`bg-ink/50`, `md:hidden`, click-to-close).
  A dedicated X-close button also appears inside the sidebar's top row
  (`md:hidden`) as a second, more discoverable way to close it beyond
  tapping the backdrop. `max-w-[85vw]` caps the drawer width so it
  never edge-to-edge-clips on very narrow phones.
- **Auto-close on mobile after picking an action**: selecting a job,
  clicking "Yeni yükləmə," or "Hamısına bax" all call a
  `closeIfMobile()` helper (checks `window.innerWidth < 768` at click
  time) so the result is immediately visible instead of staying hidden
  behind the drawer. Desktop behavior is unaffected — the sidebar stays
  open there after these same actions, matching the pre-existing
  always-visible feel.
- **Initial state**: `sidebarOpen` initializes to `window.innerWidth >=
  768` — open by default on desktop (zero behavior change from
  Phase 8–13 for existing desktop users), closed by default on mobile
  (an overlay drawer covering the whole screen on first load would be
  the opposite of a good empty-state experience). No resize-listener
  was added to re-sync this after mount — deliberately out of scope,
  since the manual toggle already covers the "I resized my window and
  want the sidebar back" case without added complexity.

### Mobile layout pass

- **Header hamburger button** (`DashboardPage.tsx`): plain 3-line icon,
  `aria-label`/`title` reflecting current state ("Sidebar-ı
  gizlət"/"Sidebar-ı göstər"), placed before the logo. Present at all
  viewport sizes (it's the single control for the hide/show feature,
  not a mobile-only affordance).
- **Main-panel padding**: both the job-detail wrapper and the
  home/empty-state wrapper in `DashboardPage.tsx` changed from a flat
  `px-6 py-12` to `px-4 sm:px-6 py-8 sm:py-12`, tightening the margins
  on narrow phones without changing anything at `sm:` and above.
- **`UsersPage.tsx`**: `UserRow`'s flex row gained `flex-wrap` (was a
  strict single-line `justify-between`) so the email/role-badge/
  action-buttons cluster wraps instead of overflowing horizontally on
  very narrow phones, with no visible change at wider sizes where it
  already fit on one line.
- **`JobHistoryPage.tsx` / `UsersPage.tsx` headers**: already had
  `hidden sm:inline` on the user's email and `px-4 sm:px-6` container
  padding from earlier phases — reviewed and left as-is, already
  mobile-safe.
- **Viewport meta tag**: already present in `index.html` from Phase 8
  (`width=device-width, initial-scale=1.0`) — confirmed, not re-added.

### Validated

- **Frontend**: `tsc -b` and `vite build` both clean. Backend untouched
  this phase — full backend suite intentionally **not** re-run, per
  this phase's explicit instruction that functional/workflow
  verification wasn't needed for a responsiveness-only frontend change
  (Phase 13's full regression pass already covers the underlying
  behavior, which nothing here alters).
- **Desktop hide/show**: toggled the header hamburger at 1280×800 —
  `<aside>` confirmed collapsing to `width: 0` (`getBoundingClientRect`)
  with the toggle button's label switching "Sidebar-ı gizlət" ↔
  "Sidebar-ı göstər" each click; re-opened, confirmed back to 288px.
- **Mobile drawer**: at 375×812, confirmed the sidebar starts off-
  screen (`left: -288`) with **no backdrop element in the DOM** and
  **no reserved layout space** (main content isn't squished — this is
  the `fixed`-vs-`static` positioning switch at the `md:` breakpoint
  doing its job, not just a visual illusion). Opened it via the header
  button — confirmed `left: 0`, backdrop present; clicked the backdrop
  — confirmed it closed again (`left: -288`, backdrop removed).
  Re-opened and clicked "Yeni yükləmə" — confirmed the drawer
  auto-closed (`closeIfMobile()` firing correctly).
- **Scrollbar**: `getComputedStyle` on the sidebar's scrollable list
  confirmed `scrollbar-width: none`; the equivalent
  `::-webkit-scrollbar{display:none}` rule is present in the same
  `.no-scrollbar` class (Chromium-verified indirectly through the
  computed-style check above, since `::-webkit-scrollbar` itself isn't
  independently queryable via `getComputedStyle`).
- **No horizontal overflow at 375px**: checked via
  `document.documentElement.scrollWidth` vs. `clientWidth` (equal, no
  overflow) on both the dashboard and the `#history` page.
- **One tooling caveat, not an app bug** (same root cause as Phase
  13's investigation): this environment's browser tab doesn't actually
  composite frames, so CSS transitions queued by a class-toggle sit
  stuck at their start value until explicitly finished
  (`element.getAnimations().forEach(a => a.finish())`) before the
  *computed layout* (not just computed color, this time) reflects the
  end state. Every geometry check above was taken only after forcing
  queued animations to finish, so the reported numbers reflect the
  actual end state a real (composited, visible) browser tab would
  reach within the 200ms transition — not a stuck intermediate value.

### Deviations from the spec

- **No resize-event re-sync** for `sidebarOpen` after mount (see above)
  — an explicit, minor scope call, not an oversight: the manual toggle
  already resolves the one real gap this would close (crossing the
  breakpoint mid-session), and adding a listener for a rare case felt
  like unrequested complexity for a phase whose brief was about adding
  a manual show/hide control, not automating it.
- **Scrollbar fully removed rather than restyled** — this phase's own
  instruction ("remove the scrollbar from sidebar") superseded Phase
  13's "styled scrollbar" deliverable; noted here since it's a direct
  reversal of a decision documented in the immediately preceding phase,
  not an oversight of it.
- No other deviations — hide/show works identically in spirit on
  desktop and mobile (one state, one control), no new colors were
  introduced, and no backend/routing/auth code was touched.

## Phase 15: standalone image upload support (PNG/JPEG)

### What it does

A user can now upload a single PNG or JPEG image directly (same
drag-drop/file-picker as PDF/DOCX) and get back a DOCX/PPTX exactly as
they do today — same job queue, same async worker, same job row, same
download experience. No changes to vision, classification, layout, or
assembly — those already handle image content unchanged (that's what
processes diagrams and scans extracted from PDF/DOCX pages today); this
phase is purely about getting a standalone image into that existing
flow as a valid one-page input.

### Ingestion hook: `ImageExtractor` (`app/core/ingestion/image_extractor.py`)

A new `Extractor` implementation (same interface as `PdfExtractor`/
`DocxExtractor`) that treats the whole uploaded file as **one IMAGE
element** — `source_page=1`, `raw_content=<the file's bytes>`,
`metadata={"image_ext": "png"|"jpeg"}`. Registered in
`app/core/ingestion/__init__.py`'s `_EXTRACTORS` dict for `.png`,
`.jpg`, `.jpeg`, alongside the existing `.pdf`/`.docx` entries — `
get_extractor()` dispatches to it exactly like any other format, so
`app/core/queue/worker.py`'s `_process_job_sync` (`classify_elements
(get_extractor(source).extract(source))` → `assemble_docx`/
`assemble_pptx`) needed **zero changes** to pick it up. The existing
classifier's `rule_image_is_diagram` (first rule in the chain, matches
any `ElementKind.IMAGE` regardless of source) routes the single element
to vision unchanged — this is the "reuse the existing per-page pipeline
entry point" the brief asked for: the hook point is `get_extractor()`,
not a new parallel pipeline.

- **Content validation happens here, not at upload time** — matching
  how a malformed PDF/DOCX is handled today (extension-checked at
  upload, format-validity checked when the real parser opens it during
  background processing). `ImageExtractor.extract()` opens the file
  with Pillow (`Image.open(...).verify()`) before returning; a corrupt
  file or a spoofed extension (e.g. a `.docx` renamed to `.png`) raises
  `ValueError`, which the worker's existing retry-then-FAIL handling
  catches — no new failure path, no new job state.
- **New direct dependency: Pillow** (`requirements.txt`, pinned
  `Pillow==12.3.0` — the version already present transitively via
  `python-pptx`, now pinned explicitly since the code imports it
  directly rather than relying on someone else's dependency to keep
  providing it).

### Upload validation (`app/api/routes/jobs.py`)

`_SUPPORTED_EXTENSIONS` extended from `{.pdf, .docx}` to `{.pdf, .docx,
.png, .jpg, .jpeg}` — the only change in this file. Upload-time
validation is still extension-only (no content sniffing), consistent
with the existing PDF/DOCX behavior and explicitly not changed by this
phase.

### Size limit: kept at 30MB, unchanged (explicit decision, flagged per
### the brief's own instruction not to change it silently)

The brief allowed a smaller limit specifically for images ("your call").
Kept the single existing `MAX_UPLOAD_SIZE_MB` (30) uniformly across all
formats rather than introducing a per-type limit — a 30MB image is
generously large for a phone photo (typical phone photos are 2–8MB), so
the shared limit doesn't meaningfully under-protect against oversized
images, and a per-type limit would be one more piece of config/state
for no concrete benefit at this stage. Flagged here explicitly per the
brief's instruction, not silently decided.

### Frontend (`components/UploadZone.tsx`, `components/HomeGreeting.tsx`)

`ACCEPTED_EXTENSIONS` gained `.png`/`.jpg`/`.jpeg` (drives both the
`accept` attribute on the file input and the client-side validation
message — no separate list to keep in sync). The visible label changed
from `"PDF, DOCX · maks. 30 MB"` to `"PDF, DOCX, PNG, JPEG · maks. 30
MB"`. The home-state subtitle (`HomeGreeting.tsx`) also updated from
"PDF və ya DOCX faylını sürükləyin..." to "PDF, DOCX faylını və ya
şəkli sürükləyin..." ("...or an image...") for consistency — not
explicitly required by the brief's requirements list, but the same
copy-consistency standard applied everywhere else in the app. No other
UI changes — an image-sourced job is just another job row, so it flows
through the unmodified sidebar/history/detail/download experience.

### Test coverage added

- **`tests/test_ingestion.py`** (`TestImageExtractor`): valid PNG →
  single IMAGE element with correct metadata; `.jpg` extension → `
  image_ext: "jpeg"`; corrupt bytes → `ValueError`; a real DOCX renamed
  to `.png` (spoofed extension) → `ValueError`. Plus `get_extractor()`
  dispatch coverage for the three new extensions.
- **`tests/test_assembly.py`** (`TestImageOnlyAssembly`): the real
  `flowchart_a3.png` sample run through `ImageExtractor` → `
  classify_elements` → `assemble_docx`/`assemble_pptx` with the
  existing `mock_vision` fixture (same real flowchart-solving mock
  already used for the PDF/DOCX assembly tests — no new mock needed,
  no Gemini call). Confirms the single element classifies as DIAGRAM,
  and both outputs are structurally valid packages (`_parse_package`,
  the same zip+XML-parses check already used for PDF/DOCX assembly)
  containing the reconstructed native shapes (a `wordprocessingCanvas`
  in DOCX, diagram shape text in PPTX) — the same "did it actually
  render as editable shapes, not a fallback picture" assertion style
  already established for the PDF/DOCX tests.
- **`tests/test_jobs_api.py`** (`TestUploadValidation`): PNG upload
  accepted (201); JPEG upload accepted (201); oversized image rejected
  (413, same `max_upload_size_mb` mock-patch pattern as the existing
  oversized-PDF/DOCX test). All use the real `flowchart_a3.png` sample
  bytes via the existing `flowchart_png_bytes` session fixture — no new
  binary fixture files added to the repo.
- **Cost control**: none of the new tests call the real Gemini API —
  `test_ingestion.py`'s tests never reach vision (they test the
  extractor in isolation), `test_assembly.py`'s use the existing
  `MagicMock`-based `mock_vision` fixture, and `test_jobs_api.py`'s
  only exercise upload validation/job creation, never actual
  processing (jobs stay PENDING; the worker isn't running in these
  tests, same as the existing PDF/DOCX upload tests).
- Full suite: **152 tests** (142 existing + 10 new), 0 failures, 0
  regressions.

### Validated

- Backend: full suite (152 tests) passes; `ruff`/`black`/`mypy` clean
  on every file this phase created or edited (pre-existing `Depends()`
  lint debt in `jobs.py`, documented since Phase 9, untouched and
  unrelated to this phase's changes).
- Frontend: `tsc -b` and `vite build` both clean.
- **One real end-to-end run, as instructed (not repeated)**: uploaded
  the real `samples/flowchart_a3.png` via a direct API call against the
  rebuilt Docker stack (a real Gemini call, not mocked), polled the job
  — `pending` → `processing` → `completed` in ~14s, both `docx` and
  `pptx` available. Downloaded and inspected the DOCX: valid Office
  package (`word/document.xml` parses), and — the meaningful check —
  its XML contains a `wordprocessingCanvas` with **no fallback
  picture**, confirming the real Gemini call actually reconstructed the
  flowchart as native editable shapes from a standalone image upload,
  not just "some file came back." Test job deleted afterward via the
  API.
- **Unsupported-type rejection re-verified live**: uploaded a `.txt`
  file via direct API call — `400 unsupported_format`, message now
  correctly lists `.docx, .jpeg, .jpg, .pdf, .png` as supported.
  Client-side `validate()` logic (mirrored in a browser console check
  since this tooling can't attach a real OS file to `<input
  type="file">`, the same limitation noted since Phase 8) confirmed
  rejecting `.txt` with the updated message and accepting `.png`/
  `.jpeg`.
- **Zero regressions confirmed live**: uploaded a real DOCX
  (`informatika_seh_40_41.docx`) via direct API call against the same
  rebuilt stack — still `201 pending`, unaffected by the extension-list
  change. Deleted afterward.
- Confirmed via the browser that the updated Azerbaijani copy
  ("PDF, DOCX faylını və ya şəkli sürükləyin...", "PDF, DOCX, PNG,
  JPEG · maks. 30 MB") renders correctly on the dashboard's home state.

### Deviations from the spec

- **Size limit kept uniform at 30MB** rather than a smaller
  image-specific limit — an explicit decision per the brief's own
  "your call, but don't silently change it" instruction, reasoned
  through and flagged above, not a silent choice.
- **`HomeGreeting.tsx` copy updated** in addition to the explicitly
  named `"PDF, DOCX · maks. 30 MB"` label — not separately called out
  in the requirements list, but left inconsistent would have meant one
  piece of UI copy still claiming only PDF/DOCX are accepted right next
  to the upload zone that now also accepts images.
- No other deviations — vision/classification/layout/assembly code is
  completely untouched, the `jobs` table/schema is unchanged (an
  image-sourced job is a normal job row, same columns), and no
  unrelated area (auth, pagination, search, sidebar) was touched.

## Phase 16: assembly bug fixes (title loss, shape collision, connector
## anchoring)

### What it does

Five concrete defects found by visually diffing real generated DOCX
output against its source images, all in the layout/assembly layer.
DOCX and PPTX share the layout engine (`app/core/layout.py`) completely
and share connector/label geometry helpers (`pptx_shapes.py`, imported
by `docx_shapes.py`) — confirmed by reading both renderers before
starting, per the phase's own instruction — so every fix below is
**one fix applied to shared code**, not two parallel ones, except where
noted.

### Bug 1 — title dropped entirely

**Root cause**: not a dropped *element* — a missing *field*.
`DiagramGraph` (the vision model's output schema, `app/models/diagram.py`)
had `nodes`/`edges` only; nothing to hold a heading line positioned
above the flowchart but outside its own nodes' text. The Gemini prompt
(`gemini_provider.py`) had no instruction to look for one either. A
title baked into the same source image as the flowchart (the case both
of the user's test images hit) had nowhere to go — it was never
extracted in the first place, not lost downstream. (A title that exists
as its own separate text element *before* an embedded image — e.g. the
real sample's q80 question line before its flowchart — was already
working correctly before this phase; that path was verified unaffected
and is what the existing `test_full_document_contains_all_element_kinds_in_order`
test covers.)

**Fix**: `DiagramGraph` gained `title: str | None = None`, mirroring
the existing `TextContent.heading` field from Phase 5. The Gemini
prompt now explicitly asks for it ("a heading/title line positioned
ABOVE the flowchart itself... NOT any node's own label... leave null
if the image has no such line — never invent one"), same
never-fabricate discipline as the existing heading field. Both
assemblers render it when present: `docx_assembler.py` adds a plain
paragraph directly before the diagram canvas; `pptx_assembler.py` adds
a heading text box on the diagram's own slide (reusing the existing
`_add_heading` helper built for `TextContent.heading`) and shrinks the
diagram's available height/shifts its top offset down to make room, so
title and diagram never overlap.

### Bugs 2/3 — shape collision + spurious terminal connector

**Root cause**: `layout.py`'s column assignment used exactly three
fixed slots — `"main"`, `"left"`, `"right"` — as literal string keys.
Every decision's branches were placed into the SAME `"left"`/`"right"`
columns regardless of which decision they belonged to. With two
decisions in one flow (or any graph where a second fork's branches land
at the same row as an already-placed node from the first), the second
fork's `place_run(..., "left", row)` / `place_run(..., "right", row)`
call would write `placed[node_id] = ("left", row)` for its own node
even though a *different* node already occupied that exact
`(column, row)` tuple — nothing checked for that collision. Since both
a node's x and y are derived purely from its `(column, row)` pair (not
from its own identity), the two nodes ended up at literally identical
coordinates. Bug 3 (the terminal node's connector looking like it
attached to the wrong shape) was traced to be a pure visual symptom of
this: connectors are attached by node id already (data-driven, not
position-based), so once two nodes stop sharing a position the
connector renders at its correct topological place — verified
explicitly with a dedicated test (see below) rather than assumed.

**Fix**: column assignment now uses a monotonically increasing,
never-reused integer counter. Every branch of every fork — not just a
second-and-later one — gets a brand-new column allocated from this
single graph-wide counter, alternating left/right of center
(-1, +1, -2, +2, ...) so the layout still reads as a roughly balanced
tree. Because each column is written to by exactly one continuous
downward walk (rows strictly increasing along it), and no column value
is ever handed out twice, two nodes can now provably never share a
`(column, row)` pair — this holds for any number of forks at any depth,
not just the two specific sample flowcharts. `NODE_SIZES`/`_node_size`/
row-height logic is unchanged; only the column bookkeeping changed.

### Bug 4 — edge label fused into the target/source node's own text

**Root cause**: `_add_edge_label`'s (`pptx_shapes.py`) label position
was computed as `source_node.y + source_node.height / 2 - 24` — the
source node's own vertical *center*, offset up by a fixed 24px. For a
DECISION source (90px tall, the tallest shape type), that formula lands
well inside the diamond's own bounding box, directly overlapping its
centered text — rendering as if the branch label were stacked onto the
node's own label. The identical formula was inlined separately in
`docx_shapes.py`.

**Fix**: new shared `_label_y()` helper in `pptx_shapes.py` (imported by
`docx_shapes.py`, replacing its inlined duplicate) positions the label
in the actual **gap** between the source's bottom edge and the target's
top edge — vertically centered in that gap, with a defensive fallback
(source-bottom + 4px) for the rare case where source/target aren't in
the normal one-row-below relationship. This can no longer overlap
either shape, for any label on any edge.

### Bug 5 — parallelogram connectors kink diagonally

**Root cause**: python-pptx's own `Connector._move_begin/end_to_cxn`
uses a shape-agnostic bounding-box-center formula for connection
indices 0 (top) and 2 (bottom) — that part was already fine. The actual
mismatch is in the OOXML **preset geometry** Office itself resolves an
attached connector's real endpoint against at render time: the built-in
"parallelogram" preset's top/bottom connection sites follow the shape's
*slanted* top/bottom edges, whose midpoints are `(shear + w) / 2` and
`(w − shear) / 2` respectively — offset from true center in *opposite*
directions, never equal to each other. Rectangles (and other
non-slanted presets) don't have this asymmetry, which is exactly why
only parallelogram-touching connectors kinked even between perfectly
vertically-aligned shapes, and why comparing against rectangles (as the
bug report suggested) surfaced the discrepancy.

**Fix**: since Office's built-in preset formula can't be edited from
our side, `INPUT_OUTPUT` nodes now get an explicit `<a:custGeom>`
instead of `<a:prstGeom prst="parallelogram">` — new shared
`parallelogram_geometry_xml()` in `pptx_shapes.py` (used directly by
`pptx_shapes.py` via `parse_xml()` + an in-place `spPr` swap, and
reused as a raw XML string by `docx_shapes.py`'s already-hand-rolled
shape XML). It draws the **identical visual outline** (same slant) but
defines an explicit `<a:cxnLst>` pinning connection points 0 (top) and
2 (bottom) to the shape's true bounding-box horizontal center —
matching rectangle behavior exactly — while 1 (left) and 3 (right) sit
at the slanted sides' midpoints for the less common
branch-out-of-a-parallelogram case. This is a general geometry fix, not
tuned to any specific node's dimensions — it recomputes from whatever
width/height the shape actually has.

### Testing

All new coverage is structural/unit-level against synthetic classified
data or the existing `flowchart_graph` fixture — no new Gemini-consuming
fixtures, per the phase's cost-conscious testing instruction:

- **`test_layout.py`** (`TestMultiForkCollision`, 4 new tests): a
  synthetic 8-node graph with a *second* decision nested inside the
  first's branch, deliberately engineered so the collision would occur
  under the pre-fix algorithm (traced by hand: n5/n6 from the second
  fork would have landed on `("left", row)` — the exact tuple n7,
  reached via the first branch's straight continuation, already
  occupied). Asserts: no two nodes overlap, all positions are distinct,
  the three same-rank nodes from different forks get three distinct
  columns, and the terminal node's incoming edge still references its
  correct topological source (bug 3, verified explicitly rather than
  assumed fixed).
- **`test_render.py`** / **`test_docx_shapes.py`** (2 new tests, one
  per renderer): builds the real `flowchart_graph` fixture, locates the
  branch-label shapes ("Hə"/"Yox") and the decision shape they originate
  from, asserts the label's top position is at or below the decision's
  bottom edge (never inside it) — PPTX via python-pptx shape geometry,
  DOCX via the raw `a:xfrm/a:off` XML.
- **`test_render.py`** / **`test_docx_shapes.py`** (2 new tests): for
  every `INPUT_OUTPUT` node, asserts the built-in `parallelogram`
  preset is gone, a `custGeom` is present with exactly 4 connection
  points, and points 0/2 (top/bottom) both sit at exactly `width // 2`.
- **`test_assembly.py`** (`TestImageOnlyAssembly`, 4 new tests): a new
  `mock_vision_with_title` fixture (same pattern as the existing
  `mock_vision`, just with `title` set on the returned `DiagramGraph`)
  proves the title appears as its own paragraph immediately before the
  DOCX canvas and as a heading text box on the PPTX diagram slide, plus
  two negative tests confirming no stray title text appears when
  `title` is `None` (the existing `mock_vision` fixture, unchanged).
- Full suite: **164 tests** (152 existing + 12 new), 0 failures, 0
  regressions. `ruff`/`black`/`mypy` clean on every file this phase
  touched (one pre-existing `N806` naming warning surfaced by a new
  `spPr` local variable, fixed to `sp_pr`; pre-existing
  `_remove_*table*` untyped-parameter debt from Phases 9/12, unrelated
  to this phase, left alone).
- **One real end-to-end regression check**, reusing the existing
  `flowchart_a3.png` sample already in `samples/`/the test fixtures
  (per the phase's own guidance to avoid burning a fresh Gemini call
  when a suitable existing fixture exists) — a real Gemini call, run
  once: uploaded via direct API call against the rebuilt Docker stack,
  completed in ~14s, both formats produced. Inspected the real DOCX
  output's XML directly: `wordprocessingCanvas` present (native shapes,
  not a fallback picture, confirming the pipeline still works
  end-to-end), `custGeom` present and the `parallelogram` preset string
  completely absent (bug 5's fix landed in real output), and all 8
  nodes' `(x, y)` positions distinct with zero collisions (bugs 2/3).
  This sample has no title baked in, so bug 1 wasn't exercised by this
  particular real run — it's covered by the structural
  `mock_vision_with_title` tests instead. Test job deleted afterward.

### Deviations from the spec

- **Bug 1's root cause was in the vision schema/prompt, not pure
  classification or pure assembly** — the phase brief anticipated this
  possibility explicitly ("classification (beyond title element
  handling if that's genuinely where bug 1 lives)... vision-calling
  code... if genuinely traced there") and reasoned through why a
  separately-extracted title element was already working (proven by an
  existing, unmodified test), before concluding the actual gap was a
  title baked into the same image as the diagram having no schema field
  to land in.
- No other deviations — all five bugs were fixed at their traced root
  cause, DOCX and PPTX both received every fix (confirmed to share the
  layout engine and connector/label/geometry helpers before touching
  anything), no bug was special-cased to the two diagnostic sample
  flowcharts (the collision fix in particular was validated against a
  synthetic graph engineered to be structurally different from both),
  and no unrelated area (auth, pagination, upload validation, sidebar,
  mobile layout) was touched or re-tested, per the phase's explicit
  scope note.

## Phase 17: connector routing fix — edge-level detour (superseded)

**Status**: superseded by Phase 18 (2026-08-15)

Phase 17 attempted to fix the "connector lines cross through unrelated
nodes" problem via edge-level routing: detecting collisions between a
back-edge's straight path and a node's bounding box, then routing the
edge around the node with a freeform polyline detour. This approach was
structurally implemented and passes all its tests, but the real output
(verified in actual Microsoft Word and PowerPoint) still showed
connectors crossing through nodes. The edge-routing approach is
fundamentally insufficient because the root cause is not "how to route
an edge around a node" but "why is this node placed in the path of this
edge in the first place?" Phase 18 fixes the root cause at the layout
level. The Phase 17 detour code is retained as a safety net for the
rare case where two loop-back siblings share a row (one back-edge must
cross the other node to reach the rail); it is no longer the primary
collision-avoidance mechanism.

### Code retained from Phase 17

- `back_edge_would_collide`, `back_edge_detour_points`,
  `_add_freeform_connector`, `_polyline_wsp`, `_hseg_crosses_node`,
  `_occupied_y_bands`, `_clear_y_above`, `polyline_geometry_xml` — all
  kept in `pptx_shapes.py` / `docx_shapes.py` as a safety net for
  sibling back-edge crossings that the layout-level fix cannot eliminate
  (two loop-back nodes from the same fork sharing a row).

## Phase 18: layout-level fix for connectors crossing nodes

**Status**: complete (2026-08-15)

### Why Phase 17's edge-routing approach failed

The layout engine assigned column positions to nodes without knowing
where back-edge corridors (the horizontal/vertical paths loop-back
connectors travel) would run. When a decision had both a continuation
branch (no loop-back) and a loop-back branch, the alternating column
allocator could place continuation nodes between loop-back nodes and
the back-edge rail — exactly where the back-edge's horizontal segment
needed to pass. No amount of edge-level routing can cleanly fix a node
that is physically inside the connector's corridor; the node must not
be placed there in the first place.

### Root cause

The layout engine's `alloc_column()` used a single counter alternating
left/right (−1, +1, −2, +2, …) without regard for which branches would
eventually loop back. In the bug-report topology:

```
n2 fork → n4 (col −1, loop-back subtree) + n3a (col +2, continuation)
n4 fork → n5 (col +1) + n6 (col −2)
```

n3a/n3 (continuation, col +2) ended up to the RIGHT of n5 (loop-back,
col +1) and between n5 and the back-edge rail — directly in the path
of n5→n2's horizontal connector segment.

### Fix

Redesigned column allocation in `layout_graph()` with a DFS pre-scan:

1. **Back-edge detection** (`_detect_back_edges`): DFS from the start
   node identifies all cycle-forming edges before placement begins.

2. **Subtree classification** (`_has_loop`): for each node, determines
   whether its forward subtree contains any back-edge. Cached to avoid
   redundant traversals.

3. **Directional column allocation**: at each fork, branches are
   classified as continuation (no loops) or loop-back:
   - **Mixed forks** (some loop, some don't): continuation branches get
     LEFT columns (`_alloc_left`: −1, −2, −3, …), loop-back branches
     get RIGHT columns (`_alloc_right`: +1, +2, +3, …).
   - **All-continuation forks** (no loops): original alternating
     allocation preserved for visual balance.
   - **All-loop-back forks**: all branches go RIGHT; the Phase 17
     detour code handles the rare sibling crossing as a safety net.

Result for the bug-report topology:

```
n2 fork → n3a (col −1, continuation, LEFT) + n4 (col +1, loop-back, RIGHT)
n4 fork → n5 (col +2, loop-back) + n6 (col +3, loop-back)
```

n3a/n3 are now far LEFT (x ≈ 50), completely clear of all back-edge
corridors. n6 (rightmost, col +3) has a clear path to the rail
(bentConnector3). n5 (col +2) has a sibling crossing with n6 only —
handled by the Phase 17 detour safety net.

### DOCX/PPTX parity

Both formats share the same `layout_graph()` function in `layout.py`.
DOCX imports collision-detection helpers from `pptx_shapes.py`. No
separate layout implementations exist — the fix applies to both formats
automatically with identical node positions (differing only in absolute
coordinates from the scale-to-fit step).

### Files changed

- **`backend/app/core/layout.py`** — rewrote column allocation:
  added `_detect_back_edges` (DFS pre-scan), `_has_loop` (cached
  subtree classification), three directional allocators
  (`_alloc_left`, `_alloc_right`, `_alloc_alternating`). `place_run`
  now classifies each fork's branches before choosing columns.

### Tests (174 total, all passing)

Updated Phase 17 tests to match the new layout behavior:

- **`test_layout.py`** (`TestBackEdgeCollisionDetection`, 5 tests):
  back-edges flagged, no overlaps, continuation nodes LEFT of loop-back
  nodes, no back-edge rail crosses any continuation node's bounding box,
  simple single-loop-back case also clean.

- **`test_render.py`** (`TestBackEdgeDetourPptx`, 2 tests): continuation
  nodes not crossed by any back-edge corridor; one freeform polyline
  (sibling crossing) + one rail bentConnector3 (clean path).

- **`test_docx_shapes.py`** (`TestBackEdgeDetourDocx`, 3 tests): mixed
  routing (1 detour + 1 rail), valid XML, unique IDs.

### Visual verification note

Correctness verified via coordinate/position inspection: continuation
nodes' x-ranges are entirely left of all loop-back nodes' x-ranges,
and back-edge horizontal segments at source mid-height do not intersect
any continuation node's bounding box. Final visual confirmation in real
Microsoft Word/PowerPoint is recommended before fully closing this
issue, since coordinate-level verification is necessary but not
sufficient (rendering engines can still surprise).

---

## Phase 19 — Diagram Layout & Structural Correctness (2026-08-15)

**Goal**: Diagnose and fix four observed symptoms in diagram rendering,
all at the layout/rendering engine level (generalized, not special-cased).

### Symptom 1: Sibling loop-back branch collision

**Root cause**: When a decision fork had 2+ loop-back branches (e.g. n5
and n6 both looping back to n4), all loop-back siblings were placed on
the same row. This caused their bounding boxes to vertically align,
making the horizontal segment of one sibling's back-edge connector
cross through the other sibling's shape.

**Fix** (`layout.py`): In `place_run()`, when a fork has ≥2 loop-back
branches, each loop-back sibling is placed on a successively lower row
(`branch_row + loop_row_offset`). This vertical stagger guarantees
each back-edge's horizontal segment travels at a unique y-coordinate
that doesn't intersect any other node.

**Verification confidence**: HIGH — 3 new tests in
`TestSiblingLoopBackStagger` directly verify stagger for 2-way and
3-way loop-back forks, and assert `back_edge_would_collide` returns
False for every back-edge.

### Symptom 2: Unglued freeform/detour connectors

**Root cause**: Phase 17's safety-net detour code
(`_add_freeform_connector` / `_polyline_wsp`) activated when
`back_edge_would_collide` returned True — creating unglued
`custGeom` polylines instead of glued `cxnSp bentConnector3`
connectors. These polylines had no `stCxn`/`endCxn` and became
disconnected when shapes moved.

**Fix**: The Symptom 1 stagger eliminates the collision condition
entirely, so the detour code path never activates. All back-edges
now use glued `bentConnector3` connectors with proper `stCxn`/`endCxn`.
The detour code remains as dead-letter safety net but is no longer
exercised for any tested topology.

**Verification confidence**: HIGH — tests
`test_all_back_edges_glued_no_freeform` (PPTX) and
`test_all_back_edges_glued_no_detour` (DOCX) assert 0 freeform/detour
elements and verify all back-edge connectors are glued bentConnector3.

### Symptom 3: Edge labels mispositioned

**Root cause**: Labels were placed at the midpoint between source and
target node centers. For side-branching edges (where the connector
exits horizontally before bending down), this midpoint was far from
the actual connector path — often overlapping the source decision
shape or floating in empty space.

**Fix** (`pptx_shapes.py`): New `label_position()` function positions
labels based on the connector's exit point from the source:
- **Vertical edges** (same column): label centered between source
  bottom and target top (original behavior, correct for these).
- **Left-branching edges**: label placed just left of the source
  shape at its vertical midpoint.
- **Right-branching edges**: label placed just right of the source
  shape at its vertical midpoint.

`docx_shapes.py` imports and uses the same `label_position()` function
for DOCX parity.

**Verification confidence**: MEDIUM-HIGH — tests verify labels don't
overlap source shapes (horizontal non-overlap assertion). Exact visual
positioning on the connector path depends on connector geometry in the
rendering application; structural tests confirm labels are adjacent to
the correct edge of their source shape.

### Symptom 4: No structural verification

**Root cause**: No automated mechanism existed to verify that the
rendered OOXML output structurally matched the input graph.

**Fix**: Added two test classes:
- `TestStructuralVerificationPptx` (5 tests): verifies node texts
  match input, connector count matches edge count, all connectors
  have glued endpoints (`stCxn`/`endCxn`), edge labels present,
  and `collision_graph` topology renders completely.
- `TestStructuralVerificationDocx` (5 tests): same verification
  pattern for DOCX/WordprocessingML Drawing Canvas output.

**Verification confidence**: HIGH — tests directly parse the XML
output and compare against the input `DiagramGraph` structure.

### DOCX/PPTX parity

All fixes apply to both formats:
- Layout stagger is in the shared `layout_graph()` function.
- `label_position()` is defined in `pptx_shapes.py` and imported
  by `docx_shapes.py`.
- Structural verification tests cover both formats with identical
  assertions.

### Files changed

- **`backend/app/core/layout.py`** — sibling loop-back row stagger
  in `place_run()` (loop_row_offset counter for ≥2 loop-back branches).
- **`backend/app/core/render/pptx_shapes.py`** — new `label_position()`
  function; `_add_edge_label()` updated to use it.
- **`backend/app/core/render/docx_shapes.py`** — switched from
  `_label_y` import to `label_position` import; label coordinates
  now computed via shared function.
- **`backend/tests/test_layout.py`** — added `TestSiblingLoopBackStagger`
  (3 tests: 2-way stagger, no-collision assertion, 3-way fork).
- **`backend/tests/test_render.py`** — updated label overlap test
  (horizontal non-overlap), renamed freeform test to
  `test_all_back_edges_glued_no_freeform`, added
  `TestStructuralVerificationPptx` (5 tests).
- **`backend/tests/test_docx_shapes.py`** — updated label overlap test,
  renamed detour test to `test_all_back_edges_glued_no_detour`, added
  `TestStructuralVerificationDocx` (5 tests).

### Tests (187 total, all passing)

13 new tests added, 4 existing tests updated to match new behavior.
Full backend suite: 187 passed, 0 failed.

---

## Phase 20 — Edge Label Positioning Fixes (2026-08-15)

**Goal**: Fix two label-positioning bugs confirmed via real Word/PowerPoint
testing — overlapping sibling labels and labels sitting on (not above)
the connector line.

### Bug A: Sibling labels at identical coordinates

**Root cause**: `label_position()` computed the label's y-coordinate
solely from the source node's geometry and the exit direction
(left/right). When two labeled edges left the same source in the same
direction (e.g., n4→n5 "Hə" and n4→n6 "Yox", both exiting right),
both labels received identical `(x, y)` coordinates — one was fully
hidden behind the other.

**Fix**: Added `compute_label_indices(graph)` — groups labeled edges by
`(source_id, exit_direction)` and assigns each a 0-based index within
its group. `label_position()` now accepts an `exit_index` parameter;
each subsequent sibling is offset by
`exit_index * (_LABEL_HEIGHT_PX + _LABEL_OFFSET_PX)` vertically,
staggering labels downward so they never overlap. Handles 2, 3, or N
siblings at the same exit point.

**Verification confidence**: HIGH — coordinate-level tests verify no
two sibling labels (2-way and 3-way) have overlapping bounding boxes;
PPTX output test confirms distinct `.top` values; DOCX output test
confirms distinct `a:off` y-coordinates.

### Bug B: Labels centered on connector line

**Root cause**: For side-branch labels, `label_y` was computed as
`source_node.y + source_node.height / 2 - _LABEL_HEIGHT_PX / 2`,
which vertically centered the label on the source's mid-height —
exactly where the connector's horizontal segment exits the shape.
The connector line bisected the label text.

**Fix**: Changed to
`source_node.y + source_node.height / 2 - _LABEL_HEIGHT_PX - _LABEL_OFFSET_PX`,
so the label's bottom edge sits just above the connector line (offset
by `_LABEL_OFFSET_PX` = 4px). The label text is now fully above the
line, never bisected by it.

**Verification confidence**: HIGH — tests assert that for every
first-sibling side-branch label, `label_bottom <= connector_y`.

### DOCX/PPTX parity

Both fixes apply to both formats:
- `compute_label_indices()` and updated `label_position()` are defined
  in `pptx_shapes.py` and imported by `docx_shapes.py`.
- Both renderers pre-compute indices and pass them through identically.
- Parallel test classes verify the behavior in both output formats.

### Files changed

- **`backend/app/core/render/pptx_shapes.py`** — added
  `compute_label_indices()`; updated `label_position()` with
  `exit_index` param, above-line offset; updated `_add_edge_label()`
  and both call sites in `add_diagram_to_slide()`.
- **`backend/app/core/render/docx_shapes.py`** — imported
  `compute_label_indices`; pre-computes indices and passes to
  `label_position()`.
- **`backend/tests/test_render.py`** — added `TestLabelPositioningPptx`
  (4 tests: 2-sibling no-overlap, 3-sibling no-overlap, above-line
  assertion, PPTX output distinct positions).
- **`backend/tests/test_docx_shapes.py`** — added
  `TestLabelPositioningDocx` (3 tests: 2-sibling distinct positions,
  3-sibling no-overlap, above-line assertion).

### Tests (194 total, all passing)

7 new tests added. All 187 Phase 19 tests still pass.
Full backend suite: 194 passed, 0 failed.

## Phase 21: Replace Custom Layout Engine with Graphviz

### What it does

Replaces the custom column/row allocation, back-edge stagger logic,
collision avoidance, and detour routing in `layout.py` with Graphviz's
`dot` algorithm. The layout engine now shells out to Graphviz via the
`graphviz` Python package (v0.21, pure Python) to compute node
positions, edge label placements, and back-edge detection — all in one
`dot -Tjson0` call. Everything downstream of layout (OOXML shape
generation, connector gluing, structural verification tests) is
unchanged.

### Why

The custom layout engine (Phases 3, 16–20) accumulated increasingly
complex patching — column allocation, collision avoidance, detour
routing, label stagger logic — and each fix risked regressions in
other graph topologies. Graphviz's `dot` algorithm handles hierarchical
directed-graph layout as a solved problem, with proper rank assignment,
crossing minimization, and label placement out of the box. This
eliminates the entire class of layout bugs the custom engine was prone
to, at the cost of a system dependency (the Graphviz binary).

### Architecture

- **`graphviz` Python package (v0.21)**, NOT `pygraphviz`. The pure
  Python `graphviz` package shells out to the `dot` binary and parses
  its output. `pygraphviz` is a C extension that compiles against
  `libgraphviz-dev` — nearly impossible to build on Windows without a
  full C toolchain. The pure Python package works on all platforms as
  long as the `dot` binary is on PATH.

- **Graphviz binary on PATH**: `layout.py` ensures it by checking
  `GRAPHVIZ_BIN` env var (default `C:\Program Files\Graphviz\bin` on
  Windows) and prepending to `os.environ["PATH"]`. In Docker, the
  `graphviz` system package is installed via `apt-get`.

- **Single `dot -Tjson0` call**: the layout engine builds a
  `graphviz.Digraph`, sets node sizes based on text/shape type, and
  calls `g.pipe(engine='dot', format='json0')`. The JSON output
  provides node center coordinates (`pos`), dimensions (`width`/
  `height` in inches), edge label positions (`lp`), and the graph
  bounding box (`bb`).

- **Coordinate conversion**: Graphviz uses 72-dpi points with y=0 at
  bottom. Layout px uses 96-dpi with y=0 at top. Conversion:
  `px_x = gv_x * (96/72) + margin`, `px_y = (bb_max_y - gv_y) *
  (96/72) + margin`. Constants: `_GV_DPI=72`, `_LAYOUT_DPI=96`,
  `_GV_TO_PX=96/72`, `_MARGIN_PX=20`.

- **Back-edge detection**: post-layout, by comparing node y-coordinates
  (target center y <= source center y = back-edge). This replaces the
  pre-layout structural detection from the custom engine.

- **Edge label positions**: extracted from Graphviz's `lp` attribute on
  edges with labels, converted to layout px using the same y-axis
  inversion. Stored as `label_x`/`label_y` on `DiagramEdge`.

### Schema changes (`app/models/diagram.py`)

`DiagramEdge` gained two optional fields:
- `label_x: float | None` — label center x in layout px
- `label_y: float | None` — label top y in layout px

Both are set by the layout engine and consumed by both renderers.

### Dead code removed

**From `pptx_shapes.py`**: `compute_label_indices()`,
`label_position()`, `_label_y()`, `back_edge_would_collide()`,
`back_edge_detour_points()`, `_add_freeform_connector()`,
`polyline_geometry_xml()`, `_occupied_y_bands()`, `_clear_y_above()`,
`_hseg_crosses_node()`.

**From `docx_shapes.py`**: imports of `compute_label_indices`,
`label_position`, `polyline_geometry_xml`; the `_polyline_wsp()`
function; detour code path in `add_diagram_to_docx()`.

**From `layout.py`**: the entire custom layout engine (column/row
allocation, `_row_for_node`, `_allocate_columns`, collision avoidance,
back-edge stagger logic).

### What was kept unchanged

- OOXML shape generation (custGeom parallelograms, rounded rects,
  diamonds) — only the coordinates fed into it changed.
- Connector gluing (`stCxn`/`endCxn`) — same mechanism, same idx values.
- `parallelogram_geometry_xml()`, `_rail_x()`, `back_edge_rail_adj()`,
  `_set_connector_adj()`, `_font_size_for()`, `_px_to_emu()`,
  `SHAPE_TYPE_TO_MSO` — all kept.
- Back-edges always use `bentConnector3` (no freeform detours needed
  with Graphviz layout — the positions are clean enough that the simple
  rail route always works).
- SVG debug renderer, title rendering, vision/classification, ingestion,
  auth, frontend — all untouched.

### Label rendering changes

Both renderers' `_add_edge_label()` functions now take `edge:
DiagramEdge` directly and use `edge.label_x`/`edge.label_y` for
positioning, instead of calling the deleted `label_position()` and
`compute_label_indices()`. Label overlap tests were updated to check
full 2D bounding box non-overlap (position is set by Graphviz, which
may place labels at the same y but different x for sibling edges).

### Dependencies

- **`graphviz==0.21`** added to `backend/requirements.txt`.
- **`graphviz` system package** added to `backend/Dockerfile`
  (`apt-get install -y graphviz` before pip install).

### Tests

**Removed**: `TestBackEdgeCollisionDetection`,
`TestSiblingLoopBackStagger` (referenced deleted functions), custom
column allocation assertions, old label positioning tests that
referenced `compute_label_indices`/`label_position`.

**Kept**: `TestFlowchartLayout` (overlap, back-edge flagging, positive
geometry, top-to-bottom, determinism), `TestMultiForkCollision` (overlap,
distinct positions), `TestStructuralVerificationPptx`,
`TestStructuralVerificationDocx`, `TestSvgDebugRenderer`,
`TestPptxShapes`, `TestDocxCanvas`, `TestBackEdgeDetourDocx`.

**Added**: `TestBackEdgeLayout` (flagging, overlap, positive geometry),
`TestEdgeLabelPositions` (label coordinates present, sibling distinct,
y-axis inversion, 3-way fork), `TestLabelPositioningPptx` (distinct
positions in PPTX), `TestLabelPositioningDocx` (distinct positions in
DOCX, 3-sibling no-overlap, Graphviz coordinate presence).

All 54 layout/render/docx-shape tests pass (0 failures).

### Test infrastructure change (`tests/conftest.py`)

The `_migrated_test_database` session fixture was updated to handle
missing test database gracefully (catches `TimeoutExpired` and
non-zero exit from alembic, issues a warning instead of failing) — this
allows layout/render tests to run without a PostgreSQL test database
running. Database-dependent tests (auth, jobs, queue) will still fail
without the database, as expected.

### Deviations from spec

- **Used `graphviz` (pure Python) instead of `pygraphviz` (C extension)**:
  `pygraphviz` requires C compilation against `libgraphviz-dev`, which
  is impractical on Windows. The pure Python `graphviz` package provides
  identical layout results by shelling out to the same `dot` binary.

- **Label overlap tests check 2D bounding box, not just horizontal
  separation**: Graphviz may place sibling labels at the same y but
  different x (along the edge path), so the old horizontal-only
  non-overlap check was too strict. Updated to full 2D bounding box
  non-overlap, matching the PPTX renderer's existing check.

- **PPTX sibling label test checks position (x,y), not just top**:
  Same reason — Graphviz may give siblings the same y but different x.

### Visual confirmation caveat

All tests verify structural correctness (no overlaps, correct gluing,
correct coordinates, correct XML). **Final visual confirmation in real
Microsoft Word/PowerPoint is recommended** — do not claim visual
correctness based on tests alone.

---

## Phase 22 — Use Graphviz's Real Edge Routing (2026-08-16)

### Problem

Phase 21 extracted node positions from Graphviz but **discarded the
computed edge paths**, drawing connectors as naïve straight or bent
lines. These could cross through unrelated nodes and labels. The root
cause: Graphviz's `pos` attribute on edges (containing the real
collision-free route) was never parsed.

### Solution

1. **`splines="ortho"`** added to the Graphviz graph attributes, giving
   axis-aligned (orthogonal) edge paths that avoid node intersections.

2. **Edge path extraction** (`layout.py`): new helpers `_parse_edge_pos`
   and `_simplify_ortho_path` parse the B-spline control points from
   Graphviz's `pos` attribute, convert coordinates to layout px, and
   simplify to meaningful waypoints (segment endpoints, deduplicated,
   collinear-interior-removed). Stored as `DiagramEdge.edge_path`.

3. **`xlabel` instead of `label`**: Graphviz warns that ortho splines do
   not handle edge labels — switched to `xlabel` (external labels) and
   read positions from `xlp` (falls back to `lp`).

4. **Connector preset selection** (`pptx_shapes.py`): new function
   `choose_connector_from_path()` maps ortho waypoints to OOXML
   connector presets:
   - 0 bends → `straightConnector1`
   - 1 bend → `bentConnector2`
   - 2 bends → `bentConnector3`
   - 3 bends → `bentConnector4`
   - 4 bends → `bentConnector5`

   Also computes OOXML adjustment values (`adj1`–`adj3`) as ratios of
   bend positions within the connector's bounding box, and determines
   connection site indices (0=top, 1=left, 2=bottom, 3=right) from the
   path's start/end directions.

5. **Safety net**: paths with >4 bends are capped at 4 (logged as
   warning), using `bentConnector5`. In testing this never triggered.

6. **Back-edge routing unchanged**: back-edges still use the rail-based
   `bentConnector3` route from Phase 19.

### DOCX / PPTX parity

Both renderers share `choose_connector_from_path()` from `pptx_shapes`.
DOCX uses `_cxn_site_pos_docx()` for endpoint calculation (same logic,
different coordinate source). All connectors in both formats are glued
(`stCxn`/`endCxn`).

### Schema changes (`app/models/diagram.py`)

`DiagramEdge` gained one optional field:
- `edge_path: list[list[float]] | None` — ordered waypoints `[[x,y], ...]`
  in layout px, set by the layout engine, consumed by renderers.

### Files changed

| File | Change |
|------|--------|
| `app/models/diagram.py` | Added `edge_path` field |
| `app/core/layout.py` | `splines="ortho"`, `xlabel`/`xlp`, edge path extraction |
| `app/core/render/pptx_shapes.py` | `choose_connector_from_path()`, adj computation, preset selection |
| `app/core/render/docx_shapes.py` | Uses shared `choose_connector_from_path()`, `_cxn_site_pos_docx()` |
| `tests/test_layout.py` | `TestOrthoEdgePaths` (6 tests), `TestSafetyNet` (3 tests) |

### Test results

- **95 non-DB tests pass** (27 layout + 36 render/docx + 32 assembly/ingestion)
- All 15 existing render tests and 21 existing DOCX shape tests pass unchanged
- New layout tests verify: all edges get paths, paths are axis-aligned,
  no connector crosses any node, no connector crosses any label,
  complex topologies (10+ nodes) route cleanly
- Safety-net tests verify: 6-bend path capped to 4 with warning,
  straight path → `straightConnector1`, 1-bend → `bentConnector2`

### Safety-net frequency

The safety net (>4 bends cap) **never triggered** in any test fixture or
sample graph. All real graphs produced 0–2 bends per edge. The cap
exists as defense-in-depth.

### Connector preset distribution (sample graph)

For the "Blok-sxem nəyi hesablayır?" 8-node graph:
- 6× `straightConnector1` (0 bends)
- 1× `bentConnector2` (1 bend, the "Yox" branch)
- 1× `bentConnector3` (back-edge rail, n8→n4)

### Deviations from spec

None. All requirements implemented as specified.

### Visual confirmation caveat

All tests verify structural correctness (axis-aligned paths, no
node/label crossings, correct OOXML presets, correct adj values, correct
gluing). **Final visual confirmation in real Microsoft Word/PowerPoint is
still needed** — the connector geometry is mathematically correct but
rendering subtleties (especially adj value interpretation across Office
versions) can only be verified visually.

---

## Phase 23 — Enforce Orthogonal Connectors (No Diagonal Lines) (2026-08-16)

### Root cause found and fixed

The edge `n4→n6` (the "Yox" branch from `a>b` to `b=b-a`) was rendered
as a diagonal `straightConnector1`. Root cause traced to a threshold
bug in `_enforce_no_diagonal()`:

Graphviz's ortho spline output for this edge collapsed to only 2
waypoints (no intermediate bend point). `_count_bends()` correctly
returned 0 for a 2-point path. `_enforce_no_diagonal()` then checked
whether the connection-site positions differed in both x and y, but
used a **layout-pixel threshold of 1.0 px**. The connection sites for
this edge differed by only ~0.7 px in x (source right-side at x=338.0
vs. target top at x=338.7) — below the 1.0 threshold, so the function
concluded "these are vertically aligned" and kept 0 bends. But at the
EMU level (1 px = 9525 EMU), that 0.7 px difference became ~6667 EMU —
enough for Office to render a visibly diagonal line. The threshold was
in layout-px when it needed to be in EMU.

### Fix (two layers)

**Layer 1 — `_enforce_no_diagonal()` threshold fix**: Changed from a
1.0 layout-px threshold to an EMU-level threshold: differences are now
compared as `abs(dx) * 9525 > 1` and `abs(dy) * 9525 > 1`. Any
sub-pixel offset that would produce even 2 EMU of diagonal now triggers
promotion from 0-bend to 1-bend (`bentConnector2`, L-shaped).

**Layer 2 — `assert_no_diagonal_connector()` auto-correction**: Changed
from raising `DiagonalConnectorError` (which crashed the pipeline) to
**auto-correcting**: if a `straightConnector1` is about to be emitted
with both cx and cy non-zero, it logs a warning and returns
`"bentConnector2"` as the corrected preset. The calling code in both
`pptx_shapes.py` and `docx_shapes.py` applies the correction to the
OOXML element. This makes diagonal output structurally impossible — even
if Layer 1's threshold were somehow bypassed, Layer 2 catches it.

### Hard invariant — where it runs

The invariant runs in two places per connector, in both renderers:

1. **`_enforce_no_diagonal()`** in `choose_connector_from_path()` —
   prevents 0-bend classification when sites differ in both axes
   (preventive, at decision time).
2. **`assert_no_diagonal_connector()`** after the connector element is
   created — checks the actual OOXML `ext.cx`/`ext.cy` values and
   auto-corrects the preset if needed (defensive, at emit time).

Both run for every non-back-edge connector in both `pptx_shapes.py`
and `docx_shapes.py`.

### Synthetic test battery — 23 topologies, 133 tests

New file `tests/test_no_diagonal.py` generates 23 distinct flowchart
topologies and runs 6 invariant checks on each (where applicable):

| Check | Scope |
|-------|-------|
| No diagonal `straightConnector1` (PPTX) | All topologies with edges |
| No diagonal `straightConnector1` (DOCX) | All topologies with edges |
| All connectors glued — stCxn/endCxn (PPTX) | All topologies with edges |
| All connectors glued — stCxn/endCxn (DOCX) | All topologies with edges |
| No edge path crosses unrelated node bbox | All topologies with edges |
| No two nodes overlap | All topologies |

Topologies include: linear chains (3, 5 nodes), single/two/three
sequential decisions, 3-way and 4-way forks, simple loop, nested loops,
diamond merge, asymmetric branches (depth mismatch), 3-decision chain,
loop with exit fork, wide merge (4→1), two sequential loops, all-I/O
shapes, all shape types mixed, long Azerbaijani text, double back-edge,
decision-into-decision, single node, two nodes, the exact Phase 23
bug-report topology (`collision_graph`), and self-loop.

All 133 tests pass. Combined with the 63 existing layout/render/docx
tests (also all passing), the total non-DB test count is 228.

### Real Gemini verification — 1 call used (3 remaining)

Ran `scripts/verify_phase23.py` on `samples/Blok Sxem1.jpeg` (the
exact image that exposed the diagonal bug in real Word). Gemini returned
a 10-node, 11-edge graph including the problematic `n4→n5→n6`/`n5→n7`
subgraph. Generated both DOCX and PPTX, inspected the XML directly:

- **PPTX**: 11 connectors, zero diagonal, all glued. The formerly
  problematic edges now use `bentConnector2` (L-shaped) instead of
  `straightConnector1`. Pure-vertical connectors correctly have `cx≤1`.
- **DOCX**: 11 connectors, zero diagonal, all glued. Same correct
  preset distribution.

### Files changed

| File | Change |
|------|--------|
| `app/core/render/pptx_shapes.py` | `_enforce_no_diagonal` threshold fix (EMU-level); `assert_no_diagonal_connector` auto-corrects instead of raising; PPTX call site applies correction |
| `app/core/render/docx_shapes.py` | DOCX call site uses returned preset from `assert_no_diagonal_connector` |
| `tests/test_no_diagonal.py` | New: 23 topologies × 6 checks = 133 tests |
| `scripts/verify_phase23.py` | New: real Gemini verification script |

### Deviations from spec

- **Gemini calls used: 1 of 4** — the first call confirmed the fix
  cleanly; the remaining 3 are held in reserve rather than spent
  automatically, per the spec's guidance to use the budget deliberately.
- **`assert_no_diagonal_connector` auto-corrects rather than raising** —
  the spec left this as "your call"; auto-correction was chosen because
  a runtime crash (the prior `DiagonalConnectorError`) would break the
  user's job pipeline, while auto-correction silently fixes the edge
  case and logs it for investigation.

### Visual confirmation caveat

All checks verify structural OOXML correctness — no diagonal presets,
correct ext values, correct gluing. **Final visual confirmation in real
Microsoft Word/PowerPoint by the user is still the last word** —
coordinate/XML checks catch what they check for, but the user should do
one more visual pass on the generated `samples/output/phase23_verify.pptx`
and `phase23_verify.docx` before this is considered fully closed.

---

## Phase 24 — Back-Edge Rail Removal + General Connector-Validity Fixes (2026-08-31)

**Goal**: fix a real, user-reported bug where a complex diagram (nested
decision with a double loop-back — the "Euclid's algorithm" flowchart,
`a==b` / `a>b` / `a=a-b` / `b=b-a`) rendered a connector cutting straight
through the `a>b` decision shape in real PowerPoint/Word, and generalize
the fix so it doesn't recur in other complex diagrams — not just this
one file.

### Root cause

`pptx_shapes.py`/`docx_shapes.py` special-cased **back-edges** (loop-back
connectors) onto a hand-rolled "virtual rail" mechanism left over from
Phase 19, *before* Graphviz was integrated (Phase 21/22 kept it
"unchanged" rather than re-deriving it): both ends were forced to
connect via the RIGHT connection site, and a single `adj1` ratio was
computed to place the rail at `rail_x = max(all nodes' right edge) + 50px`.

This is mathematically broken: a glued OOXML connector's bounding box is
always exactly the rectangle spanned by its two connection points — it
can never actually extend past them. Whenever the rail position fell
*outside* that local box (the normal case, unless one of the two nodes
happened to be the single rightmost shape in the whole diagram), the
ratio blew up or got clamped to the box's own edge — landing the "rail"
segment wherever that box's edge happened to be, which in the bug
report was directly on top of the `a>b` diamond.

Verified directly: Graphviz's own computed path for the affected
back-edges (`edge.edge_path`, already computed by `layout_graph()` since
Phase 22) was a perfectly clean, node-avoiding route — the renderer just
never used it for back-edges.

### Fix 1 (the reported bug): back-edges use the same path as forward edges

Removed the `if edge.is_back_edge: ...` special case entirely from both
`add_diagram_to_slide()` and `add_diagram_to_docx()`. Every edge —
forward or back — now routes through the same, already-tested
`choose_connector_from_path(edge.edge_path, ...)` pipeline. Deleted the
now-dead `_rail_x()` and `back_edge_rail_adj()` (and DOCX's
`_RAIL_PAD_PX` canvas-width padding, which existed solely to reserve
space for the removed rail).

### Fix 2 (found while re-testing): invalid same-axis single-bend connectors

A single-bend elbow (`bentConnector2`) is only geometrically valid
between sites on PERPENDICULAR axes (one top/bottom, the other
left/right) — a 2-segment L cannot connect two same-axis sites (both
left/right, or both top/bottom) unless they already share the
perpendicular coordinate. `_nearest_cxn_site()` can snap both ends of an
edge onto the same axis — easily, for decision (diamond) nodes, whose
real Graphviz exit point sits on a slanted edge rather than a bbox-side
midpoint — silently producing this invalid geometry. New
`_enforce_min_bends_for_axis()` (`pptx_shapes.py`) detects this and
promotes to a 2-bend Z-shape instead. Uses a 20px tolerance (not
sub-pixel, unlike `_enforce_no_diagonal`) so ordinary floating-point/
diamond-approximation noise doesn't trigger an unnecessary detour —
only genuinely large mismatches do.

### Fix 3: label-aware default bend placement

When a bend is *forced* without real Graphviz path data to derive its
position from (a 2-point straight path reinterpreted through mismatched
sites), the old code defaulted to a blind 50% midpoint — which could
(and did, in testing) land the connector straight through its own edge
label. New `_default_adj_for_forced_bend()` tries a sequence of
candidate ratios (50%, 25%, 75%, 10%, 90%, then wider ones outside the
direct span for cramped layouts) and keeps the first one that clears
every other node's and every edge label's bounding box, computed once
per graph and passed in as `avoid_boxes`.

### New test coverage: render-level crossing check

Phase 22/23's crossing tests (`TestOrthoEdgePaths`, `TestNoNodeCrossing`)
only verified `layout_graph()`'s computed `edge_path` — never what the
renderer actually wrote to the OOXML file. Since the back-edge bug lived
entirely in the renderer discarding `edge_path`, those tests passed
100% throughout while the bug shipped. New `TestNoRenderedCrossingPptx`/
`TestNoRenderedCrossingDocx` (`tests/test_no_diagonal.py`) close this
gap: they parse the actual generated file, reconstruct the true
polyline from connection-site positions + preset + adj values (exact
for 0/1-bend presets; exact for the "same-axis Z-shape" 2-bend case
`choose_connector_from_path()` actually produces), and assert no
segment crosses any unrelated node or label box — across all 23
topologies, both formats.

### Verification on the reported bug

Reconstructed the exact reported diagram (`a==b`/`a>b`/`a=a-b`/`b=b-a`,
10 nodes) and regenerated both formats. All invariants pass: no
diagonal, all glued, and — new — no rendered crossing. Saved as
`samples/output/phase24_verify.pptx` / `.docx` for visual confirmation.

### Known, separate, NOT-fixed limitation

The new render-crossing test surfaced a distinct, pre-existing issue in
tightly-packed layouts (several decisions/labels close together): a
diamond's real Graphviz exit point can differ enough from its nearest
canonical connection site that the resulting connector's *fixed* segment
(pinned by the site itself — no adj ratio can move it) lands on a third,
unrelated shape, most often a sibling edge's label. Fix 3 only adjusts
the *adjustable* middle segment of a forced bend; it cannot fix a
crossing caused by the fixed outer segments. Properly closing this needs
conflict-aware connection-*site* selection (retry an adjacent site when
the nearest one is blocked) — a larger, separate change from what this
phase delivers. 7 topologies (`three_way_fork` DOCX only, `nested_loops`,
`decision_chain_3`, `loop_with_exit_fork`, `two_loops_sequential`,
`all_shape_types`, `self_loop`) are marked `xfail` in
`TestNoRenderedCrossingPptx`/`Docx` with this reason, rather than
silently deleted or left as unexplained red tests. **The reported bug's
own topology (`collision_graph_bug_report`) is NOT in this list — it
passes cleanly.**

### Files changed

| File | Change |
|------|--------|
| `app/core/render/pptx_shapes.py` | Removed back-edge special case + `_rail_x`/`back_edge_rail_adj`; added `_enforce_min_bends_for_axis`, `_default_adj_for_forced_bend`; `choose_connector_from_path`/`_compute_adj_values` take optional `avoid_boxes` |
| `app/core/render/docx_shapes.py` | Removed back-edge special case + rail imports/padding; precomputes and passes `avoid_boxes` |
| `tests/test_render.py` | Rewrote back-edge tests to assert general glued/no-freeform invariants instead of the old fixed-rail-shape assumptions |
| `tests/test_docx_shapes.py` | Same, for DOCX |
| `tests/test_no_diagonal.py` | New `TestNoRenderedCrossingPptx`/`Docx` (render-level, adj-reconstructing crossing check across all topologies) |
| `docs/PROJECT_CONTEXT.md` | This section |

### Test results

Full non-DB suite: 228 baseline tests unaffected + expanded
`test_no_diagonal.py` (176 tests: 163 passed, 13 xfailed as documented
above, 1 xpassed since `three_way_fork` happens to pass in PPTX even
though it's marked xfail for the DOCX-only failure).

---

## Phase 25 — Image-measured geometry + layout from source positions

**Why**: the vision model reads text and topology well, but (a) the
Graphviz re-flow ignored the original arrangement (`splines=ortho` also
ignores ports), and (b) the model's own coordinates (`box_2d`) are NOT
reliable — on the real samples `gemini-3.1-flash-lite` boxes drifted by
half a symbol or landed on the wrong symbol.

**Pipeline now** (still ONE vision call per image in the normal case):
1. `app/core/diagram_detect.py` (OpenCV, no API): every closed symbol is an
   enclosed white region → exact boxes; straight connector lines are taken
   from ink connected to a symbol outline (titles/watermarks ignored) →
   `guide_segments`. Adaptive threshold; only SOLID dark blocks (letterbox
   bars) are removed — a large thin-line drawing must survive.
2. The provider sends the original image + a copy with numbered red boxes
   ("set-of-mark"); the model returns `mark` per node. `apply_detection`
   swaps in the measured box for every marked node; only when ALL nodes
   are matched one-to-one is the graph flagged `boxes_detected`.
3. `topology_problems()` checks flowchart rules (process/IO: exactly 1
   out-arrow, decision: 2, every non-terminator has an in-arrow). If
   violated (seen live: the model dropped both loop-back arrows), ONE
   corrective call lists the problems; the retry is kept only if better.
4. `app/core/layout_boxes.py`: exact mode = one uniform scale (original
   proportions), small snapping, ports = side midpoints + every point where
   an original line touches a symbol; A* router where the author's lines
   cost 5% (corners on them nearly free), so routes reproduce the source.
   Edges into the same node may merge; two exits from one point are
   effectively forbidden. Labels are placed last on the first free spot
   (no node / line / label collision); labels left of a line are
   right-aligned (`label_align_right`). Approximate mode (model boxes only)
   and Graphviz (no boxes) remain as fallbacks.
5. Renderers: straight segment between two ordinary shapes = glued
   connector; anything else (and anything touching a parallelogram) =
   editable freeform polyline with arrowhead. Parallelogram text rect uses
   `l/t/r/b` guides.

**Validation** (`tests/test_diagram_detect.py`, `tests/test_layout_boxes.py`):
real samples → all symbols detected, zero overlaps by `quality_issues()`.
Offline stress (25 topologies drawn by Graphviz as "scans"): detection
25/25; 21/25 with zero overlaps, 4 with one defect each (Graphviz-style
drawings where arrows leave diamonds mid-edge).

**Dependencies**: `opencv-python-headless`, `numpy==2.2.6` (numpy ≥2.3
needs Python 3.12; CI/Docker use 3.11).
