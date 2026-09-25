"""Background jobs with progress tracking.

Deleting 60 items takes minutes: Radarr, qBittorrent and Seerr are called for
each one. Holding the HTTP request open that long makes the browser give up
(seen in practice: BrokenPipeError after a batch of 60). So the work runs in a
thread and the UI polls its progress.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    id: str
    kind: str
    label: str
    total: int
    done: int = 0
    freed: int = 0
    results: list[dict] = field(default_factory=list)
    error: str | None = None
    finished: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.kind,
            "libellé": self.label,
            "total": self.total,
            "faits": self.done,
            "octets_libérés": self.freed,
            "terminé": self.finished,
            "erreur": self.error,
            "durée": round((self.finished_at or time.time()) - self.started_at, 1),
            "résultats": self.results,
        }


class JobRunner:
    """One heavy job at a time: everything goes through the same torrent client."""

    def __init__(self, keep: int = 20, path: Path | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self.keep = keep
        self.path = path
        self._load()

    # --------------------------------------------------------- persistence

    def _load(self) -> None:
        """Reload finished jobs from previous sessions (summaries only)."""
        if not self.path or not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines()[-self.keep:]:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            job = Job(
                id=raw["id"], kind=raw["kind"], label=raw["label"], total=raw["total"],
                done=raw["done"], freed=raw["freed"], error=raw.get("error"),
                finished=True, started_at=raw["started_at"], finished_at=raw["finished_at"],
            )
            job.results = [{"erreurs": ["·"]}] * raw.get("failures", 0)  # just the count
            with self._lock:
                self._jobs[job.id] = job
                self._order.append(job.id)

    def _persist(self, job: Job) -> None:
        if not self.path:
            return
        summary = {
            "id": job.id, "kind": job.kind, "label": job.label, "total": job.total,
            "done": job.done, "freed": job.freed, "error": job.error,
            "started_at": job.started_at, "finished_at": job.finished_at,
            "failures": sum(1 for r in job.results if r.get("erreurs")),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        # cap the file size: keep only the useful tail
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) > self.keep * 5:
            self.path.write_text("\n".join(lines[-self.keep * 2:]) + "\n", encoding="utf-8")

    def current(self) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                if not self._jobs[job_id].finished:
                    return self._jobs[job_id]
        return None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order)]

    def submit(
        self,
        kind: str,
        label: str,
        total: int,
        work: Callable[[Job], None],
    ) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, total=total)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            for stale in self._order[: -self.keep]:
                self._jobs.pop(stale, None)
            self._order = self._order[-self.keep :]

        def run() -> None:
            with self._busy:
                try:
                    work(job)
                except Exception as error:  # noqa: BLE001
                    job.error = f"{type(error).__name__}: {error}"
                finally:
                    job.finished = True
                    job.finished_at = time.time()
                    try:
                        self._persist(job)
                    except OSError:
                        pass

        threading.Thread(target=run, name=f"sweeper-{kind}", daemon=True).start()
        return job
