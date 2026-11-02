"""Print what the service used lately: jobs, pages, Gemini requests, tokens, cost.

Read-only (one SELECT on ``job_usage``)::

    python -m app.usage_report [--days N] [--usd-azn RATE]

On the server, inside the backend container::

    docker compose -p vectorizer -f docker-compose.prod.yml exec backend \\
        python -m app.usage_report --days 7

The cost is an estimate from the token counts and the list prices in the
settings, not a bill: compare it with the Google billing page.
"""

import argparse
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.core.queue.store import JobStore

_TOP = 10
_HEADER = (
    f"{'gün':<11}{'sətir':>6}{'səhifə':>8}{'sorğu':>7}"
    f"{'giriş tok.':>12}{'çıxış tok.':>12}{'USD':>9}{'AZN':>8}"
)


def _num(value: Any) -> float:
    return float(value or 0)


def _day_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    days: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        day = days[row["created_at"].strftime("%Y-%m-%d")]
        day["rows"] += 1
        day["pages"] += _num(row["pages"])
        day["calls"] += _num(row["vision_calls"])
        day["in"] += _num(row["input_tokens"])
        day["out"] += _num(row["output_tokens"]) + _num(row["thought_tokens"])
        day["usd"] += _num(row["est_cost_usd"])
    return days


def _table_line(label: str, t: Mapping[str, float], usd_azn: float) -> str:
    return (
        f"{label:<11}{t['rows']:>6.0f}{t['pages']:>8.0f}{t['calls']:>7.0f}"
        f"{t['in']:>12.0f}{t['out']:>12.0f}{t['usd']:>9.4f}"
        f"{t['usd'] * usd_azn:>8.3f}"
    )


def _top_line(row: Mapping[str, Any]) -> str:
    pages = row["pages"] if row["pages"] is not None else "-"
    return (
        f"  {row['created_at']:%Y-%m-%d %H:%M}  "
        f"{_num(row['est_cost_usd']):.4f} USD  {row['file_ext']:<5} "
        f"səhifə={pages} sorğu={row['vision_calls']} "
        f"{_num(row['duration_s']):.0f}s  {row['outcome']}"
    )


def build_report(rows: Sequence[Mapping[str, Any]], days: int, usd_azn: float) -> str:
    """Format the usage rows as plain text (pure, so it can be tested)."""
    if not rows:
        return f"Son {days} gün üçün məlumat yoxdur."

    lines = [f"Son {days} gün (təxmini xərc, Google billing ilə tutuşdurun)", ""]
    lines.append(_HEADER)
    total: dict[str, float] = defaultdict(float)
    for day, t in sorted(_day_totals(rows).items()):
        lines.append(_table_line(day, t, usd_azn))
        for key, value in t.items():
            total[key] += value
    lines.append(_table_line("cəmi", total, usd_azn))

    lines.append("")
    if total["pages"]:
        per_page = total["usd"] / total["pages"]
        lines.append(
            f"Səhifə başına orta: {per_page:.4f} USD ({per_page * usd_azn:.3f} AZN), "
            f"səhifə başına {total['calls'] / total['pages']:.2f} sorğu"
        )
    outcomes: dict[str, int] = defaultdict(int)
    for row in rows:
        outcomes[row["outcome"]] += 1
    lines.append(
        "Nəticələr: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
    )

    lines += ["", f"Ən bahalı {_TOP} sətir:"]
    top = sorted(rows, key=lambda r: _num(r["est_cost_usd"]), reverse=True)[:_TOP]
    lines.extend(_top_line(row) for row in top)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7, help="how many days back")
    parser.add_argument(
        "--usd-azn", type=float, default=1.70, help="USD to AZN rate for the AZN column"
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    store = JobStore(dsn=get_settings().database_url)
    try:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        rows = store.usage_rows_since(since)
    finally:
        store.close()
    print(build_report(rows, args.days, args.usd_azn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
