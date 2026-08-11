# Test Material Vectorizer

![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)

Converts pixelated PDF/DOCX test materials (Azerbaijani educational content —
algorithm flowcharts, tables, questions) into clean vector-quality
Word/PowerPoint files using a vision LLM to reconstruct diagrams.

> Current status: **Phase 1 — project scaffolding.** See
> [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) for architecture decisions
> and conventions.

## Prerequisites

- Python 3.10+
- Node.js 20+
- Docker + Docker Compose (optional, for containerized dev)
- GNU make (optional; on Windows use Git Bash, or run the commands directly)

## Backend setup

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate    Linux/macOS: source venv/bin/activate
venv/Scripts/activate || source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in values as needed
uvicorn app.main:app --reload
```

- Health check: http://localhost:8000/health
- OpenAPI docs: http://localhost:8000/docs

## Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Dev server: http://localhost:5173

## Docker (both services)

```bash
cp backend/.env.example backend/.env
make up          # or: docker-compose up
```

## Development commands

| Command     | Description                                    |
| ----------- | ---------------------------------------------- |
| `make dev`  | Run backend + frontend locally without Docker  |
| `make up`   | `docker-compose up`                            |
| `make test` | Run the backend pytest suite                   |
| `make lint` | `black --check`, `ruff`, `mypy` on the backend |

## Pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```
