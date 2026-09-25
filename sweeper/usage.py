"""Daily disk usage log.

A single figure says little. Without a time series there is no way to tell
whether cleanup is gaining ground or growth is catching up with it, and
nothing keeps that history: not Plex, not the *arr.

One sample per day is enough — the day's last sample overwrites the previous one.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path


def record(path: Path, quota: dict, composition: dict, orphan_bytes: int) -> None:
    samples = load(path)
    today = date.today().isoformat()
    samples = [s for s in samples if s.get("jour") != today]
    samples.append(
        {
            "jour": today,
            "horodatage": int(time.time()),
            "occupé": round(quota.get("used_gb", 0) * 1e9),
            "plan": round(quota.get("plan_gb", 0) * 1e9),
            "bibliothèque": composition.get("vue", 0) + composition.get("jamais_vue", 0),
            "jamais_vue": composition.get("jamais_vue", 0),
            "seed_seul": composition.get("seed_seul", 0),
            "hors_bibliothèque": orphan_bytes,
        }
    )
    samples = samples[-400:]          # a little over a year
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return sorted(samples, key=lambda s: s.get("jour", ""))


def trend(samples: list[dict], days: int = 14) -> dict:
    """Recent slope, in bytes per day. Needs at least two samples."""
    if len(samples) < 2:
        return {"par_jour": 0, "fiable": False, "relevés": len(samples)}
    window = samples[-days:] if len(samples) > days else samples
    first, last = window[0], window[-1]
    span = max((last["horodatage"] - first["horodatage"]) / 86400, 1)
    return {
        "par_jour": round((last["occupé"] - first["occupé"]) / span),
        "fiable": len(window) >= 3 and span >= 2,
        "relevés": len(samples),
        "jours": round(span, 1),
    }
