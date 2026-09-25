"""Who requests what, and how much it weighs.

Retention rules treat the symptom: bytes piling up. The cause is upstream —
around sixty requests a month, made by a handful of accounts, some of which
nobody ever watches.

This module links each Seerr request to the media it produced, then to its
Plex watch history. It doesn't judge anyone: it shows the volume and what
became of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

DAY = 86400


@dataclass
class Requester:
    name: str
    requests: int = 0
    recent_requests: int = 0        # over the last 90 days
    matched: int = 0                # requests found in the library
    bytes: int = 0
    watched_bytes: int = 0
    unwatched_bytes: int = 0
    unwatched_items: int = 0
    last_request: int = 0
    titles: list[str] = field(default_factory=list)

    @property
    def waste_ratio(self) -> float:
        return self.unwatched_bytes / self.bytes if self.bytes else 0.0

    def as_json(self) -> dict:
        return {
            "nom": self.name,
            "demandes": self.requests,
            "demandes_90j": self.recent_requests,
            "retrouvées": self.matched,
            "octets": self.bytes,
            "octets_vus": self.watched_bytes,
            "octets_jamais_vus": self.unwatched_bytes,
            "éléments_jamais_vus": self.unwatched_items,
            "part_jamais_vue": round(self.waste_ratio, 3),
            "dernière_demande": self.last_request,
            "exemples": self.titles[:6],
        }


def collect(seerr_inventory: dict, inventory: list) -> dict:
    """Aggregate per requester, linking request -> media -> viewing."""
    # one media item can carry several requests (seasons): index by Seerr id
    by_media: dict[int, list] = {}
    for media in seerr_inventory.values():
        if media.requests:
            by_media[media.id] = media.requests

    # on the library side, lookup is by external ID
    movies = {c.external_id: c for c in inventory if c.kind == "movie" and c.external_id}
    shows = {c.external_id: c for c in inventory if c.kind == "series" and c.external_id}
    by_rating_key = {c.plex_id: c for c in inventory if c.kind in ("movie", "series") and c.plex_id}

    people: dict[str, Requester] = {}
    by_item: dict[str, set[str]] = {}       # media key -> who requested it
    now = time.time()
    unmatched = 0
    counted: set[tuple[str, str]] = set()   # (requester, media key): no duplicates

    for media in seerr_inventory.values():
        candidate = None
        if media.media_type == "movie" and media.tmdb_id in movies:
            candidate = movies[media.tmdb_id]
        elif media.media_type == "tv" and media.tvdb_id in shows:
            candidate = shows[media.tvdb_id]
        elif media.rating_key in by_rating_key:
            candidate = by_rating_key[media.rating_key]   # outside the *arr, or no external ID

        for request in media.requests:
            person = people.setdefault(
                request.requested_by, Requester(name=request.requested_by)
            )
            person.requests += 1
            person.last_request = max(person.last_request, request.created_at)
            if request.created_at and (now - request.created_at) / DAY <= 90:
                person.recent_requests += 1
            if candidate is None:
                unmatched += 1
                continue
            by_item.setdefault(candidate.key, set()).add(person.name)
            key = (person.name, candidate.key)
            if key in counted:
                continue          # a series requested season by season counts only once
            counted.add(key)
            person.matched += 1
            person.bytes += candidate.library_bytes
            if candidate.stats.last_viewed:
                person.watched_bytes += candidate.library_bytes
            else:
                person.unwatched_bytes += candidate.library_bytes
                person.unwatched_items += 1
                if len(person.titles) < 6:
                    person.titles.append(candidate.title)

    ranked = sorted(people.values(), key=lambda p: p.bytes, reverse=True)
    return {
        "par_média": {key: sorted(names) for key, names in by_item.items()},
        "demandeurs": [p.as_json() for p in ranked],
        "totaux": {
            "demandes": sum(p.requests for p in ranked),
            "demandeurs": len(ranked),
            "octets": sum(p.bytes for p in ranked),
            "octets_jamais_vus": sum(p.unwatched_bytes for p in ranked),
            "non_retrouvées": unmatched,
        },
    }
