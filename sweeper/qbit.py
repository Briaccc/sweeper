"""qBittorrent client (WebUI API v2)."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests

from .discovery import QbitAccess
from .i18n import t
from .paths import PathMap


@dataclass
class Torrent:
    hash: str
    name: str
    category: str
    state: str
    size: int
    ratio: float
    seeding_seconds: int
    content_path: str
    save_path: str
    tracker: str
    #: automatic torrent management: qBittorrent moves the data when the category changes
    auto_tmm: bool = False
    progress: float = 1.0

    @property
    def seeding_days(self) -> float:
        return self.seeding_seconds / 86400.0

    @property
    def tracker_host(self) -> str:
        return urllib.parse.urlparse(self.tracker).hostname or ""


class QBittorrent:
    paths = PathMap()
    enabled = True

    def __init__(self, access: QbitAccess, timeout: int = 60) -> None:
        self.base_url = access.base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Referer"] = self.base_url
        self._credentials = (access.username, access.password)
        self._logged_in = False

    def login(self) -> None:
        if self._logged_in:
            return
        username, password = self._credentials
        response = self.session.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": username, "password": password},
            timeout=self.timeout,
        )
        response.raise_for_status()
        if response.text.strip().lower() == "fails.":
            raise RuntimeError(t("qBittorrent : identifiants refusés"))
        self._logged_in = True

    def _get(self, path: str, **params: Any) -> Any:
        self.login()
        response = self.session.get(
            f"{self.base_url}/api/v2/{path}", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()

    def version(self) -> str:
        self.login()
        response = self.session.get(
            f"{self.base_url}/api/v2/app/version", timeout=self.timeout
        )
        response.raise_for_status()
        return response.text.strip()

    def torrents(self, hashes: list[str] | None = None) -> list[Torrent]:
        if hashes is not None and not hashes:
            return []
        params = {"hashes": "|".join(hashes)} if hashes else {}
        return [
            Torrent(
                hash=entry["hash"],
                name=entry.get("name", ""),
                category=entry.get("category", ""),
                state=entry.get("state", ""),
                size=entry.get("size", 0),
                ratio=float(entry.get("ratio", 0.0)),
                seeding_seconds=int(entry.get("seeding_time", 0)),
                content_path=self.paths.to_local(entry.get("content_path", "")),
                save_path=self.paths.to_local(entry.get("save_path", "")),
                tracker=entry.get("tracker", ""),
                auto_tmm=bool(entry.get("auto_tmm", False)),
                progress=float(entry.get("progress", 1.0)),
            )
            for entry in self._get("torrents/info", **params)
        ]

    def _post(self, path: str, **data: str) -> None:
        self.login()
        response = self.session.post(
            f"{self.base_url}/api/v2/{path}", data=data, timeout=self.timeout
        )
        response.raise_for_status()

    def categories(self) -> dict[str, str]:
        """Category name -> its save path as configured ("" = implicit)."""
        return {
            name: entry.get("savePath", "")
            for name, entry in (self._get("torrents/categories") or {}).items()
        }

    def park(self, torrents: list[Torrent], category: str) -> None:
        """Move torrents into `category` without moving a single byte.

        A torrent in automatic management follows its category's folder: the
        change would make qBittorrent move its data. Switching it to manual
        management first pins it where it is. The new category gets the save
        path of the one the torrents come from, so a later automatic run finds
        it consistent.
        """
        if not torrents:
            return
        hashes = [x.hash for x in torrents]
        existing = self.categories()
        if category not in existing:
            sources = {existing.get(x.category, "") for x in torrents}
            self._post(
                "torrents/createCategory",
                category=category,
                savePath=sources.pop() if len(sources) == 1 else "",
            )
        managed = [x.hash for x in torrents if x.auto_tmm]
        if managed:
            self._post("torrents/setAutoManagement", hashes="|".join(managed), enable="false")
        self._post("torrents/setCategory", hashes="|".join(hashes), category=category)
        after = {x.hash: x for x in self.torrents(hashes)}
        moved = [
            x.name for x in torrents
            if x.hash in after
            and (after[x.hash].save_path, after[x.hash].content_path) != (x.save_path, x.content_path)
        ]
        if moved:
            raise RuntimeError(t(
                "qBittorrent : chemin changé après le changement de catégorie ({names})",
                names=", ".join(n[:30] for n in moved[:3]),
            ))

    def delete(self, hashes: list[str], delete_files: bool = True) -> None:
        if not hashes:
            return
        self.login()
        response = self.session.post(
            f"{self.base_url}/api/v2/torrents/delete",
            data={
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
