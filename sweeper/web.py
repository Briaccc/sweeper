"""sweeper web UI: an HTTP server from the standard library.

No dependencies to install. The server only listens on loopback: the machine
may be shared with other accounts. Access it through an SSH tunnel (see
README). A token additionally protects every request.
"""

from __future__ import annotations

import gzip
import json
import mimetypes
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import config as config_module
from . import i18n
from .i18n import code_of, t
from .arr import Radarr, Sonarr
from .config import Config, Rule, Schedule
from .jobs import JobRunner
from .watchlist import Watchlist
from .engine import Candidate, Sweeper
from .linkmap import LinkMap, uncovered_torrents
from . import orphans as orphans_module
from . import trackers as trackers_module
from . import requests as requests_module
from . import usage as usage_module
from .config import SeedingPolicy
from .plexdb import load_snapshot
from .qbit import QBittorrent
from .seerr import Seerr

ASSETS = Path(__file__).resolve().parent.parent / "webui"


def read_quota(cfg: Config) -> dict[str, float]:
    """How much of the allowance is used.

    Managed hosts often expose a quota tool that knows the plan size, which the
    filesystem does not — a shared filesystem reports the whole array. Use that
    tool when one is configured, and fall back to the filesystem otherwise.
    """
    if cfg.quota_command:
        try:
            output = subprocess.run(
                cfg.quota_command,
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout
            raw = json.loads(output)
            return {
                "plan_gb": float(raw["plan"]),
                "used_gb": float(raw["used"]),
                "free_gb": float(raw["free"]),
                "source": "quota command",
            }
        except Exception as error:  # noqa: BLE001
            print("  " + t("la commande de quota a échoué ({error}) ; mesure du système de fichiers à la place", error=error))

    import shutil

    path = cfg.storage_path
    while not path.exists() and path != path.parent:
        path = path.parent            # a path not mounted yet: measure what holds it
    usage = shutil.disk_usage(str(path))
    return {
        "plan_gb": usage.total / 1e9,
        "used_gb": usage.used / 1e9,
        "free_gb": usage.free / 1e9,
        "source": "filesystem",
    }


class State:
    """Compute the inventory and keep it in memory (a few seconds of work)."""

    def __init__(self, cfg: Config) -> None:
        self.config = cfg
        self.lock = threading.Lock()
        self.computed_at: float = 0.0
        self.inventory: list[Candidate] = []
        self.engine: Sweeper | None = None
        self.quota: dict[str, float] = {}
        self.growth: dict[str, int] = {}
        self.composition: dict[str, int] = {}
        self.orphans: list = []
        self.trackers: list = []
        self.link_roots: list[Path] = []
        self.coverage_gap: dict = {"torrents": 0, "octets": 0, "exemples": []}
        self.missing_files: dict = {"éléments": 0, "exemple": None}
        self.plex_collections: dict[str, int] = {}
        self.started_at: float = time.time()
        self.health: dict = {}
        self.requesters: dict = {"demandeurs": [], "totaux": {}}
        self.library_roots: list[Path] = []
        self.error: str | None = None
        self._last_link_map: LinkMap | None = None
        self.plex_db: Path | None = None
        self.plex_paths = None
        self.connections: list[dict] = []
        self.jobs = JobRunner(path=cfg.state_dir / "jobs.jsonl")
        self.watchlist = Watchlist(cfg.state_dir / "watchlist.json")
        self.last_scheduled: float = 0.0

    # ------------------------------------------------------------ automation

    def due_media(self) -> list[Candidate]:
        """Queued items that have become deletable.

        The queue is an explicit intent: it doesn't depend on any rule. What
        matters is that no blocker remains — seed obligation met, no
        protection, an app available to carry out the deletion.
        """
        wanted = self.watchlist.keys("media")
        return [c for c in self.inventory if c.key in wanted and not c.blockers]

    def due_orphans(self) -> list:
        wanted = self.watchlist.keys("orphan")
        return [g for g in self.orphans if g.key in wanted and g.seed_ok]

    def run_scheduled(self, job) -> None:
        """One automatic pass: the queue first, then the rules."""
        schedule = self.config.schedule
        engine = self.engine
        if engine is None:
            return
        if schedule.apply_watchlist:
            media = self.due_media()
            orphan_groups = self.due_orphans()
            job.total += len(media) + len(orphan_groups)
            if media:
                plan = engine.plan_for_keys(self.inventory, [c.key for c in media])
                engine.execute(plan, progress=lambda r: _tick(job, r))
                for candidate in media:
                    self.watchlist.remove(candidate.key)
            if orphan_groups:
                engine.delete_orphan_groups(
                    orphan_groups, progress=lambda r: _tick(job, r)
                )
                for group in orphan_groups:
                    self.watchlist.remove(group.key)
            self.watchlist.save()
        if schedule.apply_rules:
            from .plexdb import load_snapshot  # local: avoids a circular import

            self.refresh()
            # use the Plex database resolved during refresh: config.plex_db is None
            # when it was auto-discovered
            plan = self.engine.build_plan(
                load_snapshot(self.plex_db, self.plex_paths), self._last_link_map
            )
            job.total += len(plan.candidates)
            self.engine.execute(plan, progress=lambda r: _tick(job, r))
        self.last_scheduled = time.time()
        self.refresh()

    def services(self):
        from . import services as services_module

        svc = services_module.connect(self.config)
        self.plex_paths = svc.plex_paths
        return svc.radarr, svc.sonarr, svc.seerr, svc.qbit

    def refresh(self) -> None:
        with self.lock:
            began = time.time()
            try:
                self.config = config_module.load(self.config.config_path)
                from . import services as services_module

                # each service is probed separately: an unresponsive one must not
                # hide the state of the others, nor prevent fixing its configuration
                self.connections = services_module.probe_all(self.config)
                broken = [c for c in self.connections if c["activé"] and not c["ok"]
                          and c["service"] != "seerr"]
                if broken:
                    raise RuntimeError(t("connexion impossible — {details}", details=t(" ; ").join(
                        t("{name} : {detail}", name=c["nom"], detail=c["détail"]) for c in broken)))
                from . import discovery

                radarr, sonarr, seerr, qbit = self.services()
                from . import services as services_module

                plex_db = services_module.plex_database(self.config)
                if plex_db is None:
                    raise RuntimeError(
                        t("base Plex introuvable — indique son chemin dans l'écran Connexions")
                    )
                snapshot = load_snapshot(plex_db, self.plex_paths)
                self.plex_db = plex_db
                self.plex_collections = dict(snapshot.collections)
                library_roots = sorted(
                    {Path(m.path).parent for m in radarr.movies() if m.path}
                    | {Path(s.path).parent for s in sonarr.series() if s.path}
                )
                roots = self.config.link_roots or discovery.auto_link_roots(
                    radarr,
                    sonarr,
                    qbit,
                    discovery.first_existing([Path.home() / ".cross-seed/config.js"]),
                )
                self.link_roots = roots
                link_map = (
                    LinkMap(roots, library_roots).scan().index_torrents(qbit.torrents())
                )
                self._last_link_map = link_map
                engine = Sweeper(self.config, radarr, sonarr, seerr, qbit)
                # everything is computed into locals: a request reading the state
                # during these seconds sees the complete old state, not a mix
                inventory = engine.build_inventory(snapshot, link_map)
                all_torrents = qbit.torrents()
                orphans = orphans_module.find(
                    link_map, all_torrents, library_roots, self.config.seeding
                )
                requesters = requests_module.collect(engine.seerr_inventory, inventory)
                trackers = trackers_module.collect(
                    all_torrents, inventory, orphans, self.config.seeding
                )
                gap = uncovered_torrents(all_torrents, roots)
                blind = [c for c in inventory if c.blockers and c.blockers[0].startswith("fichiers introuvables")]
                unseen = list(engine.unseen)
                example = (blind[0].blockers[0].split("(ex. ")[1].split(")")[0] if blind
                           else unseen[0] if unseen else None)
                self.missing_files = {"éléments": len(blind) + len(unseen), "exemple": example}
                self.inventory, self.orphans = inventory, orphans
                self.requesters, self.trackers = requesters, trackers
                self.engine = engine
                self.library_roots = library_roots
                self.coverage_gap = {
                    "torrents": len(gap),
                    "octets": sum(t.size for t in gap),
                    "exemples": [
                        {"nom": " ".join(t.name.split())[:60], "chemin": t.save_path}
                        for t in gap[:5]
                    ],
                }
                self.quota = read_quota(self.config)
                self.growth = snapshot.additions_by_month
                self.composition = self._composition(link_map, library_roots)
                usage_module.record(
                    self.config.state_dir / "usage.jsonl",
                    self.quota,
                    self.composition,
                    sum(g.bytes for g in self.orphans),
                )
                valid = {c.key for c in self.inventory} | {g.key for g in self.orphans}
                for entry in self.watchlist.prune(valid):
                    print("  " + t("file d'attente : « {title} » retiré (clé disparue)", title=entry.title[:50]))
                self.computed_at = time.time()
                self.error = None
                self.health = self._health(radarr, sonarr, seerr, qbit, plex_db, began)
            except Exception as error:  # noqa: BLE001
                self.error = f"{type(error).__name__}: {error}"
                raise

    def _health(self, radarr, sonarr, seerr, qbit, plex_db: Path, began: float) -> dict:
        """What `sweeper.py check` would report, measured here for the dashboard.

        Each probe is isolated: an unresponsive service must not hide the others.
        """
        def probe(label, fn):
            try:
                return {"service": label, "ok": True, "détail": str(fn())}
            except Exception as error:  # noqa: BLE001
                return {"service": label, "ok": False, "détail": f"{type(error).__name__}: {error}"}

        services = [
            {"service": c["nom"], "ok": c["ok"], "détail": c["détail"]}
            for c in self.connections if c["service"] != "plex"
        ]
        state_dir = self.config.state_dir
        state_bytes = sum(f.stat().st_size for f in state_dir.glob("*") if f.is_file()) if state_dir.is_dir() else 0
        return {
            "services": services,
            "plex_db_écrite_il_y_a": int(time.time() - plex_db.stat().st_mtime) if plex_db.is_file() else None,
            "durée_rafraîchissement": round(time.time() - began, 1),
            "démarré_depuis": int(time.time() - self.started_at),
            "état_octets": state_bytes,
            "racines": [str(r) for r in self.link_roots],
        }

    def _composition(self, link_map: LinkMap, library_roots: list[Path]) -> dict:
        """Split actual bytes between watched library, unwatched library and seed-only."""
        roots = [str(r) for r in library_roots]

        def in_library(path: str) -> bool:
            return any(path == r or path.startswith(r + "/") for r in roots)

        watched: set[int] = set()
        unwatched: set[int] = set()
        for item in self.inventory:
            if item.kind == "season":
                continue  # already counted in the series
            target = watched if item.stats.last_viewed else unwatched
            for path in item.files:
                inode = link_map.inode_by_path.get(path)
                if inode is not None:
                    target.add(inode)
        unwatched -= watched

        library_inodes: set[int] = set()
        seed_only = 0
        for inode, paths in link_map.paths_by_inode.items():
            if any(in_library(p) for p in paths):
                library_inodes.add(inode)
            else:
                seed_only += link_map.size_by_inode.get(inode, 0)

        size = link_map.size_by_inode
        used = int(self.quota.get("used_gb", 0) * 1e9)
        vue = sum(size.get(i, 0) for i in watched)
        jamais = sum(size.get(i, 0) for i in unwatched)
        autre = sum(size.get(i, 0) for i in library_inodes - watched - unwatched)
        return {
            "vue": vue,
            "jamais_vue": jamais,
            "hors_suivi": autre,
            "seed_seul": seed_only,
            "reste": max(used - (vue + jamais + autre + seed_only), 0),
        }


def _tick(job, record: dict) -> None:
    job.done += 1
    job.freed += record.get("octets_libérés", 0)
    job.results.append(record)


def candidate_json(item: Candidate, requesters: dict[str, list] | None = None) -> dict[str, Any]:
    return {
        "requesters": (requesters or {}).get(item.key, []),
        "key": item.key,
        "kind": item.kind,
        "title": item.title,
        "rule": item.rule,
        "eligible": item.eligible,
        "actionable": item.actionable,
        "library_bytes": item.library_bytes,
        "freed_bytes": item.freed_bytes,
        "last_viewed": item.stats.last_viewed,
        "viewers": item.stats.distinct_viewers,
        "views": item.stats.views,
        "age_days": round(item.age_days),
        "idle_days": round(item.idle_days) if item.idle_days is not None else None,
        "files": len(item.files),
        "torrents": [
            {
                "name": " ".join(i.torrent.name.split()),
                "tracker": i.torrent.tracker_host,
                "ratio": round(i.torrent.ratio, 2),
                "seed_days": round(i.torrent.seeding_days),
                "category": i.torrent.category,
            }
            for i in item.impacts
        ],
        "seerr_requests": len(item.seerr_request_ids),
        "seerr_media": item.seerr_media_id,
        "blockers": item.blockers,
        "blocker_codes": [code_of(b) for b in item.blockers],
        "in_progress": item.in_progress,
        "unmanaged": item.unmanaged,
        "collections": item.collections,
        "plex_ids": item.plex_ids,
        "year": item.year,
        "unblocks_in_days": item.unblocks_in_days,
        "seed_blocked": item.seed_blocked,
    }


def orphan_json(group) -> dict[str, Any]:
    return {
        "key": group.key,
        "name": group.name,
        "bytes": group.bytes,
        "files": group.files,
        "torrents": len(group.torrents),
        "trackers": group.trackers,
        "ratio": round(group.best_ratio, 2),
        "seed_days": round(group.oldest_seed_days),
        "unblocks_in_days": group.unblocks_in_days,
        "seed_ok": group.seed_ok,
        "blocked": group.blocked,
        "blocked_code": code_of(group.blocked) if group.blocked else None,
    }


def _schedule_json(schedule: Schedule) -> dict[str, Any]:
    return {
        "enabled": schedule.enabled,
        "interval_minutes": schedule.interval_minutes,
        "apply_rules": schedule.apply_rules,
        "apply_watchlist": schedule.apply_watchlist,
    }


def rule_json(rule: Rule) -> dict[str, Any]:
    return {
        "name": rule.name,
        "media": rule.media,
        "enabled": rule.enabled,
        "never_watched": rule.never_watched,
        "unwatched_days": rule.unwatched_days,
        "min_age_days": rule.min_age_days,
        "max_distinct_viewers": rule.max_distinct_viewers,
    }


def rule_stats(state_dir: Path) -> dict[str, dict]:
    """What each rule has actually deleted since the start, according to the log."""
    path = state_dir / "history.jsonl"
    stats: dict[str, dict] = {}
    if not path.is_file():
        return stats
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("erreurs"):
            continue
        name = record.get("règle") or t("(sélection manuelle)")
        entry = stats.setdefault(name, {"éléments": 0, "octets": 0, "dernier": 0})
        entry["éléments"] += 1
        entry["octets"] += record.get("octets_libérés", 0)
        entry["dernier"] = max(entry["dernier"], record.get("horodatage", 0))
    return stats


def state_payload(state: State) -> dict[str, Any]:
    """Build the full /api/state payload.

    Pure function, separate from the HTTP handler: it takes an already
    refreshed State and does no I/O. That makes it testable with a
    purpose-built State (for example a different config) without starting a
    server or duplicating the serialisation by hand.
    """
    cfg = state.config
    used_gb = state.quota.get("used_gb", 0)
    plan_gb = state.quota.get("plan_gb", 0)
    inventory = state.inventory
    never = [c for c in inventory if not c.stats.last_viewed and c.kind != "season"]
    months = sorted(state.growth.items())[-12:]
    recent = [octets for _mois, octets in months[-6:]]
    return {
        "calculé_à": state.computed_at,
        "quota": state.quota,
        "composition": state.composition,
        "croissance": [{"mois": m, "octets": o} for m, o in months],
        "croissance_moyenne": sum(recent) / len(recent) if recent else 0,
        "paliers": [
            {
                "nom": tier.name,
                "gb": tier.gb,
                "prix": tier.price,
                "devise": tier.currency,
                "actuel": abs(tier.gb - plan_gb) < 50,
                "tient": used_gb < tier.gb,
            }
            for tier in cfg.tiers
        ],
        "totaux": {
            "éléments": len([c for c in inventory if c.kind != "season"]),
            "jamais_vus": len(never),
            "octets_jamais_vus": sum(c.library_bytes for c in never),
            "libérable_maintenant": sum(
                c.freed_bytes for c in inventory if c.actionable
            ),
            "libérable_bloqué": sum(
                c.freed_bytes for c in inventory if c.eligible and not c.actionable
            ),
        },
        "règles": [rule_json(rule) for rule in cfg.rules],
        "stats_règles": rule_stats(cfg.state_dir),
        "protection": {
            "tags": cfg.protect.tags,
            "requested_within_days": cfg.protect.requested_within_days,
            "in_progress": cfg.protect.in_progress,
            "collections": cfg.protect.collections,
        },
        "collections_plex": state.plex_collections,
        "seeding": {
            "mode": cfg.seeding.mode,
            "min_ratio": cfg.seeding.min_ratio,
            "min_seed_days": cfg.seeding.min_seed_days,
        },
        "médias": [
            candidate_json(c, state.requesters.get("par_média")) for c in inventory
        ],
        "orphelins": [orphan_json(g) for g in state.orphans],
        "orphelins_totaux": {
            "lots": len(state.orphans),
            "octets": sum(g.bytes for g in state.orphans),
            "prêts": sum(g.bytes for g in state.orphans if g.seed_ok),
            "en_attente": sum(
                g.bytes for g in state.orphans if not g.seed_ok and not g.blocked
            ),
        },
        "libérable_sous_30j": sum(
            c.freed_bytes
            for c in inventory
            if c.eligible
            and not c.actionable
            and c.unblocks_in_days is not None
            and c.unblocks_in_days <= 30
        )
        + sum(
            g.bytes
            for g in state.orphans
            if not g.seed_ok
            and g.unblocks_in_days is not None
            and g.unblocks_in_days <= 30
        ),
        "file_attente": state.watchlist.as_json(),
        "en_attente_prêts": {
            "médias": len(state.due_media()),
            "lots": len(state.due_orphans()),
            "octets": sum(c.freed_bytes for c in state.due_media())
            + sum(g.bytes for g in state.due_orphans()),
        },
        "trackers": [t.as_json() for t in state.trackers],
        "demandes": state.requesters,
        "occupation": (
            lambda samples: {
                "relevés": samples[-90:],
                "tendance": usage_module.trend(samples),
            }
        )(usage_module.load(state.config.state_dir / "usage.jsonl")),
        "planification": _schedule_json(cfg.schedule),
        "dernier_passage": state.last_scheduled,
        "job": (job.as_json() if (job := state.jobs.current()) else None),
        "couverture_hardlinks": state.coverage_gap,
        "fichiers_introuvables": state.missing_files,
        "connexions": state.connections,
        "configuré": bool(state.computed_at),
        "santé": state.health,
        "derniers_travaux": [
            {
                "id": j.id, "type": j.kind, "libellé": j.label, "faits": j.done,
                "total": j.total, "octets_libérés": j.freed, "terminé": j.finished,
                "erreur": j.error, "début": int(j.started_at),
                "durée": round((j.finished_at or time.time()) - j.started_at, 1),
                "échecs": sum(1 for r in j.results if r.get("erreurs")),
            }
            for j in state.jobs.recent()[:8]
        ],
        "erreur": state.error,
        "langue": i18n.language(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "sweeper"
    state: State
    token: str
    _login_failures = 0
    _login_lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.path.startswith("/api/state"):
            print(f"  {self.address_string()} {fmt % args}")

    # Reachable from the Internet through a reverse proxy: standard web page
    # protections apply. There are no inline scripts (the login one lives in
    # /assets/login.js), so the CSP can be strict.
    CSP = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def _write(self, body: bytes, content_type: str, status: int = 200, extra=None) -> None:
        """Single exit point: headers, compression, body.

        The state weighs ~430 KB and the UI reloads it after every action;
        gzip cuts it tenfold for what goes through the reverse proxy.
        """
        accepts = "gzip" in self.headers.get("Accept-Encoding", "")
        if accepts and len(body) > 1024:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if accepts and len(body) > 0 and body[:2] == b"\x1f\x8b":
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self._security_headers()
        for name, value in (extra or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send(self, payload: Any, status: int = 200) -> None:
        self._write(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8", status,
        )

    def _authorised(self) -> bool:
        supplied = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        ).get("token", [None])[0]
        if supplied is None:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "sweeper_token":
                    supplied = value
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._authorised():
            # the login screen needs its stylesheet and its script — nothing
            # else is served without a token
            if path in ("/assets/style.css", "/assets/login.js", "/assets/i18n.js"):
                self._serve_asset(path[len("/assets/"):])
            elif path.startswith("/api/"):
                self._send({"erreur": t("jeton invalide")}, 401)
            else:
                self._serve_asset("login.html")
            return
        if path in ("/", "/index.html"):
            self._serve_asset("index.html", set_cookie=True)
        elif path.startswith("/assets/"):
            self._serve_asset(path[len("/assets/"):])
        elif path == "/api/state":
            self._api_state()
        elif path == "/api/history":
            self._api_history()
        elif path == "/api/job":
            self._api_job()
        else:
            self._send({"erreur": t("route inconnue")}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/login" and not self._authorised():
            self._send({"erreur": t("jeton invalide")}, 401)
            return
        try:
            if path == "/api/login":
                self._api_login()
            elif path == "/api/refresh":
                self.state.refresh()
                self._api_state()
            elif path == "/api/rules":
                self._api_save_rules()
            elif path == "/api/delete":
                self._api_delete()
            elif path == "/api/protect":
                self._api_protect()
            elif path == "/api/orphans/delete":
                self._api_delete_orphans()
            elif path == "/api/rules/preview":
                self._api_preview_rules()
            elif path == "/api/watchlist":
                self._api_watchlist()
            elif path == "/api/schedule":
                self._api_schedule()
            elif path == "/api/seeding":
                self._api_seeding(save=True)
            elif path == "/api/seeding/preview":
                self._api_seeding(save=False)
            elif path == "/api/run-now":
                self._api_run_now()
            elif path == "/api/restore":
                self._api_restore()
            elif path == "/api/connections/test":
                self._api_connection(save=False)
            elif path == "/api/connections":
                self._api_connection(save=True)
            elif path == "/api/adopt/preview":
                self._api_adopt(preview=True)
            elif path == "/api/adopt":
                self._api_adopt(preview=False)
            elif path == "/api/protect-collections":
                self._api_protect_collections()
            elif path == "/api/tidy/preview":
                self._api_tidy(preview=True)
            elif path == "/api/tidy":
                self._api_tidy(preview=False)
            elif path == "/api/language":
                self._api_language()
            else:
                self._send({"erreur": t("route inconnue")}, 404)
        except Exception as error:  # noqa: BLE001
            self._send({"erreur": f"{type(error).__name__}: {error}"}, 500)

    def _serve_asset(self, name: str, set_cookie: bool = False) -> None:
        target = (ASSETS / name).resolve()
        if not target.is_file() or ASSETS.resolve() not in target.parents:
            self._send({"erreur": t("introuvable")}, 404)
            return
        body = target.read_bytes()
        if name.endswith(".html"):
            # the page is served in the instance's language: the UI reads it
            # from <html lang> before its first render
            body = body.replace(b'<html lang="fr"', f'<html lang="{i18n.language()}"'.encode(), 1)
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        extra = []
        if set_cookie:
            # behind a reverse proxy the request arrives over HTTPS: the cookie
            # is then marked Secure so it is never sent in clear text
            https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            flags = "; Secure" if https else ""
            extra.append(("Set-Cookie",
                f"sweeper_token={self.token}; Path=/; Max-Age=31536000"
                f"; SameSite=Lax; HttpOnly{flags}"))
        self._write(body, f"{mime}; charset=utf-8", 200, extra)

    def _api_state(self) -> None:
        state = self.state
        if not state.computed_at:
            try:
                state.refresh()
            except Exception:  # noqa: BLE001 — the error is in state.error; serve anyway
                pass
        self._send(state_payload(state))

    def _api_login(self) -> None:
        supplied = str(self._body().get("token", "")).strip()
        # 192 bits of entropy: brute force is not a threat, but the service
        # is on the Internet — so slow down repeated attempts anyway.
        with Handler._login_lock:
            if Handler._login_failures >= 5:
                time.sleep(min(Handler._login_failures - 4, 5))
        if not supplied or not secrets.compare_digest(supplied, self.token):
            with Handler._login_lock:
                Handler._login_failures += 1
            self._send({"erreur": t("Jeton refusé.")}, 401)
            return
        with Handler._login_lock:
            Handler._login_failures = 0
        https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        self._write(
            json.dumps({"ok": True}).encode("utf-8"), "application/json; charset=utf-8", 200,
            [("Set-Cookie",
              f"sweeper_token={self.token}; Path=/; Max-Age=31536000"
              f"; SameSite=Lax; HttpOnly{'; Secure' if https else ''}")],
        )

    def _api_history(self) -> None:
        path = self.state.config.state_dir / "history.jsonl"
        if not path.is_file():
            self._send([])
            return
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self._send(records[-200:][::-1])

    def _api_save_rules(self) -> None:
        payload = self._body()
        rules = [
            Rule(
                name=entry["name"],
                media=entry["media"],
                enabled=bool(entry.get("enabled", True)),
                never_watched=bool(entry.get("never_watched", False)),
                unwatched_days=entry.get("unwatched_days") or None,
                min_age_days=int(entry.get("min_age_days") or 0),
                max_distinct_viewers=entry.get("max_distinct_viewers"),
            )
            for entry in payload.get("règles", [])
        ]
        config_module.write_rules(self.state.config.config_path, rules)
        self.state.refresh()
        self._api_state()

    def _api_delete(self) -> None:
        payload = self._body()
        state = self.state
        if state.engine is None:
            state.refresh()
        plan = state.engine.plan_for_keys(
            state.inventory,
            payload.get("clés", []),
            force_seeding=bool(payload.get("forcer_seed", False)),
        )
        keys = [c.key for c in plan.candidates]

        def work(job) -> None:
            state.engine.execute(plan, progress=lambda r: _tick(job, r))
            for key in keys:
                state.watchlist.remove(key)
            state.watchlist.save()
            state.refresh()

        job = state.jobs.submit(
            "suppression", t("{n} élément(s)", n=len(plan.candidates)), len(plan.candidates), work
        )
        self._send(
            {
                "job": job.as_json(),
                "refusés": [
                    {"titre": c.title, "blocages": c.blockers} for c in plan.skipped
                ],
            }
        )

    def _api_delete_orphans(self) -> None:
        payload = self._body()
        keys = set(payload.get("clés", []))
        force = bool(payload.get("forcer_seed", False))
        state = self.state
        chosen = [g for g in state.orphans if g.key in keys]

        def work(job) -> None:
            state.engine.delete_orphan_groups(
                chosen, force=force, progress=lambda r: _tick(job, r)
            )
            for group in chosen:
                state.watchlist.remove(group.key)
            state.watchlist.save()
            state.refresh()

        job = state.jobs.submit(
            "seed-seul", t("{n} lot(s)", n=len(chosen)), len(chosen), work
        )
        self._send({"job": job.as_json()})

    def _api_preview_rules(self) -> None:
        payload = self._body()
        rules = [
            Rule(
                name=entry["name"],
                media=entry["media"],
                enabled=bool(entry.get("enabled", True)),
                never_watched=bool(entry.get("never_watched", False)),
                unwatched_days=entry.get("unwatched_days") or None,
                min_age_days=int(entry.get("min_age_days") or 0),
                max_distinct_viewers=entry.get("max_distinct_viewers"),
            )
            for entry in payload.get("règles", [])
        ]
        state = self.state
        if state.engine is None:
            state.refresh()
        self._send({"aperçu": state.engine.preview_rules(state.inventory, rules)})

    def _api_restore(self) -> None:
        """Bring back a deleted title, from what the log kept."""
        body = self._body()
        wanted_id = body.get("id")
        stamp = int(body.get("horodatage", 0) or 0)
        title = body.get("titre")
        path = self.state.config.state_dir / "history.jsonl"
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if path.is_file() else []
        # the ID first; for entries older than its introduction, timestamp AND
        # title — several deletions can share the same second
        record = next((r for r in records if wanted_id and r.get("id") == wanted_id), None)
        if record is None and not wanted_id:
            matches = [r for r in records if r.get("horodatage") == stamp
                       and (title is None or r.get("titre") == title)]
            record = matches[0] if len(matches) == 1 else None
            if len(matches) > 1:
                self._send({"erreur": t("entrée ambiguë : plusieurs suppressions à la même seconde")}, 409)
                return
        if record is None:
            self._send({"erreur": t("entrée de journal introuvable")}, 404)
            return
        external = record.get("identifiant_externe")
        if not external:
            self._send(
                {"erreur": t("cette entrée est antérieure au journal détaillé, "
                             "l'identifiant n'a pas été conservé")},
                400,
            )
            return
        radarr, sonarr, _seerr, _qbit = self.state.services()
        if record["type"] == "movie":
            result = radarr.restore_movie(external)
            label = result.get("title", record["titre"])
        else:
            result = sonarr.restore_series(external, record.get("saison"))
            label = result.get("title", record["titre"])
        self.state.refresh()
        self._send({"ok": True, "titre": label})

    def _resolve_adoption(self, candidate, radarr, sonarr) -> dict:
        """Find what Radarr/Sonarr would match the item to, without writing anything.

        An external ID known to Plex gives a reliable match; failing that, search
        by title and use the year as a tie-breaker. A film sitting bare at the
        root cannot be adopted in place: Radarr requires a folder.
        """
        # The folder to hand over is the first level under the library root:
        # "Tv Show/Some Series", not "Tv Show/Some Series/Season 1" (Sonarr
        # wants the series root), and likewise for a film whose file is nested.
        # A file sitting bare at the root has no folder at all: Radarr cannot
        # adopt it in place.
        roots = self.state.engine._library_roots
        folders: set[str] = set()
        season_folders = False
        for file in candidate.files:
            root = next((r for r in roots if file.startswith(r + "/")), None)
            if root is None:
                continue
            parts = Path(file).relative_to(root).parts
            if len(parts) == 1:
                return {
                    "clé": candidate.key, "titre": candidate.title, "possible": False,
                    "raison": t("fichier nu à la racine — Radarr exige un dossier par film ; "
                                "déplace-le dans un dossier à son nom d'abord"),
                    "nu": True,
                }
            folders.add(str(Path(root) / parts[0]))
            season_folders = season_folders or len(parts) > 2
        if len(folders) != 1:
            return {
                "clé": candidate.key, "titre": candidate.title, "possible": False,
                "raison": t("fichiers répartis dans {n} dossiers : impossible "
                            "de désigner un dossier unique à adopter", n=len(folders)),
            }
        folder = folders.pop()
        # An ID is proof; a title is only a hint. Try every ID Plex knows, in
        # the most reliable order for the target app, and fall back to the
        # title only as a last resort — requiring the year to match. Adopting
        # the wrong series would import episodes under someone else's entry:
        # better to refuse and say so.
        ids = candidate.plex_ids or {}
        lookup = radarr.lookup_movie if candidate.kind == "movie" else sonarr.lookup_series
        order = ("tmdb", "imdb") if candidate.kind == "movie" else ("tvdb", "tmdb", "imdb")
        match, via, sure = None, None, False
        for source in order:
            if not ids.get(source):
                continue
            try:
                results = lookup(f"{source}:{ids[source]}")
            except Exception:  # noqa: BLE001 — an unsupported prefix is not fatal
                results = []
            if results:
                match, via, sure = results[0], t("identifiant {source} connu de Plex", source=source), True
                break
        if match is None:
            results = lookup(candidate.title)
            same_year = [r for r in results if candidate.year and r.get("year") == candidate.year]
            if same_year:
                match, via = same_year[0], t("titre, année concordante")
            else:
                known = ", ".join(f"{k} {v}" for k, v in ids.items() if k in order)
                app = "Radarr" if candidate.kind == "movie" else "Sonarr"
                source = "TMDB" if candidate.kind == "movie" else "TheTVDB"
                if known:
                    # Plex has IDs but the app can't resolve them: the title doesn't
                    # exist in its metadata database, so it will never be able to manage it.
                    reason = t("{app} ne connaît pas ce titre : Plex a bien {known}, mais aucune "
                               "recherche ne le trouve — il n'est probablement pas référencé sur "
                               "{source}, dont {app} dépend. À supprimer depuis Plex si besoin.",
                               app=app, known=known, source=source)
                elif results:
                    first = results[0]
                    reason = t("aucun identifiant ; la recherche par titre propose « {title} "
                               "({year}) » mais l'année ne concorde pas avec Plex "
                               "({plex_year}) — à adopter à la main",
                               title=first.get("title"), year=first.get("year"),
                               plex_year=candidate.year or "?")
                else:
                    reason = t("aucune correspondance trouvée")
                return {"clé": candidate.key, "titre": candidate.title, "possible": False,
                        "raison": reason}
        return {
            "clé": candidate.key, "titre": candidate.title, "possible": True,
            "dossier": folder,
            "dossiers_saison": season_folders,
            "correspondance": {
                "titre": match.get("title"), "année": match.get("year"),
                "tmdb": match.get("tmdbId"), "tvdb": match.get("tvdbId"),
                "via": via,
                "sûre": sure,
            },
            "_match": match,
        }

    def _api_adopt(self, preview: bool) -> None:
        keys = set(self._body().get("clés", []))
        state = self.state
        radarr, sonarr, _seerr, _qbit = state.services()
        chosen = [c for c in state.inventory if c.key in keys and c.unmanaged]
        resolved = [self._resolve_adoption(c, radarr, sonarr) for c in chosen]
        if preview:
            self._send({"aperçu": [{k: v for k, v in r.items() if k != "_match"} for r in resolved]})
            return
        by_key = {c.key: c for c in chosen}
        doable = [e for e in resolved if e["possible"]]
        refused = [{"titre": e["titre"], "raison": e["raison"]} for e in resolved if not e["possible"]]

        def work(job) -> None:
            for entry in doable:
                candidate = by_key[entry["clé"]]
                record = {"titre": entry["titre"], "octets_libérés": 0, "étapes": [], "erreurs": []}
                try:
                    if candidate.kind == "movie":
                        radarr.adopt_movie(entry["_match"], entry["dossier"])
                        record["étapes"].append(t("Radarr : ajouté en place, scan du dossier lancé"))
                    else:
                        sonarr.adopt_series(entry["_match"], entry["dossier"],
                                            season_folders=entry.get("dossiers_saison", True))
                        record["étapes"].append(t("Sonarr : ajoutée en place, scan du dossier lancé"))
                except Exception as error:  # noqa: BLE001
                    record["erreurs"].append(f"{type(error).__name__}: {error}")
                _tick(job, record)
            state.refresh()

        job = state.jobs.submit("adoption", t("{n} élément(s)", n=len(doable)), len(doable), work)
        self._send({"job": job.as_json(), "refusés": refused})

    def _api_tidy(self, preview: bool) -> None:
        """Move films sitting bare at the root into a folder — see sweeper.tidy."""
        from . import tidy as tidy_module

        keys = set(self._body().get("clés", []))
        state = self.state
        roots = state.engine._library_roots
        chosen = [c for c in state.inventory if c.key in keys and c.unmanaged and c.kind == "movie"]
        plans = [(c, tidy_module.plan(Path(c.files[0]), roots)) for c in chosen]
        if preview:
            self._send({"aperçu": [
                {"clé": c.key, "titre": c.title, "possible": pl.possible,
                 "dossier": str(pl.target_dir), "raison": pl.refusal,
                 "fichiers": [src.name for src, _ in pl.moves]}
                for c, pl in plans
            ]})
            return
        doable = [(c, pl) for c, pl in plans if pl.possible]
        refused = [{"titre": c.title, "raison": pl.refusal} for c, pl in plans if not pl.possible]

        def work(job) -> None:
            for c, pl in doable:
                record = {"titre": c.title, "octets_libérés": 0, "étapes": [], "erreurs": []}
                try:
                    tidy_module.apply(pl)
                    record["étapes"].append(t("Disque : rangé dans {folder}/", folder=pl.target_dir.name))
                except Exception as error:  # noqa: BLE001
                    record["erreurs"].append(f"{type(error).__name__}: {error}")
                _tick(job, record)
            state.refresh()

        job = state.jobs.submit("rangement", t("{n} film(s)", n=len(doable)), len(doable), work)
        self._send({"job": job.as_json(), "refusés": refused})

    def _api_language(self) -> None:
        """Switch the instance's language, then recompute the texts of the plan."""
        language = self._body().get("langue")
        if language not in i18n.LANGUAGES:
            self._send({"erreur": t("langue inconnue : {language}", language=language)}, 400)
            return
        if os.environ.get("SWEEPER_LANG"):
            self._send({"erreur": t("défini par variable d'environnement, non modifiable ici : {fields}",
                                    fields="SWEEPER_LANG")}, 409)
            return
        state = self.state
        config_module.write_language(state.config.config_path, language)
        state.config.language = language
        i18n.set_language(language)
        try:
            state.refresh()
        except Exception:  # noqa: BLE001 — the setting is saved; the banner will say what failed
            pass
        self._send({"ok": True, "langue": language})

    def _api_protect_collections(self) -> None:
        names = [str(n) for n in self._body().get("collections", [])]
        protect = self.state.config.protect
        protect.collections = names
        config_module.write_protect(self.state.config.config_path, protect)
        self.state.refresh()
        self._send(state_payload(self.state))

    FIELDS = {
        "radarr": ("enabled", "url", "api_key"), "sonarr": ("enabled", "url", "api_key"),
        "seerr": ("enabled", "url", "api_key"),
        "qbittorrent": ("enabled", "url", "username", "password"), "plex": ("database",),
    }
    SECRETS = ("api_key", "password")

    def _connection_values(self, name: str, body: dict) -> dict:
        """What the user entered, ready to merge into [services.<name>].

        A secret left empty keeps the one already saved: the UI never displays
        it again, so it cannot send it back.
        """
        values = {}
        for key in self.FIELDS[name]:
            if key not in body:
                continue
            value = body[key]
            if key == "enabled":
                values[key] = bool(value)
            elif isinstance(value, str):
                value = value.strip()
                if key in self.SECRETS and not value:
                    continue
                if key == "url" and value and not value.startswith(("http://", "https://")):
                    raise ValueError(t("l'URL doit commencer par http:// ou https://"))
                values[key] = value or None
        return values

    def _api_connection(self, save: bool) -> None:
        import dataclasses

        from . import services as services_module

        body = self._body()
        name = body.get("service")
        if name not in self.FIELDS:
            self._send({"erreur": t("service inconnu : {name}", name=name)}, 400)
            return
        cfg = self.state.config
        try:
            values = self._connection_values(name, body)
        except ValueError as error:
            self._send({"erreur": str(error)}, 400)
            return
        locked = [k for k in values if (name, k) in cfg.env_fields]
        if locked:
            self._send({"erreur": t("défini par variable d'environnement, non modifiable ici : {fields}", fields=", ".join(locked))}, 409)
            return
        # probe with the candidate configuration, without writing anything
        services = {k: dict(v) for k, v in cfg.services.items()}
        merged = {**services.get(name, {}), **{k: v for k, v in values.items() if v is not None}}
        for k, v in values.items():
            if v is None:
                merged.pop(k, None)
        services[name] = merged
        candidate = dataclasses.replace(
            cfg, services=services,
            plex_db=Path(merged["database"]).expanduser() if name == "plex" and merged.get("database") else cfg.plex_db,
        )
        result = services_module.probe(candidate, name)
        if not save:
            self._send({"test": result})
            return
        if not result["ok"] and not body.get("forcer"):
            self._send({"erreur": t("test échoué, rien n'est enregistré : {detail}", detail=result["détail"]), "test": result}, 400)
            return
        config_module.write_service(cfg.config_path, name, values)
        try:
            self.state.refresh()
        except Exception:  # noqa: BLE001
            pass
        self._send(state_payload(self.state))

    def _api_job(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job_id = query.get("id", [None])[0]
        job = self.state.jobs.get(job_id) if job_id else self.state.jobs.current()
        self._send(job.as_json() if job else {"job": None})

    def _api_watchlist(self) -> None:
        payload = self._body()
        state = self.state
        action = payload.get("action", "add")
        kind = payload.get("kind", "media")
        index = (
            {c.key: (c.title, c.freed_bytes or c.library_bytes) for c in state.inventory}
            if kind == "media"
            else {g.key: (g.name, g.bytes) for g in state.orphans}
        )
        for key in payload.get("clés", []):
            if action == "remove":
                state.watchlist.remove(key)
            elif key in index:
                title, size = index[key]
                state.watchlist.add(key, kind, title, size)
        state.watchlist.save()
        self._send({"file_attente": state.watchlist.as_json()})

    def _api_schedule(self) -> None:
        payload = self._body()
        current = self.state.config.schedule
        schedule = Schedule(
            enabled=bool(payload.get("enabled", current.enabled)),
            interval_minutes=max(
                int(payload.get("interval_minutes", current.interval_minutes)), 15
            ),
            apply_rules=bool(payload.get("apply_rules", current.apply_rules)),
            apply_watchlist=bool(payload.get("apply_watchlist", current.apply_watchlist)),
        )
        config_module.write_schedule(self.state.config.config_path, schedule)
        self.state.config.schedule = schedule
        self._send({"planification": _schedule_json(schedule)})

    def _policy_from(self, payload: dict) -> SeedingPolicy:
        current = self.state.config.seeding
        per_tracker: dict[str, dict] = {}
        for name, override in (payload.get("par_tracker") or {}).items():
            cleaned = {
                key: float(value) if key == "min_ratio" else int(value)
                for key, value in override.items()
                if key in ("min_ratio", "min_seed_days") and value not in (None, "")
            }
            if cleaned:
                per_tracker[name] = cleaned
        return SeedingPolicy(
            mode=payload.get("mode", current.mode),
            min_ratio=float(payload.get("min_ratio", current.min_ratio)),
            min_seed_days=int(payload.get("min_seed_days", current.min_seed_days)),
            per_tracker=per_tracker,
        )

    def _api_seeding(self, save: bool) -> None:
        state = self.state
        policy = self._policy_from(self._body())
        if state.engine is None or state._last_link_map is None:
            state.refresh()
        simulation = state.engine.simulate_seeding(
            state.inventory, state.orphans, state._last_link_map, policy
        )
        if not save:
            self._send({"simulation": simulation})
            return
        config_module.write_seeding(state.config.config_path, policy)
        state.refresh()
        self._api_state()

    def _api_run_now(self) -> None:
        state = self.state
        job = state.jobs.submit("planifié", t("passage automatique"), 0, state.run_scheduled)
        self._send({"job": job.as_json()})

    def _api_protect(self) -> None:
        payload = self._body()
        keys = payload.get("clés", [])
        add = bool(payload.get("protéger", True))
        state = self.state
        label = state.config.protect.tags[0] if state.config.protect.tags else "keep"
        by_key = {c.key: c for c in state.inventory}
        movie_ids = [
            by_key[k].arr_id for k in keys if k in by_key and by_key[k].kind == "movie"
        ]
        series_ids = [
            by_key[k].arr_id for k in keys if k in by_key and by_key[k].kind != "movie"
        ]
        radarr, sonarr, _seerr, _qbit = state.services()
        if movie_ids:
            radarr.apply_tag(movie_ids, radarr.ensure_tag(label), add=add)
        if series_ids:
            sonarr.apply_tag(series_ids, sonarr.ensure_tag(label), add=add)
        state.refresh()
        self._send({"ok": True, "films": len(movie_ids), "séries": len(series_ids)})


def _scheduler_loop(state: State) -> None:
    """Wake up every minute; act only once the interval has elapsed.

    The work goes through the JobRunner, which serialises jobs: an automatic
    pass can therefore never land in the middle of a deletion started from the UI.
    """
    while True:
        time.sleep(60)
        try:
            schedule = state.config.schedule
            if not schedule.enabled:
                continue
            due = state.last_scheduled + schedule.interval_minutes * 60
            if time.time() < due:
                continue
            if state.jobs.current() is not None:
                continue
            state.jobs.submit(
                "planifié", t("passage automatique"), 0, state.run_scheduled
            )
        except Exception as error:  # noqa: BLE001 — the scheduler must never die
            print("  " + t("planificateur : {error}", error=f"{type(error).__name__}: {error}"))


def load_token(cfg: Config) -> str:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.state_dir / "token"
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    state = State(cfg)
    token = load_token(cfg)
    handler = type("BoundHandler", (Handler,), {"state": state, "token": token})
    host = host or cfg.web_host
    port = port or cfg.web_port

    print(t("Calcul de l'inventaire initial…"))
    try:
        state.refresh()
    except Exception as error:  # noqa: BLE001
        # first run or unreachable service: start anyway, the Connections
        # screen lets the user fix it
        print(f"  ⚠ {error}")
        print("  " + t("Ouvre l'interface : l'écran Connexions permet de configurer les services."))
    print("  " + t(
        "{items} éléments analysés, {batches} lots hors bibliothèque",
        items=len(state.inventory), batches=len(state.orphans),
    ))
    if state.watchlist.entries:
        print("  " + t("{n} élément(s) en file d'attente", n=len(state.watchlist.entries)))
    schedule = cfg.schedule
    print("  " + t("passage automatique : {when}", when=(
        t("toutes les {n} min", n=schedule.interval_minutes)
        if schedule.enabled
        else t("désactivé")
    )))
    threading.Thread(
        target=_scheduler_loop, args=(state,), name="sweeper-scheduler", daemon=True
    ).start()
    print()
    if host in ("127.0.0.1", "localhost", "::1"):
        print("  " + t("Écoute sur {host}:{port} uniquement.", host=host, port=port))
        print("  " + t("Depuis une autre machine, redirige le port par SSH :"))
        print()
        print(f"    ssh -N -L {port}:127.0.0.1:{port} <user>@<host>")
    else:
        # in a container, the port mapping decides who can reach it
        print("  " + t("Écoute sur {host}:{port}, protégé par le jeton.", host=host, port=port))
        print()
    print(f"    http://127.0.0.1:{port}/?token={token}")
    print()
    print("  " + t("Jeton mémorisé en cookie après la 1re ouverture, stocké dans"))
    print("  " + t("{path}. Ctrl-C pour arrêter.", path=cfg.state_dir / "token"))
    print()
    ThreadingHTTPServer((host, port), handler).serve_forever()
