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
