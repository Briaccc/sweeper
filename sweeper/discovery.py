"""Locating the services sweeper talks to.

Two ways to reach a service:

1. **Auto-discovery** — read the credentials straight out of the application's
   own configuration file. Nothing is copied into sweeper's config, so there is
   no second place for an API key to go stale or leak.
2. **Explicit** — the URL and key are written in sweeper's config. Needed when
   the application runs in a container, on another host, or behind a proxy.

Auto-discovery is tried first and falls back to the explicit settings.
"""

from __future__ import annotations

import json
import os
import platform
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

HOME = Path.home()


class DiscoveryError(RuntimeError):
    pass


class NotConfigured(DiscoveryError):
    """Nothing tells sweeper where the service is: no setting, nothing to discover."""


@dataclass(frozen=True)
class ServiceAccess:
    base_url: str
    api_key: str


@dataclass(frozen=True)
class QbitAccess:
    base_url: str
    username: str
    password: str


# --------------------------------------------------------------------------
# Candidate locations
# --------------------------------------------------------------------------

def _windows_dirs() -> list[Path]:
    dirs = []
    for var in ("ProgramData", "LOCALAPPDATA", "APPDATA"):
        if value := os.environ.get(var):
            dirs.append(Path(value))
    return dirs


def arr_config_candidates(app: str) -> list[Path]:
    """Where `app` (Radarr, Sonarr, …) keeps config.xml, across install styles."""
    lower = app.lower()
    paths = [
        HOME / ".config" / app / "config.xml",          # native Linux
        HOME / ".config" / lower / "config.xml",
        Path("/config/config.xml"),                     # common container mount
        Path(f"/config/{app}/config.xml"),
        Path(f"/var/lib/{lower}/config.xml"),           # distribution package
        Path(f"/opt/{app}/config.xml"),
        HOME / "Library/Application Support" / app / "config.xml",   # macOS
    ]
    paths += [d / app / "config.xml" for d in _windows_dirs()]
    return paths


def plex_database_candidates() -> list[Path]:
    """Where Plex keeps its library database, across platforms."""
    suffix = Path("Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db")
    roots = [
        HOME / "Library/Application Support",                     # macOS, and many seedboxes
        Path("/var/lib/plexmediaserver/Library/Application Support"),
        Path("/config/Library/Application Support"),              # container mount
        HOME / ".local/share",
        Path("/var/snap/plexmediaserver/common/Library/Application Support"),
    ]
    if platform.system() == "Windows":
        # %LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases\… — same suffix
        roots += [d for d in _windows_dirs()]
    return [root / suffix for root in roots]


def seerr_settings_candidates() -> list[Path]:
    """Overseerr, Jellyseerr and Seerr all use the same settings.json layout."""
    names = ("seerr", "jellyseerr", "overseerr")
    paths = []
    for name in names:
        paths += [
            HOME / name / "config/settings.json",
            HOME / ".config" / name / "settings.json",
            Path(f"/app/{name}/config/settings.json"),
        ]
    paths.append(Path("/app/config/settings.json"))
    paths.append(Path("/config/settings.json"))
    return paths


def first_existing(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.is_file()), None)


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def read_arr_config(path: Path, host: str = "127.0.0.1") -> ServiceAccess:
    """Port, API key and URL base out of an *arr config.xml."""
    root = ElementTree.parse(path).getroot()

    def field(name: str, default: str = "") -> str:
        node = root.find(name)
        return (node.text or default).strip() if node is not None else default

    api_key = field("ApiKey")
    if not api_key:
        raise DiscoveryError(f"no <ApiKey> in {path}")
    scheme = "https" if field("EnableSsl").lower() == "true" else "http"
    port = field("SslPort" if scheme == "https" else "Port") or "80"
    base = f"{scheme}://{host}:{port}"
    if url_base := field("UrlBase").strip("/"):
        base = f"{base}/{url_base}"
    return ServiceAccess(base_url=base, api_key=api_key)


def read_seerr_settings(path: Path, base_url: str) -> ServiceAccess:
    with path.open(encoding="utf-8") as handle:
        settings = json.load(handle)
    api_key = (settings.get("main") or {}).get("apiKey")
    if not api_key:
        raise DiscoveryError(f"no main.apiKey in {path}")
    return ServiceAccess(base_url=base_url.rstrip("/"), api_key=api_key)


_CROSS_SEED_CLIENT = re.compile(r'"qbittorrent:(?P<url>[^"]+)"')


def read_qbit_from_cross_seed(path: Path) -> QbitAccess:
    """cross-seed stores the qBittorrent URL with its credentials embedded."""
    match = _CROSS_SEED_CLIENT.search(path.read_text(encoding="utf-8"))
    if not match:
        raise DiscoveryError(f"no qbittorrent entry in torrentClients of {path}")
    return qbit_from_url(match.group("url"))


