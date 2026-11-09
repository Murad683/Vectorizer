"""Print what the automatic gap check found on finished jobs lately.

Read-only (one SELECT on ``job_quality``)::

    python -m app.quality_report [--days N]

On the server, inside the backend container::

    docker compose -p vectorizer -f docker-compose.prod.yml exec backend \\
        python -m app.quality_report --days 7

Every finished job that sent pages to the model is looked at once: ink on the
page that nothing in the answer explains, empty option letters, text missing
from the Word file, and so on (``app/eval/gaps.py``). The numbers are an
estimate of what was lost, not a verdict: open the saved answer
(``data/answers/<job id>.json``, kept for ``INPUT_RETENTION_HOURS``) and the
input to see what really happened.
"""

import argparse
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.queue.store import JobStore

_TOP = 10
ANSWERS_DIR = Path("data/answers")
_HEADER = (
    f"{'gün':<11}{'iş':>5}{'səhifə':>8}{'xətalı':>8}{'%':>7}{'xəta':>7}{'baxın':>7}"
)


def _num(value: Any) -> int:
    return int(value or 0)


def _day_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    days: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        day = days[row["created_at"].strftime("%Y-%m-%d")]
        day["jobs"] += 1
        day["pages"] += _num(row["pages_checked"])
        day["bad_jobs"] += 1 if _num(row["errors"]) else 0
        day["errors"] += _num(row["errors"])
        day["reviews"] += _num(row["reviews"])
    return days


def _line(label: str, t: Mapping[str, int]) -> str:
    share = 100 * t["bad_jobs"] / t["jobs"] if t["jobs"] else 0.0
    return (
        f"{label:<11}{t['jobs']:>5}{t['pages']:>8}{t['bad_jobs']:>8}"
        f"{share:>7.0f}{t['errors']:>7}{t['reviews']:>7}"
    )


def build_report(
    rows: Sequence[Mapping[str, Any]],
    days: int,
    answers_dir: Path | None = ANSWERS_DIR,
) -> str:
    """Format the quality rows as plain text (pure, so it can be tested)."""
    if not rows:
        return f"Son {days} gün üçün itki yoxlaması məlumatı yoxdur."

    lines = [f"Son {days} gün: işlərin avtomatik itki yoxlaması", ""]
    lines.append(_HEADER)
    total: dict[str, int] = defaultdict(int)
    for day, t in sorted(_day_totals(rows).items()):
        lines.append(_line(day, t))
        for key, value in t.items():
            total[key] += value
    lines.append(_line("cəmi", total))
    lines.append("(xətalı = ən azı bir xətası olan iş; % = onların işlərə payı)")

    kinds: dict[str, int] = defaultdict(int)
    for row in rows:
        for kind, count in (row["kinds"] or {}).items():
            kinds[kind] += int(count)
    lines += ["", "Xəta növləri (sayına görə):"]
    if kinds:
        lines.extend(
            f"  {kind:<22}{count:>5}"
            for kind, count in sorted(kinds.items(), key=lambda item: -item[1])
        )
    else:
        lines.append("  xəta yoxdur")

    worst = sorted(rows, key=lambda r: _num(r["errors"]), reverse=True)
    worst = [r for r in worst if _num(r["errors"])][:_TOP]
    if worst:
        lines += ["", f"Ən çox xətası olan {len(worst)} iş:"]
        for row in worst:
            saved = (
                answers_dir is not None
                and (answers_dir / f"{row['job_id']}.json").exists()
            )
            lines.append(
                f"  {row['created_at']:%Y-%m-%d %H:%M}  iş {str(row['job_id'])[:8]}  "
                f"xəta={_num(row['errors'])} baxın={_num(row['reviews'])} "
                f"səhifə={_num(row['pages_checked'])}"
                + ("  cavab faylı var" if saved else "")
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7, help="how many days back")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    store = JobStore(dsn=get_settings().database_url)
    try:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        rows = store.quality_rows_since(since)
    finally:
        store.close()
    print(build_report(rows, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
