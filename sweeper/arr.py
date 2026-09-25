"""Radarr and Sonarr clients (API v3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .discovery import ServiceAccess
from .i18n import t
from .paths import PathMap


class ArrClient:
    #: translates paths as the application sees them into paths as sweeper sees them
    paths = PathMap()
    enabled = True

    def __init__(self, access: ServiceAccess, timeout: int = 60) -> None:
        self.base_url = access.base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = access.api_key

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v3/{path.lstrip('/')}"

    def get(self, path: str, **params: Any) -> Any:
        response = self.session.get(self._url(path), params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def delete(self, path: str, **params: Any) -> None:
        response = self.session.delete(
            self._url(path), params=params, timeout=self.timeout
        )
        response.raise_for_status()

    def put(self, path: str, payload: Any) -> Any:
        response = self.session.put(self._url(path), json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json() if response.content else None

    def post(self, path: str, payload: Any) -> Any:
        response = self.session.post(self._url(path), json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json() if response.content else None

    def status(self) -> dict[str, Any]:
        return self.get("system/status")

    def defaults(self, items: list) -> tuple[int, str]:
        """Most-used quality profile and root folder in the collection.

        Reusing what already dominates avoids re-adding a title with settings
        that look like nothing else in the library.
        """
        from collections import Counter

        profiles = Counter(
            i.get("qualityProfileId") for i in items if i.get("qualityProfileId")
        )
        folders = Counter(
            i.get("rootFolderPath") for i in items if i.get("rootFolderPath")
        )
        profile = profiles.most_common(1)[0][0] if profiles else None
        folder = folders.most_common(1)[0][0] if folders else None
        if profile is None:
            available = self.get("qualityprofile")
            profile = available[0]["id"] if available else 1
        if folder is None:
            available = self.get("rootfolder")
            if not available:
                raise RuntimeError(t(
                    "aucun dossier racine configuré — impossible de choisir où placer le titre"
                ))
            folder = available[0]["path"]
        return profile, folder

    def tags(self) -> dict[int, str]:
        return {tag["id"]: tag["label"] for tag in self.get("tag")}

    def root_folders(self) -> list[str]:
        """Root folders, as sweeper sees them."""
        return [self.paths.to_local(f["path"]) for f in self.get("rootfolder") if f.get("path")]

    def ensure_tag(self, label: str) -> int:
        for tag_id, existing in self.tags().items():
            if existing.lower() == label.lower():
                return tag_id
        return self.create_tag(label)

    def create_tag(self, label: str) -> int:
        response = self.session.post(
            self._url("tag"), json={"label": label}, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()["id"]


@dataclass
class Movie:
    id: int
    tmdb_id: int
    title: str
    year: int
    path: str
    file_path: str | None
    size: int
    added: str
    tag_ids: list[int]


@dataclass
class EpisodeFile:
    id: int
    season: int
    path: str
    size: int


@dataclass
class Series:
    id: int
    tvdb_id: int
    title: str
    path: str
    size: int
    added: str
    tag_ids: list[int]
    seasons: dict[int, bool] = field(default_factory=dict)  # number -> monitored


class Radarr(ArrClient):
    def movies(self) -> list[Movie]:
        result = []
        for entry in self.get("movie"):
            movie_file = entry.get("movieFile") or {}
            result.append(
                Movie(
                    id=entry["id"],
                    tmdb_id=entry.get("tmdbId", 0),
                    title=entry.get("title", ""),
                    year=entry.get("year", 0),
                    path=self.paths.to_local(entry.get("path", "")),
                    file_path=self.paths.to_local(movie_file.get("path")),
                    size=entry.get("sizeOnDisk", 0),
                    added=entry.get("added", ""),
                    tag_ids=entry.get("tags", []),
                )
            )
        return result

    def apply_tag(self, movie_ids: list[int], tag_id: int, add: bool = True) -> None:
        if not movie_ids:
            return
        self.put(
            "movie/editor",
            {
                "movieIds": movie_ids,
                "tags": [tag_id],
                "applyTags": "add" if add else "remove",
            },
        )

    def delete_movie(self, movie_id: int, add_exclusion: bool = False) -> None:
        self.delete(
            f"movie/{movie_id}",
            deleteFiles="true",
            addImportExclusion="true" if add_exclusion else "false",
        )


    def lookup_movie(self, term: str) -> list[dict]:
        return self.get("movie/lookup", term=term)

    def adopt_movie(self, match: dict, folder: str) -> dict:
        """Take over a movie already on disk, without moving anything.

        Radarr gets the existing folder as `path`: it scans it, imports the file
        and downloads nothing. From then on the usual deletion path applies —
        sweeper never has to touch a file itself.
        """
        profile, _root = self.defaults(self.get("movie"))
        movie = dict(match)
        movie.update(
            {
                "qualityProfileId": profile,
                # the folder is in sweeper's view: translate it into Radarr's
                "rootFolderPath": self.paths.to_remote(str(Path(folder).parent)),
                "path": self.paths.to_remote(folder),
                "monitored": True,
                "minimumAvailability": movie.get("minimumAvailability", "released"),
                "addOptions": {"searchForMovie": False, "monitor": "movieOnly"},
            }
        )
        return self.post("movie", movie)

    def restore_movie(self, tmdb_id: int) -> dict:
        """Re-add a movie to Radarr and start a search."""
        found = self.get("movie/lookup", term=f"tmdb:{tmdb_id}")
        if not found:
            raise RuntimeError(t("TMDB {id} introuvable dans Radarr", id=tmdb_id))
        movie = found[0]
        profile, folder = self.defaults(self.get("movie"))
        movie.update(
            {
                "qualityProfileId": profile,
                "rootFolderPath": folder,
                "monitored": True,
                "minimumAvailability": movie.get("minimumAvailability", "released"),
                "addOptions": {"searchForMovie": True},
            }
        )
        return self.post("movie", movie)


class Sonarr(ArrClient):
    def series(self) -> list[Series]:
        result = []
        for entry in self.get("series"):
            stats = entry.get("statistics") or {}
            result.append(
                Series(
                    id=entry["id"],
                    tvdb_id=entry.get("tvdbId", 0),
                    title=entry.get("title", ""),
                    path=self.paths.to_local(entry.get("path", "")),
                    size=stats.get("sizeOnDisk", 0),
                    added=entry.get("added", ""),
                    tag_ids=entry.get("tags", []),
                    seasons={
                        season["seasonNumber"]: season.get("monitored", False)
                        for season in entry.get("seasons", [])
                    },
                )
            )
        return result

    def episode_files(self, series_id: int) -> list[EpisodeFile]:
        return [
            EpisodeFile(
                id=entry["id"],
                season=entry.get("seasonNumber", -1),
                path=self.paths.to_local(entry.get("path", "")),
                size=entry.get("size", 0),
            )
            for entry in self.get("episodefile", seriesId=series_id)
        ]

    def apply_tag(self, series_ids: list[int], tag_id: int, add: bool = True) -> None:
        if not series_ids:
            return
        self.put(
            "series/editor",
            {
                "seriesIds": series_ids,
                "tags": [tag_id],
                "applyTags": "add" if add else "remove",
            },
        )

    def lookup_series(self, term: str) -> list[dict]:
        return self.get("series/lookup", term=term)

    def adopt_series(self, match: dict, folder: str, season_folders: bool = True) -> dict:
        """Take over a series already on disk, without moving anything.

        `season_folders` mirrors the layout found on disk: forcing it on a flat
        series would invite Sonarr to move everything on the first rename.
        """
        profile, _root = self.defaults(self.get("series"))
        series = dict(match)
        series.update(
            {
                "qualityProfileId": profile,
                "rootFolderPath": self.paths.to_remote(str(Path(folder).parent)),
                "path": self.paths.to_remote(folder),
                "monitored": True,
                "seasonFolder": season_folders,
                "addOptions": {"searchForMissingEpisodes": False, "monitor": "existing"},
            }
        )
        return self.post("series", series)

    def restore_series(self, tvdb_id: int, season: int | None = None) -> dict:
        """Re-add a series to Sonarr and start a search."""
        found = self.get("series/lookup", term=f"tvdb:{tvdb_id}")
        if not found:
            raise RuntimeError(t("TVDB {id} introuvable dans Sonarr", id=tvdb_id))
        series = found[0]
        profile, folder = self.defaults(self.get("series"))
        seasons = series.get("seasons", [])
        if season is not None:
            # restoring a single season: the others stay unmonitored
            for entry in seasons:
                entry["monitored"] = entry.get("seasonNumber") == season
        series.update(
            {
                "qualityProfileId": profile,
                "rootFolderPath": folder,
                "monitored": True,
                "seasons": seasons,
                "addOptions": {
                    "searchForMissingEpisodes": True,
                    "monitor": "all" if season is None else "none",
                },
            }
        )
        return self.post("series", series)

    def delete_series(self, series_id: int, add_exclusion: bool = False) -> None:
        self.delete(
            f"series/{series_id}",
            deleteFiles="true",
            addImportListExclusion="true" if add_exclusion else "false",
        )

    def delete_episode_files(self, file_ids: list[int]) -> None:
        """Bulk-delete episode files (keeps the series in the database)."""
        if not file_ids:
            return
        response = self.session.delete(
            self._url("episodefile/bulk"),
            json={"episodeFileIds": file_ids},
            timeout=self.timeout,
        )
        if response.status_code == 404:  # older instances: one at a time
            for file_id in file_ids:
                self.delete(f"episodefile/{file_id}")
            return
        response.raise_for_status()

    def unmonitor_season(self, series_id: int, season_number: int) -> None:
        payload = self.get(f"series/{series_id}")
        for season in payload.get("seasons", []):
            if season.get("seasonNumber") == season_number:
                season["monitored"] = False
        self.put(f"series/{series_id}", payload)
