"""REAL vision check: one Gemini call per image (+1 corrective retry only
when the answer breaks a flowchart rule), then the offline replay.

COSTS MONEY — run only when approved. Needs GEMINI_API_KEY and writes the
debug dumps under DEBUG_DUMP_DIR (default here: data/debug_dumps).

Usage (from backend/, inside the backend container):
    python scripts/live_check.py ../samples/quadratic_equation.png ...
    python scripts/live_check.py --all-samples      # the 4 Word-test originals
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DEBUG_DUMP_DIR", "data/debug_dumps")

from app.config import get_settings  # noqa: E402
from app.core.vision import get_vision_provider  # noqa: E402
from scripts.replay_diagram import _from_dump, replay  # noqa: E402

_SAMPLES = [
    "quadratic_equation.png",
    "fahrenheit_celsius.png",
    "sp_open_exit.png",
    "sum_loop.jpg",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("images", nargs="*")
    parser.add_argument("--all-samples", action="store_true")
    args = parser.parse_args()
    samples = Path(__file__).resolve().parents[2] / "samples"
    paths = [Path(p) for p in args.images]
    if args.all_samples:
        paths += [samples / name for name in _SAMPLES]
    if not paths:
        parser.error("no images given")

    get_settings.cache_clear()
    root = Path(get_settings().debug_dump_dir)
    provider = get_vision_provider()
    for path in paths:
        before = set(root.glob("*")) if root.exists() else set()
        result = provider.analyze_diagram(path.read_bytes())
        if result.error:
            print(f"== {path.name}: ERROR {result.error}")
            continue
        new = sorted(set(root.glob("*")) - before)
        if not new:
            print(f"== {path.name}: no dump written")
            continue
        folder = new[-1]
        print(f"   dump: {folder}")
        image, graph, seen = _from_dump(folder)
        replay(path.stem, image, graph, seen, folder / "replay")


if __name__ == "__main__":
    main()
