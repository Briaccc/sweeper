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

    def torrents(self) -> list[Torrent]:
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
            )
            for entry in self._get("torrents/info")
        ]

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
