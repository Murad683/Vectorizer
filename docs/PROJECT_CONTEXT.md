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

### Notes for Phase 3+

- `google-generativeai` (0.8.6) is Google's deprecated legacy SDK (EOL
  notice on import). Consider migrating `gemini_provider.py` to the
  `google-genai` SDK — the provider isolation means nothing else changes.
- Test fixtures use the REAL samples in `samples/` (flowchart PNG appears
  in the DOCX at question 80). There is no real PDF sample; the PDF test
  builds one on the fly from the real assets (see `tests/conftest.py`).
- The Gemini prompt in `gemini_provider.py` is a first working version —
  prompt refinement and Mermaid render-validation are Phase 3 work.
