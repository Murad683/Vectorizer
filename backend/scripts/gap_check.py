"""Find what the conversion loses: record the model's answers, then check them.

    python scripts/gap_check.py synth   <corpus> [--photo] [--fake-runs]
    python scripts/gap_check.py capture <corpus> --runs 3 [--only NAME] --yes
    python scripts/gap_check.py check   <corpus> [--only NAME]

``<corpus>`` is a folder OUTSIDE the repository (customer pages must never be
committed). One sub-folder per sample::

    <corpus>/<sample>/source.png|jpg|jpeg|pdf    the page(s)
    <corpus>/<sample>/expected.txt               optional: the text it must give back
    <corpus>/<sample>/runs/run1/results.json     recorded model answers (capture)
    <corpus>/reports/<time>/index.html           the report (check)

``capture`` sends requests to Gemini and costs money: it prints how many it will
send and does nothing without ``--yes``. ``check`` is offline and free: it plays
the recorded answers back through the real pipeline and looks for gaps.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.core.vision import (
    VisionBudget,
    gemini_provider,
    get_vision_provider,
)  # noqa: E402
from app.eval.gaps import (
    Gap,  # noqa: E402
    check_assembly,
    check_expected,
    docx_text,
    find_gaps,
)  # noqa: E402
from app.eval.replay import (  # noqa: E402
    RecordingProvider,
    ReplayProvider,
    load_results,
    page_images,
    page_of,
    result_key,
    run_pipeline,
    save_results,
)
from app.eval.report import Row, build_report  # noqa: E402
from app.eval.synth import all_pages, photo_effect  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
_SOURCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
# A page costs about one request, two when the flowchart rescue also runs.
_COST_LOW, _COST_HIGH = 0.002, 0.004


def ensure_outside_repo(corpus: Path) -> Path:
    """Refuse a corpus folder inside the repository."""
    resolved = corpus.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise SystemExit(
            f"{resolved} is inside the repository. Keep test pages outside it, "
            "customer content must never be committed."
        )
    return resolved


def samples(corpus: Path, only: str | None) -> list[tuple[str, Path]]:
    """(name, source file) of every sample folder."""
    found = []
    for folder in sorted(
        p for p in corpus.iterdir() if p.is_dir() and p.name != "reports"
    ):
        if only and folder.name != only:
            continue
        sources = [
            f
            for f in folder.iterdir()
            if f.stem == "source" and f.suffix.lower() in _SOURCE_SUFFIXES
        ]
        if sources:
            found.append((folder.name, sources[0]))
    return found


def run_dirs(sample_dir: Path) -> list[Path]:
    runs = sample_dir / "runs"
    return sorted(p for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else []


def cmd_synth(args: argparse.Namespace) -> int:
    corpus = ensure_outside_repo(Path(args.corpus))
    for page in all_pages():
        folder = corpus / page.name
        folder.mkdir(parents=True, exist_ok=True)
        png = photo_effect(page.png) if args.photo else page.png
        source = folder / "source.png"
        source.write_bytes(png)
        (folder / "expected.txt").write_text(page.expected, encoding="utf-8")
        if args.fake_runs:
            key = result_key(png)
            for name, answer in (("run1", page.good), ("run2", page.broken)):
                from app.models.document import ContentType, VisionResult

                result = VisionResult(content_type=ContentType.PAGE, page=answer)
                save_results(folder / "runs" / name / "results.json", {key: result})
        print(f"wrote {folder}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    corpus = ensure_outside_repo(Path(args.corpus))
    plan = []
    for name, source in samples(corpus, args.only):
        pages = len(page_images(source))
        todo = [
            k
            for k in range(1, args.runs + 1)
            if args.force
            or not (corpus / name / "runs" / f"run{k}" / "results.json").exists()
        ]
        if todo:
            plan.append((name, source, pages, todo))
    requests = sum(pages * len(todo) for _, _, pages, todo in plan)
    for name, _, pages, todo in plan:
        print(f"  {name}: {pages} page(s) x {len(todo)} run(s)")
    print(
        f"{requests} page request(s) to Gemini, about "
        f"${requests * _COST_LOW:.2f} - ${requests * _COST_HIGH:.2f}"
        + (
            " (no flowchart rescue)"
            if args.no_rescue
            else " (flowchart rescue may add some)"
        )
    )
    if not requests:
        print("nothing to capture")
        return 0
    if not args.yes:
        print("Not sent. Run again with --yes to send them.")
        return 2
    original = gemini_provider._RESCUE_MIN_SHAPES  # noqa: SLF001
    if args.no_rescue:
        gemini_provider._RESCUE_MIN_SHAPES = (
            10**6
        )  # noqa: SLF001 - one request per page
    try:
        record_runs(corpus, plan)
    finally:  # never leave the switch changed for whatever runs next
        gemini_provider._RESCUE_MIN_SHAPES = original  # noqa: SLF001
    return 0


def record_runs(corpus: Path, plan: list[tuple[str, Path, int, list[int]]]) -> None:
    """Send the planned pages to the model and save every answer and its usage."""
    settings = get_settings()
    for name, source, pages, todo in plan:
        for k in todo:
            budget = VisionBudget(limit=max(25, pages * 2 + 5))
            recorder = RecordingProvider(get_vision_provider(budget))
            run_dir = corpus / name / "runs" / f"run{k}"
            run_pipeline(source, recorder, run_dir)
            save_results(run_dir / "results.json", recorder.results)
            cost = (
                budget.input_tokens * settings.gemini_price_in_per_m
                + (budget.output_tokens + budget.thought_tokens)
                * settings.gemini_price_out_per_m
            ) / 1_000_000
            (run_dir / "usage.json").write_text(
                json.dumps(
                    {
                        "requests": budget.used,
                        "input_tokens": budget.input_tokens,
                        "output_tokens": budget.output_tokens,
                        "est_cost_usd": round(cost, 4),
                    }
                ),
                encoding="utf-8",
            )
            print(f"  {name} run{k}: {budget.used} request(s), ~${cost:.4f}")


def cmd_check(args: argparse.Namespace) -> int:
    corpus = ensure_outside_repo(Path(args.corpus))
    rows: list[Row] = []
    for name, source in samples(corpus, args.only):
        images = page_images(source)
        expected_file = source.parent / "expected.txt"
        expected = (
            expected_file.read_text(encoding="utf-8")
            if expected_file.exists()
            else None
        )
        for run_dir in run_dirs(source.parent):
            results_file = run_dir / "results.json"
            if not results_file.exists():
                continue
            results = load_results(results_file, images)
            docx_path, _ = run_pipeline(
                source, ReplayProvider(results), run_dir / "replay"
            )
            output = docx_text(docx_path)
            for number, image in enumerate(images, start=1):
                page = page_of(results.get(result_key(image)))
                gaps: list[Gap] = []
                note = ""
                if page is None:
                    note = "no page answer was recorded"
                else:
                    gaps = find_gaps(image, page) + check_assembly(
                        page, docx_path, image
                    )
                if number == 1 and expected is not None:
                    missing, ratio = check_expected(expected, output)
                    gaps += missing
                    note = (note + f" text similarity {ratio * 100:.0f}%").strip()
                rows.append(Row(name, run_dir.name, number, image, page, gaps, note))
    if not rows:
        print("no recorded runs found; use capture (or synth --fake-runs) first")
        return 1
    out = corpus / "reports" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(build_report(rows), encoding="utf-8")
    print(f"{'sample':28s} {'run':6s} page  errors  review")
    for r in rows:
        print(
            f"{r.sample:28s} {r.run:6s} {r.page_number:4d}  "
            f"{r.errors:6d}  {r.reviews:6d}  {r.note}"
        )
    print(f"report: {out / 'index.html'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    synth = sub.add_parser("synth", help="write made-up test pages")
    synth.add_argument("corpus")
    synth.add_argument(
        "--photo", action="store_true", help="make them look like phone photos"
    )
    synth.add_argument(
        "--fake-runs", action="store_true", help="also write a good and a broken answer"
    )
    capture = sub.add_parser(
        "capture", help="send pages to Gemini and record the answers (costs money)"
    )
    capture.add_argument("corpus")
    capture.add_argument("--runs", type=int, default=3)
    capture.add_argument("--only")
    capture.add_argument("--force", action="store_true", help="record runs again")
    capture.add_argument(
        "--no-rescue", action="store_true", help="no flowchart rescue request"
    )
    capture.add_argument("--yes", action="store_true", help="really send the requests")
    check = sub.add_parser(
        "check", help="look for gaps in the recorded answers (offline, free)"
    )
    check.add_argument("corpus")
    check.add_argument("--only")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return {"synth": cmd_synth, "capture": cmd_capture, "check": cmd_check}[
        args.command
    ](args)


if __name__ == "__main__":
    raise SystemExit(main())
