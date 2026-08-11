# Local development commands. Requires GNU make (on Windows: use Git Bash).
# VENV_BIN is "Scripts" on Windows venvs, "bin" on Linux/macOS.
VENV_BIN := $(shell test -d backend/venv/Scripts && echo Scripts || echo bin)
PY := venv/$(VENV_BIN)/python

.PHONY: dev dev-backend dev-frontend up test lint

# Run backend + frontend locally without Docker
dev:
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	cd backend && $(PY) -m uvicorn app.main:app --reload

dev-frontend:
	cd frontend && npm run dev

up:
	docker-compose up

test:
	cd backend && $(PY) -m pytest

lint:
	cd backend && $(PY) -m black --check app tests
	cd backend && $(PY) -m ruff check app tests
	cd backend && $(PY) -m mypy app
