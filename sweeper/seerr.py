"""Seerr / Jellyseerr / Overseerr client (API v1).

This is the piece Maintainerr lacks: after a media item is deleted, we also
remove its request(s), then the media entry — which makes the title
requestable again for users instead of leaving it marked "Available".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

from .discovery import ServiceAccess

PAGE_SIZE = 100


@dataclass
class SeerrRequest:
    id: int
    media_id: int
    media_type: str
    created_at: int
    seasons: list[int]
    requested_by: str
    status: int


@dataclass
class SeerrMedia:
    id: int
    media_type: str
    tmdb_id: int | None
    tvdb_id: int | None
    external_service_id: int | None
    status: int
    rating_key: int | None = None    # Plex identifier: links items no *arr manages
    requests: list[SeerrRequest] = field(default_factory=list)

    @property
    def last_request_at(self) -> int | None:
        return max((r.created_at for r in self.requests), default=None)


def _as_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
        )
    except ValueError:
        return 0


class Seerr:
    def __init__(self, access: ServiceAccess, timeout: int = 60) -> None:
        self.base_url = access.base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = access.api_key

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def status(self) -> dict[str, Any]:
        response = self.session.get(self._url("status"), timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _paginate(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        skip = 0
        while True:
            response = self.session.get(
                self._url(path),
                params={"take": PAGE_SIZE, "skip": skip, **params},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results", [])
            yield from results
            page_info = payload.get("pageInfo", {})
            skip += PAGE_SIZE
            if skip >= page_info.get("results", 0) or not results:
                return

    def inventory(self) -> dict[int, SeerrMedia]:
        """Media entries keyed by Seerr ID, with their requests attached."""
        media: dict[int, SeerrMedia] = {}
        for entry in self._paginate("media"):
            media[entry["id"]] = SeerrMedia(
                id=entry["id"],
                media_type=entry.get("mediaType", ""),
                tmdb_id=entry.get("tmdbId"),
                tvdb_id=entry.get("tvdbId"),
                external_service_id=entry.get("externalServiceId"),
                status=entry.get("status", 0),
                rating_key=_as_int(entry.get("ratingKey")),
            )
        for entry in self._paginate("request", filter="all", sort="added"):
            media_entry = entry.get("media") or {}
            media_id = media_entry.get("id")
            if media_id is None:
                continue
            request = SeerrRequest(
                id=entry["id"],
                media_id=media_id,
                media_type=entry.get("type", media_entry.get("mediaType", "")),
                created_at=_timestamp(entry.get("createdAt")),
                seasons=[
                    season.get("seasonNumber")
                    for season in entry.get("seasons", [])
                    if season.get("seasonNumber") is not None
                ],
                requested_by=(entry.get("requestedBy") or {}).get(
                    "displayName", "inconnu"
                ),
                status=entry.get("status", 0),
            )
            if media_id not in media:
                media[media_id] = SeerrMedia(
                    id=media_id,
                    media_type=request.media_type,
                    tmdb_id=media_entry.get("tmdbId"),
                    tvdb_id=media_entry.get("tvdbId"),
                    external_service_id=media_entry.get("externalServiceId"),
                    status=media_entry.get("status", 0),
                    rating_key=_as_int(media_entry.get("ratingKey")),
                )
            media[media_id].requests.append(request)
        return media

    def delete_request(self, request_id: int) -> None:
        response = self.session.delete(
            self._url(f"request/{request_id}"), timeout=self.timeout
        )
        if response.status_code == 404:
            return
        response.raise_for_status()

    def delete_media(self, media_id: int) -> None:
        response = self.session.delete(
            self._url(f"media/{media_id}"), timeout=self.timeout
        )
        if response.status_code == 404:
            return
        response.raise_for_status()


def index_by_external_id(
    inventory: dict[int, SeerrMedia],
) -> tuple[dict[int, SeerrMedia], dict[int, SeerrMedia]]:
    """Return (movies by tmdbId, shows by tvdbId)."""
    movies: dict[int, SeerrMedia] = {}
    shows: dict[int, SeerrMedia] = {}
    for media in inventory.values():
        if media.media_type == "movie" and media.tmdb_id:
            movies[media.tmdb_id] = media
        elif media.media_type == "tv" and media.tvdb_id:
            shows[media.tvdb_id] = media
    return movies, shows


def index_by_rating_key(inventory: dict[int, SeerrMedia]) -> dict[int, SeerrMedia]:
    return {m.rating_key: m for m in inventory.values() if m.rating_key}
