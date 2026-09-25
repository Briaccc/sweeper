"""Connecting to the services sweeper talks to — one place for the CLI and the server.

Where each setting comes from, highest priority first:

1. an environment variable (``SWEEPER_RADARR_API_KEY``…) — the container convention;
2. ``config.toml`` — typed in the Connections screen, or by hand;
3. auto-discovery — the key read from the application's own config file, which
   only works when sweeper runs on the same machine with access to that file.

Any service but Plex can be switched off (``enabled = false``): a Usenet-only
setup has no torrent client, a movies-only setup has no Sonarr. A disabled
service is replaced by a null client that reports nothing and refuses to delete.

Paths are the other thing that differs between setups. Radarr in a container may
see ``/movies`` where sweeper sees ``/mnt/media/movies``. Every hardlink
calculation depends on paths, so each service has a ``path_map`` translating what
it reports into what sweeper sees — and back when sweeper tells it a path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import discovery
from .arr import Radarr, Sonarr
from .i18n import t
from .paths import PathMap
from .qbit import QBittorrent
from .seerr import Seerr

OPTIONAL = ("radarr", "sonarr", "seerr", "qbittorrent")
LABELS = {"radarr": "Radarr", "sonarr": "Sonarr", "seerr": "Seerr",
          "qbittorrent": "qBittorrent", "plex": "Plex"}


# --------------------------------------------------------------------------
# Null clients for disabled services
# --------------------------------------------------------------------------

class _Disabled:
    name = "service"
    enabled = False
    paths = PathMap()

    def status(self) -> dict[str, Any]:
        return {"version": t("désactivé")}

    def get(self, path: str, **params: Any) -> list:
        return []

    def tags(self) -> dict[int, str]:
        return {}

    def root_folders(self) -> list[str]:
        return []

    def _refuse(self, *args, **kwargs):
        raise RuntimeError(t("{service} est désactivé dans la configuration", service=self.name))

    ensure_tag = create_tag = apply_tag = _refuse


class NullRadarr(_Disabled):
    name = "Radarr"

    def movies(self) -> list:
        return []

    def lookup_movie(self, term: str) -> list:
        return []

    delete_movie = adopt_movie = restore_movie = _Disabled._refuse


class NullSonarr(_Disabled):
    name = "Sonarr"

    def series(self) -> list:
        return []

    def episode_files(self, series_id: int) -> list:
        return []

    def lookup_series(self, term: str) -> list:
        return []

    delete_series = delete_episode_files = unmonitor_season = _Disabled._refuse
    adopt_series = restore_series = _Disabled._refuse


class NullQbit:
    enabled = False
    paths = PathMap()

    def version(self) -> str:
        return t("désactivé")

    def torrents(self) -> list:
        return []

    def delete(self, hashes: list[str], delete_files: bool = True) -> None:
        if hashes:
            raise RuntimeError(t("{service} est désactivé dans la configuration", service="qBittorrent"))


# --------------------------------------------------------------------------
# Connecting
# --------------------------------------------------------------------------

@dataclass
class Services:
    radarr: Any
    sonarr: Any
    seerr: Any            # None when disabled or unreachable: requests are simply not cleaned
    qbit: Any
    plex_db: Path | None
    plex_paths: PathMap


def enabled(cfg, name: str) -> bool:
    return bool(cfg.service(name).get("enabled", True))


def plex_database(cfg) -> Path | None:
    explicit = cfg.plex_db
    if explicit:
        return explicit
    return discovery.first_existing(discovery.plex_database_candidates())


def _paths(cfg, name: str) -> PathMap:
    return PathMap(cfg.service(name).get("path_map", ()))


def open_service(cfg, name: str):
    """One client, ready to use. Raises DiscoveryError when it cannot be reached."""
    settings = cfg.service(name)
    if name in OPTIONAL and not enabled(cfg, name):
        return {"radarr": NullRadarr, "sonarr": NullSonarr, "qbittorrent": NullQbit}.get(name, lambda: None)()
    if name == "radarr":
        client = Radarr(discovery.resolve_arr("Radarr", settings))
    elif name == "sonarr":
        client = Sonarr(discovery.resolve_arr("Sonarr", settings))
    elif name == "qbittorrent":
        client = QBittorrent(discovery.resolve_qbit(settings))
    elif name == "seerr":
        client = Seerr(discovery.resolve_seerr(settings))
    else:
        raise ValueError(name)
    client.paths = _paths(cfg, name)
    client.enabled = True
    return client


def connect(cfg, with_seerr: bool = True) -> Services:
    """Every client. Radarr, Sonarr and qBittorrent must answer when enabled;
    Seerr is best effort — without it sweeper still works, it just leaves requests."""
    seerr = None
    if with_seerr and enabled(cfg, "seerr"):
        try:
            seerr = open_service(cfg, "seerr")
        except Exception:  # noqa: BLE001
            seerr = None
    return Services(
        radarr=open_service(cfg, "radarr"),
        sonarr=open_service(cfg, "sonarr"),
        seerr=seerr,
        qbit=open_service(cfg, "qbittorrent"),
        plex_db=plex_database(cfg),
        plex_paths=_paths(cfg, "plex"),
    )


# --------------------------------------------------------------------------
# Describing and probing, for the Connections screen and `check`
# --------------------------------------------------------------------------

def mask(secret: str | None) -> str | None:
    if not secret:
        return None
    return "••••" + secret[-4:] if len(secret) > 8 else "••••"


def source_of(cfg, name: str, field: str) -> str:
    if (name, field) in getattr(cfg, "env_fields", set()):
        return "env"
    if cfg.service(name).get(field) not in (None, ""):
        return "config"
    return "auto"


def probe(cfg, name: str) -> dict[str, Any]:
    """Can sweeper reach this service with the current settings? Never raises."""
    settings = cfg.service(name)
    entry: dict[str, Any] = {
        "service": name,
        "nom": LABELS[name],
        "activé": name == "plex" or enabled(cfg, name),
        "requis": name == "plex",
        "url": settings.get("url"),
        "clé": mask(settings.get("api_key")),
        "utilisateur": settings.get("username"),
        "mot_de_passe": mask(settings.get("password")),
        "base": str(cfg.plex_db) if name == "plex" and cfg.plex_db else None,
        "correspondances": len(settings.get("path_map", ()) or ()),
        "sources": {f: source_of(cfg, name, f) for f in ("url", "api_key", "username", "password", "database")},
        "ok": False,
        "détail": "",
    }
    if not entry["activé"]:
        entry.update(ok=True, détail=t("désactivé"))
        return entry
    try:
        if name == "plex":
            db = plex_database(cfg)
            if db is None or not db.is_file():
                raise FileNotFoundError(t("base Plex introuvable — indique son chemin"))
            entry.update(ok=True, détail=str(db), base=str(db))
            return entry
        client = open_service(cfg, name)
        if name == "qbittorrent":
            version = client.version()
            entry["url"] = client.base_url
        else:
            version = client.status().get("version")
            entry["url"] = client.base_url
        entry.update(ok=True, détail=str(version))
    except FileNotFoundError as error:
        entry.update(ok=False, détail=str(error))
    except discovery.NotConfigured:
        entry.update(ok=False, détail=t(
            "non configuré — renseigne l'URL, l'utilisateur et le mot de passe, "
            "ou désactive-le si tu n'utilises pas de torrents" if name == "qbittorrent"
            else "non configuré — renseigne l'URL et la clé d'API"
        ))
    except Exception as error:  # noqa: BLE001
        entry.update(ok=False, détail=f"{type(error).__name__}: {error}")
    return entry


def probe_all(cfg) -> list[dict[str, Any]]:
    return [probe(cfg, name) for name in ("radarr", "sonarr", "qbittorrent", "seerr", "plex")]
