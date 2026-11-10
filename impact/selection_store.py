"""Local, atomic persistence for explicitly applied repository scope."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .client import repository_selection


class SelectionStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path.home() / ".local/state/github-impact-assessment/repositories.json"

    def load(self, limit: int) -> tuple[str, ...] | None:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(49153)
        except FileNotFoundError:
            return ()
        if len(raw) > 49152:
            raise ValueError("Saved repository scope exceeds size limit")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"mode", "repositories"}:
            raise ValueError("Invalid saved repository scope")
        mode, names = data["mode"], data["repositories"]
        if not isinstance(mode, str) or not isinstance(names, list):
            raise ValueError("Invalid saved repository scope")
        if mode == "selected":
            return repository_selection(names, limit)
        if mode not in {"paused", "all"} or names:
            raise ValueError("Invalid saved repository scope")
        return None if mode == "all" else ()

    def save(self, mode: str, repositories: list[str]) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".repositories-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"mode": mode, "repositories": repositories}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
