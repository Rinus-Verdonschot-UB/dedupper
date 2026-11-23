from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


class JobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not job_id.isalnum():
            raise ValueError("Invalid job identifier.")
        return self.root / job_id

    def create(self, job_id: str) -> Path:
        path = self.job_dir(job_id)
        path.mkdir(parents=True, exist_ok=False)
        (path / "uploads").mkdir()
        return path

    def save_json(self, job_id: str, name: str, value: Any) -> Path:
        target = self.job_dir(job_id) / f"{name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, target)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise
        return target

    def load_json(self, job_id: str, name: str, default: Any = None) -> Any:
        target = self.job_dir(job_id) / f"{name}.json"
        if not target.exists():
            return default
        with target.open(encoding="utf-8") as handle:
            return json.load(handle)

    def clean(self, job_id: str) -> None:
        path = self.job_dir(job_id)
        if path.exists():
            shutil.rmtree(path)

    def purge_expired(self, ttl_hours: float, now: float | None = None) -> int:
        """Delete job folders older than *ttl_hours*; return how many were removed.

        Uploaded bibliographic exports are working data. Expired jobs are removed
        whenever a new upload starts, so no scheduler is needed in the container.
        """

        if not ttl_hours or ttl_hours <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - ttl_hours * 3600
        removed = 0
        for path in self.root.iterdir():
            if not path.is_dir() or not path.name.isalnum():
                continue
            try:
                newest = max(
                    [path.stat().st_mtime]
                    + [item.stat().st_mtime for item in path.glob("*.json")]
                )
            except OSError:
                continue
            if newest < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed
