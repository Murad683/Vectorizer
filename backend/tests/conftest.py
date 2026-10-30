"""Shared fixtures for the backend test suite."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pymupdf
import pytest

from app.config import get_settings
from app.core.auth.store import RefreshTokenStore, UserStore
from app.core.queue.store import JobStore
from app.models.diagram import DiagramEdge, DiagramGraph, DiagramNode, ShapeType

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
BACKEND_DIR = Path(__file__).resolve().parents[1]

# Points at the ephemeral test database: `docker compose --profile test up
# -d postgres-test` locally (see docker-compose.yml), or the `postgres`
# service block in .github/workflows/ci.yml. Not the dev/prod database —
# tests never touch that one.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://test:test@localhost:5433/vectorizer_test",
)


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_database() -> None:
    """Apply Alembic migrations to the test database once per session."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            import warnings

            warnings.warn("Test database not available — DB tests will fail")
    except subprocess.TimeoutExpired:
        import warnings

        warnings.warn("Test database not reachable — DB tests will fail")


@pytest.fixture(autouse=True)
def _no_debug_dumps(monkeypatch: pytest.MonkeyPatch) -> object:
    """Tests never write into the real dump folder.

    The dev container sets DEBUG_DUMP_DIR, so mocked vision calls used to
    leave fake "a < 5" dumps next to the real ones. Tests that exercise the
    dump set their own temporary directory.
    """
    monkeypatch.setenv("DEBUG_DUMP_DIR", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _truncate_all() -> None:
    """Wipe all tables for per-test isolation (cheap vs. a fresh DB)."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE TABLE jobs, users, refresh_tokens, job_submissions "
            "RESTART IDENTITY CASCADE"
        )


@pytest.fixture()
def job_store() -> JobStore:
    """A fresh JobStore against the truncated test database."""
    _truncate_all()
    store = JobStore(dsn=TEST_DATABASE_URL)
    yield store
    store.close()


@pytest.fixture()
def user_store() -> UserStore:
    """A fresh UserStore against the truncated test database."""
    _truncate_all()
    store = UserStore(dsn=TEST_DATABASE_URL)
    yield store
    store.close()


@pytest.fixture()
def refresh_token_store() -> RefreshTokenStore:
    """A fresh RefreshTokenStore against the truncated test database.

    Does NOT call ``_truncate_all`` itself — tests that need this
    alongside ``job_store``/``user_store`` should depend on whichever of
    those already truncates, to avoid wiping data set up by an earlier
    fixture in the same test.
    """
    store = RefreshTokenStore(dsn=TEST_DATABASE_URL)
    yield store
    store.close()


@pytest.fixture(scope="session")
def flowchart_graph() -> DiagramGraph:
    """The real flowchart sample's known structure (question 80).

    8 nodes, one decision with two branches, one back-edge
    (a = a + 16 loops back to the decision).
    """
    return DiagramGraph(
        nodes=[
            DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Başlanğıc"),
            DiagramNode(id="n2", shape_type=ShapeType.INPUT_OUTPUT, text="n"),
            DiagramNode(id="n3", shape_type=ShapeType.PROCESS, text="a = 3"),
            DiagramNode(id="n4", shape_type=ShapeType.DECISION, text="a < 164"),
            DiagramNode(id="n5", shape_type=ShapeType.INPUT_OUTPUT, text="n"),
            DiagramNode(id="n6", shape_type=ShapeType.TERMINATOR, text="Son"),
            DiagramNode(id="n7", shape_type=ShapeType.PROCESS, text="n = n - 8"),
            DiagramNode(id="n8", shape_type=ShapeType.PROCESS, text="a = a + 16"),
        ],
        edges=[
            DiagramEdge(source_id="n1", target_id="n2"),
            DiagramEdge(source_id="n2", target_id="n3"),
            DiagramEdge(source_id="n3", target_id="n4"),
            DiagramEdge(source_id="n4", target_id="n5", label="Yox"),
            DiagramEdge(source_id="n5", target_id="n6"),
            DiagramEdge(source_id="n4", target_id="n7", label="Hə"),
            DiagramEdge(source_id="n7", target_id="n8"),
            DiagramEdge(source_id="n8", target_id="n4"),
        ],
    )


@pytest.fixture(scope="session")
def collision_graph() -> DiagramGraph:
    """Synthetic topology reproducing the Phase 17 bug report.

    Two nested decisions where one branch ("a==b" -> "d=c/a" -> "d") is
    the same depth as the OTHER decision's two branches ("a=a-b",
    "b=b-a") — all three land in the same layout row, with "d" placed
    between the other two. Both "a=a-b" and "b=b-a" loop back to the
    outer decision: the left one's back-edge must reach past "d" and
    "b=b-a" to get to the rail, which is exactly the case the simple
    right-rail route couldn't handle without crossing "d"'s box.
    """
    return DiagramGraph(
        nodes=[
            DiagramNode(id="n1", shape_type=ShapeType.TERMINATOR, text="Başlanğıc"),
            DiagramNode(id="n2", shape_type=ShapeType.DECISION, text="a==b"),
            DiagramNode(id="n4", shape_type=ShapeType.DECISION, text="a>b"),
            DiagramNode(id="n3a", shape_type=ShapeType.PROCESS, text="d=c/a"),
            DiagramNode(id="n3", shape_type=ShapeType.INPUT_OUTPUT, text="d"),
            DiagramNode(id="n5", shape_type=ShapeType.PROCESS, text="a=a-b"),
            DiagramNode(id="n6", shape_type=ShapeType.PROCESS, text="b=b-a"),
        ],
        edges=[
            DiagramEdge(source_id="n1", target_id="n2"),
            DiagramEdge(source_id="n2", target_id="n4", label="Yox"),
            DiagramEdge(source_id="n2", target_id="n3a", label="Hə"),
            DiagramEdge(source_id="n3a", target_id="n3"),
            DiagramEdge(source_id="n4", target_id="n5", label="Hə"),
            DiagramEdge(source_id="n4", target_id="n6", label="Yox"),
            DiagramEdge(source_id="n5", target_id="n2"),
            DiagramEdge(source_id="n6", target_id="n2"),
        ],
    )


@pytest.fixture(scope="session")
def sample_docx() -> Path:
    """The real Azerbaijani informatics test DOCX from samples/."""
    return SAMPLES_DIR / "informatika_seh_40_41.docx"


@pytest.fixture(scope="session")
def flowchart_png_bytes() -> bytes:
    """Raw bytes of the real flowchart sample image."""
    return (SAMPLES_DIR / "flowchart_a3.png").read_bytes()


@pytest.fixture(scope="session")
def table87_png_bytes() -> bytes:
    """Raw bytes of the real question-87 matching-question image."""
    return (SAMPLES_DIR / "table_87.png").read_bytes()


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PDF built from the real sample assets.

    No PDF sample was provided in samples/, so this fixture composes one
    from the real flowchart PNG and real question text, using PyMuPDF.
    """
    pdf_path = tmp_path_factory.mktemp("fixtures") / "sample_flowchart.pdf"
    question = (
        "80. Alqoritmin blok-sxeminə əsasən n dəyişəninin çapa veriləcək "
        "qiymətini müəyyən edin."
    )
    answers = "A) 320        B) 147        C) 328        D) 312        E) 171"
    doc = pymupdf.open()
    page = doc.new_page()
    # insert_htmlbox handles the Azerbaijani characters (ə, ı) that the
    # default PDF base fonts cannot encode.
    page.insert_htmlbox(pymupdf.Rect(50, 40, 550, 95), question)
    page.insert_image(
        pymupdf.Rect(150, 100, 450, 400),
        filename=str(SAMPLES_DIR / "flowchart_a3.png"),
    )
    page.insert_htmlbox(pymupdf.Rect(50, 420, 550, 465), answers)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path
