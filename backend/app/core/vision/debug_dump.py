"""Optional on-disk record of every vision call (dev aid, off by default).

Enabled by ``DEBUG_DUMP_DIR``. Each call gets its own folder::

    <DEBUG_DUMP_DIR>/<yyyymmdd-HHMMSS>-<sha1 of image>/
        image.png|jpg     the exact bytes sent to the model
        marked.png        the set-of-mark image (if one was sent)
        prompt.txt        the full prompt text
        response.json     raw text of the accepted first answer
        retry.json        raw text of the corrective retry (if any)
        detection.json    measured shapes + guide segments
        graph.json        the final DiagramGraph handed to the layout
        meta.json         model name, topology problems, which answer won

``scripts/replay_diagram.py`` re-runs detection → layout → render from such
a folder, so layout work needs no further API calls. Failures to write are
logged and never affect the pipeline.
"""

import dataclasses
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.diagram_detect import Detection

logger = logging.getLogger(__name__)


def _image_ext(image: bytes) -> str:
    return "jpg" if image[:3] == b"\xff\xd8\xff" else "png"


class DebugDump:
    """Writes the artefacts of one vision call; a no-op when disabled."""

    def __init__(self, image: bytes, base_dir: str | None = None) -> None:
        root = base_dir if base_dir is not None else get_settings().debug_dump_dir
        self.folder: Path | None = None
        if not root:
            return
        digest = hashlib.sha1(image).hexdigest()[:8]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = Path(root) / f"{stamp}-{digest}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"image.{_image_ext(image)}").write_bytes(image)
        except OSError:
            logger.exception("Debug dump disabled: cannot write to %s", folder)
            return
        self.folder = folder
        logger.info("Debug dump: %s", folder)

    @property
    def enabled(self) -> bool:
        """True when artefacts are being written."""
        return self.folder is not None

    def binary(self, name: str, data: bytes) -> None:
        """Save a binary artefact."""
        self._write(name, data)

    def text(self, name: str, data: str) -> None:
        """Save a text artefact (UTF-8)."""
        self._write(name, data.encode("utf-8"))

    def json(self, name: str, data: Any) -> None:
        """Save a JSON-serialisable artefact, pretty-printed."""
        self.text(name, json.dumps(data, ensure_ascii=False, indent=2))

    def detection(self, detection: Detection | None) -> None:
        """Save the measured geometry."""
        if detection is not None:
            self.json("detection.json", dataclasses.asdict(detection))

    def _write(self, name: str, data: bytes) -> None:
        if self.folder is None:
            return
        try:
            (self.folder / name).write_bytes(data)
        except OSError:
            logger.exception("Debug dump: could not write %s", name)
