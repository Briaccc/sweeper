"""Deferred queue: "delete it as soon as the seed obligation is met".

Almost everything worth deleting is held back for a few days or weeks by
tracker seeding requirements. Without memory, you would have to come back every
day to catch whatever just became free. So the intent is recorded once, and the
scheduler carries it out when it becomes allowed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Entry:
    key: str
    kind: str  # "media" | "orphan"
    title: str
    added_at: int
    bytes: int


class Watchlist:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, Entry] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self.entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.entries = {}
            return
        self.entries = {
            key: Entry(
                key=key,
                kind=value.get("kind", "media"),
                title=value.get("title", key),
                added_at=int(value.get("added_at", 0)),
                bytes=int(value.get("bytes", 0)),
            )
            for key, value in raw.items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "kind": entry.kind,
                "title": entry.title,
                "added_at": entry.added_at,
                "bytes": entry.bytes,
            }
            for key, entry in self.entries.items()
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, key: str, kind: str, title: str, size: int) -> None:
        if key not in self.entries:
            self.entries[key] = Entry(key, kind, title, int(time.time()), size)

    def remove(self, key: str) -> None:
        self.entries.pop(key, None)

    def prune(self, valid_keys: set[str]) -> list[Entry]:
        """Remove entries whose key no longer exists.

        A title deleted elsewhere, or a non-*arr item that got adopted (its key
        changes from `plex:…` to `movie:<id>`), would otherwise leave a "gone"
        row forever. Returns what was removed, for the log.
        """
        gone = [e for k, e in self.entries.items() if k not in valid_keys]
        for entry in gone:
            del self.entries[entry.key]
        if gone:
            self.save()
        return gone

    def keys(self, kind: str) -> set[str]:
        return {k for k, e in self.entries.items() if e.kind == kind}

    def as_json(self) -> list[dict]:
        return [
            {
                "key": e.key,
                "kind": e.kind,
                "titre": e.title,
                "depuis": e.added_at,
                "octets": e.bytes,
            }
            for e in sorted(self.entries.values(), key=lambda e: -e.bytes)
        ]