def qbit_from_url(url: str) -> QbitAccess:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    if host == "localhost":
        host = "127.0.0.1"
    netloc = host if not parsed.port else f"{host}:{parsed.port}"
    return QbitAccess(
        base_url=urllib.parse.urlunsplit((parsed.scheme or "http", netloc, "", "", "")),
        username=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
    )


# --------------------------------------------------------------------------
# Resolution: explicit settings win, discovery fills the gaps
# --------------------------------------------------------------------------

def resolve_arr(app: str, settings: dict) -> ServiceAccess:
    if settings.get("url") and settings.get("api_key"):
        return ServiceAccess(settings["url"].rstrip("/"), settings["api_key"])

    host = settings.get("host", "127.0.0.1")
    explicit = settings.get("config_xml")
    if explicit:
        path = Path(str(explicit)).expanduser()
        if not path.is_file():
            raise DiscoveryError(f"{app}: {path} does not exist")
        return read_arr_config(path, host)

    path = first_existing(arr_config_candidates(app))
    if path is None:
        raise NotConfigured(
            f"{app}: no config.xml found. Set services.{app.lower()}.config_xml, "
            f"or services.{app.lower()}.url and .api_key."
        )
    access = read_arr_config(path, host)
    if settings.get("url"):        # container: key from the file, URL from config
        return ServiceAccess(settings["url"].rstrip("/"), access.api_key)
    return access


def resolve_seerr(settings: dict) -> ServiceAccess:
    url = (settings.get("url") or "http://127.0.0.1:5055").rstrip("/")
    if settings.get("api_key"):
        return ServiceAccess(url, settings["api_key"])
    explicit = settings.get("settings_json")
    path = Path(str(explicit)).expanduser() if explicit else first_existing(
        seerr_settings_candidates()
    )
    if path is None or not path.is_file():
        raise NotConfigured(
            "seerr: no settings.json found. Set services.seerr.settings_json, "
            "or services.seerr.api_key."
        )
    return read_seerr_settings(path, url)


def resolve_qbit(settings: dict) -> QbitAccess:
    if settings.get("url") and settings.get("username") is not None:
        return QbitAccess(
            base_url=settings["url"].rstrip("/"),
            username=settings.get("username", ""),
            password=settings.get("password", ""),
        )
    source = settings.get("from_cross_seed")
    path = (
        Path(str(source)).expanduser()
        if source
        else first_existing([HOME / ".cross-seed/config.js", Path("/config/config.js")])
    )
    if path is None or not path.is_file():
        raise NotConfigured(
            "qbittorrent: set services.qbittorrent.url / .username / .password, "
            "or point services.qbittorrent.from_cross_seed at a cross-seed config."
        )
    return read_qbit_from_cross_seed(path)


# --------------------------------------------------------------------------
# Hardlink roots
# --------------------------------------------------------------------------

def auto_link_roots(radarr, sonarr, qbit, cross_seed: Path | None = None) -> list[Path]:
    """Every directory tree that may hold a hardlink to the same bytes.

    Getting this wrong breaks the freed-bytes arithmetic silently — a missing
    tree means sweeper believes deleting a file frees space when another name
    still pins the inode. So it is derived from the applications themselves
    rather than left to the user: library roots from the *arr root folders,
    download roots from the torrents' save paths, link dirs from cross-seed.
    """
    roots: set[Path] = set()

    for client in (radarr, sonarr):
        try:
            for path in client.root_folders():
                roots.add(Path(path))
        except Exception:  # noqa: BLE001 — a missing service must not be fatal
            continue

    try:
        for torrent in qbit.torrents():
            if torrent.save_path:
                roots.add(Path(torrent.save_path))
    except Exception:  # noqa: BLE001
        pass

    if cross_seed and cross_seed.is_file():
        text = cross_seed.read_text(encoding="utf-8")
        if match := re.search(r"linkDirs\s*:\s*\[(?P<body>[^\]]*)\]", text):
            for raw in re.findall(r'"([^"]+)"', match.group("body")):
                roots.add(Path(raw))

    return _prune(roots)


def _prune(roots: set[Path]) -> list[Path]:
    """Drop directories already covered by an ancestor, and anything missing.

    Scanning both a parent and its child would count some files twice and slow
    the walk down for nothing.
    """
    existing = sorted({r for r in roots if r.is_dir()}, key=lambda p: len(p.parts))
    kept: list[Path] = []
    for root in existing:
        if any(root == k or k in root.parents for k in kept):
            continue
        kept.append(root)
    return kept
