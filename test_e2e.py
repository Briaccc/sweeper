#!/usr/bin/env python3
"""End-to-end test, without touching any real data.

Fake Radarr, Sonarr, Seerr and qBittorrent (e2e_fakes.py), a fake Plex
database, and a real temporary disk with real hardlinks and cross-seeding.
Every CLI command and every API route is actually run against them —
deletions, seed-only removals, queue, automatic run, adoption, tidying,
restore — then we check the remaining files, the bytes actually freed, and
every call received by each service.

HOME is redirected to the temporary folder before importing sweeper: the
machine's real configuration (cross-seed, Plex, *arr) stays out of reach.
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="sweeper-e2e-"))
os.environ["HOME"] = str(TMP)                       # before any sweeper import
os.environ["SWEEPER_LANG"] = "fr"                   # messages are asserted in French
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import requests  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
import threading  # noqa: E402

from e2e_fakes import FakeQbit, FakeRadarr, FakeSeerr, FakeSonarr, View  # noqa: E402

DAY = 86400
NOW = time.time()
FAILURES: list[str] = []


def ago(days: float) -> float:
    return NOW - days * DAY


def check(label: str, condition, detail: str = "") -> None:
    print(f"  {'OK  ' if condition else 'ÉCHEC'} {label}{' — ' + str(detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
# Disk and Plex database
# --------------------------------------------------------------------------

ROOT = TMP / "data"
DL, MOV, TV, XS = ROOT / "Downloads", ROOT / "Movies", ROOT / "Tv Show", ROOT / "cross-seed-links"
PLEX_DB = TMP / "Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
for d in (DL, MOV, TV, XS):
    d.mkdir(parents=True)

# "Container" mode: each service reports paths from another world
# (/srv/media/…, nonexistent here); sweeper must translate everything via path_map.
REMOTE = bool(os.environ.get("E2E_REMOTE"))
VIEW = View(ROOT, "/srv/media") if REMOTE else View()


def put(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def link(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)
    return dst


def inodes() -> dict[tuple[int, int], int]:
    """Every file on the test disk: identity -> size."""
    out = {}
    for f in ROOT.rglob("*"):
        if f.is_file():
            st = f.stat()
            out[(st.st_dev, st.st_ino)] = st.st_size
    return out


class Plex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE metadata_items (id INTEGER PRIMARY KEY, guid TEXT, title TEXT, year INTEGER,
                added_at INTEGER, metadata_type INTEGER, parent_id INTEGER, "index" INTEGER,
                library_section_id INTEGER);
            CREATE TABLE media_items (id INTEGER PRIMARY KEY, metadata_item_id INTEGER);
            CREATE TABLE media_parts (id INTEGER PRIMARY KEY, media_item_id INTEGER, file TEXT, size INTEGER);
            CREATE TABLE metadata_item_views (id INTEGER PRIMARY KEY, account_id INTEGER, guid TEXT,
                metadata_type INTEGER, library_section_id INTEGER, grandparent_guid TEXT,
                parent_index INTEGER, "index" INTEGER, viewed_at INTEGER);
            CREATE TABLE metadata_item_settings (id INTEGER PRIMARY KEY, account_id INTEGER, guid TEXT,
                view_offset INTEGER, view_count INTEGER, last_viewed_at INTEGER);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, tag TEXT, tag_type INTEGER);
            CREATE TABLE taggings (id INTEGER PRIMARY KEY, metadata_item_id INTEGER, tag_id INTEGER);
        """)

    def _item(self, guid, title, year, added, mtype, parent=None, index=None) -> int:
        return self.db.execute(
            'INSERT INTO metadata_items (guid,title,year,added_at,metadata_type,parent_id,"index") VALUES (?,?,?,?,?,?,?)',
            (guid, title, year, int(added) if added else None, mtype, parent, index)).lastrowid

    def _file(self, item_id: int, path: Path) -> None:
        media = self.db.execute("INSERT INTO media_items (metadata_item_id) VALUES (?)", (item_id,)).lastrowid
        self.db.execute("INSERT INTO media_parts (media_item_id,file,size) VALUES (?,?,?)",
                        (media, VIEW.remote(path), path.stat().st_size))

    def movie(self, guid, title, year, file, added) -> int:
        item = self._item(guid, title, year, added, 1)
        self._file(item, file)
        return item

    def show(self, guid, title, year, added) -> int:
        return self._item(guid, title, year, added, 2)

    def season(self, show_id, n) -> int:
        return self._item(None, f"Saison {n}", None, None, 3, show_id, n)

    def episode(self, season_id, n, guid, file, added) -> int:
        item = self._item(guid, f"Épisode {n}", None, added, 4, season_id, n)
        self._file(item, file)
        return item

    def view_movie(self, guid, when, account=1) -> None:
        self.db.execute("INSERT INTO metadata_item_views (account_id,guid,metadata_type,viewed_at) VALUES (?,?,1,?)",
                        (account, guid, int(when)))

    def view_episode(self, show_guid, season, when, account=1) -> None:
        self.db.execute("INSERT INTO metadata_item_views (account_id,guid,metadata_type,grandparent_guid,parent_index,viewed_at) "
                        "VALUES (?,?,4,?,?,?)", (account, f"{show_guid}/s{season}", show_guid, season, int(when)))

    def resume(self, guid, when, account=2) -> None:
        self.db.execute("INSERT INTO metadata_item_settings (account_id,guid,view_offset,view_count,last_viewed_at) "
                        "VALUES (?,?,600000,0,?)", (account, guid, int(when)))

    def tag(self, item_id, tag, tag_type) -> None:
        row = self.db.execute("SELECT id FROM tags WHERE tag=? AND tag_type=?", (tag, tag_type)).fetchone()
        tag_id = row[0] if row else self.db.execute("INSERT INTO tags (tag,tag_type) VALUES (?,?)", (tag, tag_type)).lastrowid
        self.db.execute("INSERT INTO taggings (metadata_item_id,tag_id) VALUES (?,?)", (item_id, tag_id))

    def move_file(self, old: Path, new: Path) -> None:
        self.db.execute("UPDATE media_parts SET file=? WHERE file=?", (VIEW.remote(new), VIEW.remote(old)))

    def commit(self) -> None:
        self.db.commit()


# --------------------------------------------------------------------------
# Scenario: each case exercises a specific branch
# --------------------------------------------------------------------------

radarr, sonarr, seerr, qbit = FakeRadarr(MOV, VIEW), FakeSonarr(TV, VIEW), FakeSeerr(), FakeQbit(VIEW)
plex = Plex(PLEX_DB)
radarr.tags = [{"id": 1, "label": "keep"}]
F: dict[str, Path] = {}          # notable paths, for the checks


def movie_case(id, tmdb, title, year, size, added_days, ratio=2.0, seed_days=40, tags=(), crossseed=False):
    rel = f"{title.replace(' ', '.')}.{year}"
    src = put(DL / rel / "film.mkv", size)
    folder = MOV / f"{title} ({year})"
    lib = link(src, folder / "film.mkv")
    qbit.add(f"h_m{id}", rel, DL / rel, DL, ratio, seed_days, "tracker.alpha.org")
    F[f"m{id}_dl"], F[f"m{id}_lib"] = src, lib
    if crossseed:
        F[f"m{id}_xs"] = link(src, XS / "xs" / rel / "film.mkv")
        qbit.add(f"h_m{id}x", rel, XS / "xs" / rel, XS / "xs", 0.3, seed_days, "tk.beta.net", "cross-seed")
    radarr.add_movie(id, tmdb, title, year, folder, lib, ago(added_days), tags)
    return plex.movie(f"plex://movie/{tmdb}", title, year, lib, ago(added_days))


# M1 — never watched, 200 d, torrent + cross-seed, requested long ago      → TO DELETE
i1 = movie_case(1, 1001, "Film Jamais Vu", 2020, 5000, 200, crossseed=True)
seerr.add_media(101, "movie", tmdb=1001, rating_key=i1)
seerr.add_request(501, 101, "alice", ago(200))
# M2 — watched 150 d ago, but paused by someone                            → held: in progress
movie_case(2, 1002, "Film En Cours", 2019, 4100, 200)
plex.view_movie("plex://movie/1002", ago(150))
plex.resume("plex://movie/1002", ago(150))
# M3 — keep tag                                                            → held: tag
movie_case(3, 1003, "Film Tag Keep", 2018, 4200, 200, tags=[1])
# M4 — torrent that hasn't paid off (ratio 0.1, 5 d)                       → held: seeding, in 25 d
movie_case(4, 1004, "Film Seed Jeune", 2021, 4300, 200, ratio=0.1, seed_days=5)
# M5 — in a protected collection                                           → held: collection
i5 = movie_case(5, 1005, "Film Favori", 2017, 4400, 200)
plex.tag(i5, "Favoris", 2)
# M6 — requested 5 d ago                                                   → held: recent request
movie_case(6, 1006, "Film Demande", 2022, 4500, 200)
seerr.add_media(106, "movie", tmdb=1006)
seerr.add_request(506, 106, "bob", ago(5))
# M7 — watched yesterday: no rule applies                                  → neither candidate nor held
movie_case(7, 1007, "Film Vu Hier", 2023, 4600, 200)
plex.view_movie("plex://movie/1007", ago(1))

# S1 — never-watched series, season pack                                   → TO DELETE
d = DL / "Serie.Morte.S01"
e1, e2 = put(d / "e01.mkv", 3000), put(d / "e02.mkv", 3100)
l1, l2 = link(e1, TV / "Serie Morte/Season 01/e01.mkv"), link(e2, TV / "Serie Morte/Season 01/e02.mkv")
F.update(s1_dl=e1, s1_lib=l1)
qbit.add("h_s1", "Serie.Morte.S01", d, DL, 2.0, 40, "tracker.alpha.org", "tv-sonarr")
sonarr.add_series(10, 2001, "Série Morte", 2019, TV / "Serie Morte", {1: [l1, l2]}, ago(200))
sh = plex.show("plex://show/2001", "Série Morte", 2019, ago(200)); se = plex.season(sh, 1)
plex.episode(se, 1, "plex://episode/2001-1", l1, ago(200)); plex.episode(se, 2, "plex://episode/2001-2", l2, ago(200))
seerr.add_media(201, "tv", tvdb=2001)
seerr.add_request(601, 201, "carol", ago(200), seasons=[1])

# S2 — season 1 dormant (300 d), season 2 watched 5 d ago                  → TO DELETE: season 1 only
a1 = put(DL / "Serie.Longue.S01E01.mkv", 3200); a2 = put(DL / "Serie.Longue.S02E01.mkv", 3300)
b1 = link(a1, TV / "Serie Longue/Season 01/S01E01.mkv"); b2 = link(a2, TV / "Serie Longue/Season 02/S02E01.mkv")
F.update(s2s1_dl=a1, s2s1_lib=b1, s2s2_dl=a2, s2s2_lib=b2)
qbit.add("h_s2e1", "Serie.Longue.S01E01", a1, DL, 2.0, 400, "tracker.alpha.org", "tv-sonarr")
qbit.add("h_s2e2", "Serie.Longue.S02E01", a2, DL, 2.0, 400, "tracker.alpha.org", "tv-sonarr")
sonarr.add_series(11, 2002, "Série Longue", 2015, TV / "Serie Longue", {1: [b1], 2: [b2]}, ago(400))
sh = plex.show("plex://show/2002", "Série Longue", 2015, ago(400))
plex.episode(plex.season(sh, 1), 1, "plex://episode/2002-1", b1, ago(400))
plex.episode(plex.season(sh, 2), 1, "plex://episode/2002-2", b2, ago(400))
plex.view_episode("plex://show/2002", 1, ago(300)); plex.view_episode("plex://show/2002", 2, ago(5))
seerr.add_media(202, "tv", tvdb=2002)
seerr.add_request(602, 202, "dave", ago(400), seasons=[1]); seerr.add_request(603, 202, "dave", ago(400), seasons=[2])

# S3 — import by move: the torrent points into the library                 → held: only copy
c1 = put(TV / "Serie Import Direct/Season 01/a.mkv", 3400); c2 = put(TV / "Serie Import Direct/Season 02/b.mkv", 3500)
F.update(s3_a=c1, s3_b=c2)
qbit.add("h_s3", "Serie.Import.Direct", TV / "Serie Import Direct", TV, 2.0, 40, "tracker.alpha.org", "tv-sonarr")
sonarr.add_series(12, 2003, "Série Import Direct", 2016, TV / "Serie Import Direct", {1: [c1], 2: [c2]}, ago(400))
sh = plex.show("plex://show/2003", "Série Import Direct", 2016, ago(400))
plex.episode(plex.season(sh, 1), 1, "plex://episode/2003-1", c1, ago(400))
plex.episode(plex.season(sh, 2), 1, "plex://episode/2003-2", c2, ago(400))
plex.view_episode("plex://show/2003", 1, ago(300)); plex.view_episode("plex://show/2003", 2, ago(3))

# U1 — served by Plex, unknown to the *arrs, in a folder                   → outside *arr, adoptable
F["u1"] = put(MOV / "Vieux Doc/vieux.mkv", 2000)
iu1 = plex.movie("plex://movie/9001", "Vieux Doc", 2010, F["u1"], ago(200)); plex.tag(iu1, "tmdb://9001", 314)
radarr.catalogue[9001] = {"tmdbId": 9001, "title": "Vieux Doc", "year": 2010}
# U2 — bare file at the root, with subtitles                               → to tidy, then adoptable
F["u2"] = put(MOV / "Nu.mkv", 1500); F["u2_srt"] = put(MOV / "Nu.fr.srt", 10)
iu2 = plex.movie("plex://movie/9002", "Nu", 2011, F["u2"], ago(200)); plex.tag(iu2, "tmdb://9002", 314)
radarr.catalogue[9002] = {"tmdbId": 9002, "title": "Nu", "year": 2011}

# O1 — seed-only, obligations met                                          → removable
F["o1"] = put(DL / "Old.Release/old.mkv", 7000)
qbit.add("h_o1", "Old.Release", DL / "Old.Release", DL, 1.5, 60, "tracker.gamma.org")
# O2 — seed-only, 3 days of seeding                                        → in 27 d
F["o2"] = put(DL / "Young.Orphan/y.mkv", 2500)
qbit.add("h_o2", "Young.Orphan", DL / "Young.Orphan", DL, 0.0, 3, "tracker.gamma.org")
plex.commit()

# E2E_NO_MAP: negative control — foreign paths WITHOUT translation, everything must break
PATH_MAP = f'path_map = [["/srv/media", "{ROOT}"]]' if REMOTE and not os.environ.get("E2E_NO_MAP") else ""
CFG = TMP / "config.toml"
CFG.write_text(f"""
[services.radarr]
url = "{radarr.url}"
api_key = "radarr-key"
{PATH_MAP}
[services.sonarr]
url = "{sonarr.url}"
api_key = "sonarr-key"
{PATH_MAP}
[services.seerr]
url = "{seerr.url}"
api_key = "seerr-key"
[services.qbittorrent]
url = "{qbit.url}"
username = "admin"
password = "s3cret"
{PATH_MAP}
[services.plex]
{PATH_MAP}
[paths]
state_dir = "{TMP}/state"
[storage]
path = "{ROOT}"
[behaviour]
add_import_exclusion = false
delete_seerr_media = true
[web]
port = 29399
host = "127.0.0.1"

[[rule]]
name = "séries jamais lues"
media = "series"
never_watched = true
min_age_days = 45

[[rule]]
name = "films jamais vus"
media = "movie"
never_watched = true
min_age_days = 45

[[rule]]
name = "films dormants"
media = "movie"
unwatched_days = 120
min_age_days = 45

[[rule]]
name = "saisons dormantes"
media = "season"
unwatched_days = 240
min_age_days = 60

[seeding]
mode = "either"
min_ratio = 1.0
min_seed_days = 30

[protect]
tags = ["keep"]
titles = []
requested_within_days = 30
keep_torrent_if_shared = true
in_progress = true
collections = ["Favoris"]

[limits]
max_items_per_run = 25
max_gb_per_run = 400

[schedule]
enabled = false
interval_minutes = 360
apply_rules = false
apply_watchlist = true

[[tier]]
name = "petit"
gb = 1
price = 10
currency = "EUR"
""", encoding="utf-8")


def cli(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "sweeper.py"), *args, "--config", str(CFG), "--no-color"],
                          capture_output=True, text=True, timeout=timeout, env=os.environ.copy())


# ==========================================================================
print(f"Mode : {'conteneur — les services voient /srv/media, sweeper voit ' + str(ROOT) if REMOTE else 'direct — mêmes chemins partout'}"
      + (" — SANS correspondance de chemins (contrôle de sécurité)" if os.environ.get("E2E_NO_MAP") else ""))

if os.environ.get("E2E_NO_MAP"):
    # Misconfigured Docker setup: no service sees the same paths as sweeper.
    # Before the "files not found" safeguard, this scenario deleted a movie whose
    # seeding wasn't paid off, and an only copy. Nothing must go.
    section("Sécurité : chemins non traduits")
    r = cli("plan", "--json")
    blind = json.loads(r.stdout)
    check("rien de supprimable", blind["à_supprimer"] == [], [x["titre"] for x in blind["à_supprimer"]])
    reasons = {x["blocages"][0].split(" — ")[0] for x in blind["retenus"] if x["blocages"]}
    check("tout est retenu pour « fichiers introuvables »", "fichiers introuvables" in reasons, sorted(reasons))
    before = inodes()
    cli("run", "--execute", "--yes")
    check("run --execute : aucun fichier touché", inodes() == before)
    check("aucun appel de suppression reçu par Radarr, Sonarr ou qBittorrent",
          not radarr.called("DELETE", "/") and not sonarr.called("DELETE", "/") and not qbit.called("POST", "/api/v2/torrents/delete"))
    r = cli("check")
    check("check échoue et suggère path_map", r.returncode != 0 and "path_map" in r.stdout)
    for fake in (radarr, sonarr, seerr, qbit):
        fake.stop()
    print(f"\n{'Tout passe.' if not FAILURES else str(len(FAILURES)) + ' échec(s) : ' + ', '.join(FAILURES)}")
    sys.exit(1 if FAILURES else 0)

section("A. CLI contre les faux services")
# ==========================================================================

r = cli("check")
check("check : code 0", r.returncode == 0, r.stderr[-200:] if r.returncode else "")
for name in ("Radarr", "Sonarr", "Seerr", "qBittorrent", "Plex"):
    check(f"check : {name} joignable", any(line.strip().startswith(name) and " OK " in line for line in r.stdout.splitlines()))
check("check : base Plex trouvée par découverte automatique (plex_db absent de la config)", "films" in r.stdout)
check("check : racines déduites couvrent tous les torrents", "tous les torrents couverts" in r.stdout)

r = cli("plan", "--json")
check("plan --json : code 0", r.returncode == 0, r.stderr[-300:])
plan = json.loads(r.stdout) if r.returncode == 0 else {"à_supprimer": [], "retenus": []}
to_delete = {x["clé"] for x in plan["à_supprimer"]}
check("plan : exactement les 3 candidats attendus", to_delete == {"movie:1", "series:10", "season:11:1"}, sorted(to_delete))
held = {x["clé"]: x for x in plan["retenus"]}


def held_for(key: str, reason: str) -> bool:
    return key in held and any(b.startswith(reason) for b in held[key]["blocages"])


check("plan : M2 retenu — visionnage en cours", held_for("movie:2", "visionnage en cours"))
check("plan : M3 retenu — tag protégé", held_for("movie:3", "tag protégé"))
check("plan : M4 retenu — seed insuffisant", held_for("movie:4", "seed insuffisant"))
check("plan : M4 se débloque dans 25 j", held.get("movie:4", {}).get("dans_combien_de_jours") == 25,
      held.get("movie:4", {}).get("dans_combien_de_jours"))
check("plan : M5 retenu — collection protégée", held_for("movie:5", "collection protégée"))
check("plan : M6 retenu — demande Seerr récente", held_for("movie:6", "demande Seerr récente"))
check("plan : S3 saison 1 retenue — copie unique", held_for("season:12:1", "copie unique"))
check("plan : U1 et U2 retenus — hors *arr",
      held_for("plex:movie:plex://movie/9001", "hors *arr") and held_for("plex:movie:plex://movie/9002", "hors *arr"))
check("plan : M7 (vu hier) n'est ni candidat ni retenu", "movie:7" not in to_delete and "movie:7" not in held)
check("plan : S2 saison 2 (regardée) n'est ni candidate ni retenue", "season:11:2" not in to_delete and "season:11:2" not in held)
check("plan : octets annoncés = M1 + S1 + S2s1", plan.get("octets_libérés") == 5000 + 6100 + 3200, plan.get("octets_libérés"))

r = cli("run")
check("run sans --execute : simulation, rien ne bouge", r.returncode == 0 and "Simulation" in r.stdout and not radarr.called("DELETE", "/api/v3/movie"))

before = inodes()
r = cli("run", "--execute", "--yes")
after = inodes()
check("run --execute : code 0", r.returncode == 0, (r.stdout + r.stderr)[-300:])
check("run --execute : 3/3 traités", "3/3 traité(s)" in r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

gone = {k: v for k, v in before.items() if k not in after}
history = [json.loads(line) for line in (TMP / "state/history.jsonl").read_text().splitlines()]
check("octets réellement disparus du disque = octets annoncés", sum(gone.values()) == sum(h["octets_libérés"] for h in history),
      f"disque {sum(gone.values())} o, journal {sum(h['octets_libérés'] for h in history)} o")
check("M1 : les 3 noms (téléchargement, bibliothèque, cross-seed) ont disparu",
      not any(F[k].exists() for k in ("m1_dl", "m1_lib", "m1_xs")))
check("M1 : Radarr a reçu DELETE movie/1 avec deleteFiles=true",
      any(c[1] == "/api/v3/movie/1" and c[2].get("deleteFiles") == "true" for c in radarr.called("DELETE", "/api/v3/movie/")))
deleted_hashes = {h for c in qbit.called("POST", "/api/v2/torrents/delete") for h in c[3]["hashes"].split("|")}
check("M1 : qBittorrent a retiré le torrent ET son jumeau cross-seed", {"h_m1", "h_m1x"} <= deleted_hashes, sorted(deleted_hashes))
check("M1 : Seerr — request 501 et fiche 101 supprimées", 501 not in seerr.requests and 101 not in seerr.media)
check("S1 : fichiers bibliothèque + téléchargement disparus", not F["s1_dl"].exists() and not F["s1_lib"].exists())
check("S1 : Sonarr DELETE series/10", bool(sonarr.called("DELETE", "/api/v3/series/10")))
check("S1 : torrent retiré, request 601 et fiche 201 supprimées", "h_s1" not in qbit.torrents and 601 not in seerr.requests and 201 not in seerr.media)
check("S2 : saison 1 disparue, saison 2 intacte",
      not F["s2s1_lib"].exists() and not F["s2s1_dl"].exists() and F["s2s2_lib"].exists() and F["s2s2_dl"].exists())
check("S2 : Sonarr a supprimé les fichiers de la saison 1 en lot", bool(sonarr.called("DELETE", "/api/v3/episodefile/bulk")))
monitored = {s["seasonNumber"]: s["monitored"] for s in sonarr.series[11]["seasons"]}
check("S2 : saison 1 démonitorée, saison 2 toujours suivie", monitored == {1: False, 2: True}, monitored)
check("S2 : torrent de la saison 1 retiré, pas celui de la saison 2", "h_s2e1" not in qbit.torrents and "h_s2e2" in qbit.torrents)
check("S2 : request de la saison 1 supprimée, celle de la saison 2 et la fiche gardées",
      602 not in seerr.requests and 603 in seerr.requests and 202 in seerr.media)
check("rien d'autre n'a bougé (M2–M7, S3, U1, U2, O1, O2)",
      all(F[k].exists() for k in ("m2_lib", "m3_lib", "m4_lib", "m5_lib", "m6_lib", "m7_lib", "s3_a", "s3_b", "u1", "u2", "o1", "o2")))
check("journal : 3 entrées, sans erreur, avec identifiant externe et torrents",
      len(history) == 3 and all(not h["erreurs"] and h.get("identifiant_externe") for h in history),
      [(h["titre"], h["identifiant_externe"], len(h.get("torrents", []))) for h in history])

r = cli("history")
check("history : récapitulatif", r.returncode == 0 and "3 suppression(s)" in r.stdout)
r = cli("export", "--out", str(TMP / "export.tar.gz"))
check("export : archive écrite", r.returncode == 0 and (TMP / "export.tar.gz").stat().st_size > 0)
r = cli("init-tags")
check("init-tags : tag keep créé dans Sonarr, pas de doublon dans Radarr",
      r.returncode == 0 and any(t["label"] == "keep" for t in sonarr.tags) and len(radarr.tags) == 1)
fresh = TMP / "generated.toml"
r = subprocess.run([sys.executable, str(HERE / "sweeper.py"), "init", "--config", str(fresh)],
                   capture_output=True, text=True, timeout=120, env=os.environ.copy())
check("init : écrit un config.toml chargeable (aucun service trouvé dans ce HOME vide)", r.returncode == 0 and fresh.is_file())
r = cli_raw = subprocess.run([sys.executable, str(HERE / "sweeper.py"), "run", "--execute", "--yes", "--config", str(TMP / "faute-de-frappe.toml")],
                             capture_output=True, text=True, timeout=120, env=os.environ.copy())
check("--config vers un fichier absent : erreur, et aucun fichier créé", r.returncode != 0 and not (TMP / "faute-de-frappe.toml").exists(),
      (r.stderr.strip().splitlines() or ["?"])[-1][:90])
sys.path.insert(0, str(HERE))
from sweeper import config as config_module  # noqa: E402
try:
    config_module.load(fresh)
    check("init : le fichier généré se charge", True)
except Exception as error:  # noqa: BLE001
    check("init : le fichier généré se charge", False, error)

# ==========================================================================
section("B. API web contre les faux services")
# ==========================================================================

from sweeper import web  # noqa: E402

cfg = config_module.load(CFG)
state = web.State(cfg)
token = web.load_token(cfg)
handler = type("TestHandler", (web.Handler,), {"state": state, "token": token,
                                               "log_message": lambda self, *args: None})
server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"
state.refresh()

anon = requests.Session()
r = anon.get(f"{BASE}/")
check("sans jeton : / sert la page de connexion", r.status_code == 200 and "Jeton" in r.text)
check("sans jeton : /api/state refusé", anon.get(f"{BASE}/api/state").status_code == 401)
check("sans jeton : app.js non servi", "use strict" not in anon.get(f"{BASE}/assets/app.js").text)
check("mauvais jeton refusé", anon.post(f"{BASE}/api/login", json={"token": "faux"}).status_code == 401)
s = requests.Session()
r = s.post(f"{BASE}/api/login", json={"token": token})
check("bon jeton : cookie HttpOnly posé", r.status_code == 200 and "HttpOnly" in r.headers.get("Set-Cookie", ""))


def st() -> dict:
    return s.get(f"{BASE}/api/state").json()


def job(response) -> dict:
    """Follows a background job until it finishes."""
    data = response.json()
    j = data["job"]
    for _ in range(300):
        if j["terminé"]:
            return {**j, "refusés": data.get("refusés", [])}
        time.sleep(0.2)
        j = s.get(f"{BASE}/api/job", params={"id": j["id"]}).json()
    raise TimeoutError(j)


r = s.get(f"{BASE}/api/state", headers={"Accept-Encoding": "gzip"}, stream=True)
raw = r.raw.read()
check("état compressé en gzip", r.headers.get("Content-Encoding") == "gzip" and gzip.decompress(raw).startswith(b"{"))
check("en-têtes de sécurité (CSP, nosniff, DENY)", all(h in r.headers for h in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options")))
S0 = st()
check("état : aucune erreur de rafraîchissement", S0["erreur"] is None, S0["erreur"])
check("état : 4 services en bonne santé", all(x["ok"] for x in S0["santé"]["services"]) and len(S0["santé"]["services"]) == 4)
ghosts = [m["title"] for m in S0["médias"] if m["unmanaged"] and m["title"] not in ("Vieux Doc", "Nu")]
check("après suppression, aucun fantôme « hors *arr » (Plex n'a pas rescanné)", not ghosts, ghosts)
by_key = {m["key"]: m for m in S0["médias"]}
orph = {g["name"]: g for g in S0["orphelins"]}
check("seed seul : O1 prêt, O2 dans 27 j", orph.get("Old.Release", {}).get("seed_ok") is True
      and orph.get("Young.Orphan", {}).get("unblocks_in_days") == 27, {k: (v["seed_ok"], v["unblocks_in_days"]) for k, v in orph.items()})
check("couverture des hardlinks complète", S0["couverture_hardlinks"]["torrents"] == 0, S0["couverture_hardlinks"])


def ui_run(payload: dict, name: str, lang: str = "fr"):
    """Renders the UI on this payload in jsdom; returns (ok, last line)."""
    path = TMP / name
    path.write_text(json.dumps(payload, ensure_ascii=False))
    r = subprocess.run(["node", str(HERE / "test_webui.mjs"), str(path), "--lang", lang],
                       capture_output=True, text=True, timeout=120)
    lines = [x for x in r.stdout.splitlines() if "ÉCHEC" in x] or r.stdout.strip().splitlines() or [r.stderr[-200:]]
    return r.returncode == 0, lines[0][:110]


ok, last = ui_run(S0, "state-configured.json")
check("interface configurée (jsdom, français) : rendu complet, aucune erreur", ok, last)
ok, last = ui_run({**S0, "paliers": []}, "state-no-tiers.json")
check("sans [[tier]] : vue disque simple, aucun palier ni prix affiché", ok, last)
m4 = by_key["movie:4"]
check("M4 : blocage « seed » porté par un code, pas par la phrase", "seed" in m4["blocker_codes"]
      and m4["blockers"][0].startswith("seed insuffisant"), m4["blocker_codes"])
check("langue verrouillée par SWEEPER_LANG : changement refusé (409)",
      s.post(f"{BASE}/api/language", json={"langue": "en"}).status_code == 409)
os.environ.pop("SWEEPER_LANG")
r = s.post(f"{BASE}/api/language", json={"langue": "en"})
SE = st()
check("passer en anglais : enregistré dans [ui], état recalculé", r.status_code == 200 and SE["langue"] == "en"
      and 'language = "en"' in CFG.read_text())
check("… la page est servie en anglais", '<html lang="en"' in s.get(f"{BASE}/").text)
m4e = {m["key"]: m for m in SE["médias"]}["movie:4"]
check("… les blocages sont traduits, le code reste le même", m4e["blockers"][0].startswith("seeding not done")
      and m4e["blocker_codes"] == m4["blocker_codes"], m4e["blockers"][0][:50])
check("… une erreur d'API aussi", s.post(f"{BASE}/api/nope").json().get("erreur") == "unknown route")
ok, last = ui_run(SE, "state-configured-en.json", "en")
check("interface configurée (jsdom, anglais) : rendu complet, aucune chaîne française", ok, last)
check("langue inconnue refusée", s.post(f"{BASE}/api/language", json={"langue": "de"}).status_code == 400)
s.post(f"{BASE}/api/language", json={"langue": "fr"})
os.environ["SWEEPER_LANG"] = "fr"
check("retour au français", st()["langue"] == "fr" and '<html lang="fr"' in s.get(f"{BASE}/").text)

u1_key = "plex:movie:plex://movie/9001"
j = job(s.post(f"{BASE}/api/delete", json={"clés": [u1_key]}))
check("supprimer un hors *arr : refusé, aucun appel Radarr", j["total"] == 0 and j["refusés"] and F["u1"].exists())

j = job(s.post(f"{BASE}/api/delete", json={"clés": ["movie:4"]}))
check("M4 sans forcer : refusé (seed insuffisant)", j["total"] == 0 and F["m4_lib"].exists())
j = job(s.post(f"{BASE}/api/delete", json={"clés": ["movie:4"], "forcer_seed": True}))
check("M4 en forçant le seed : film, fichier de téléchargement ET torrent partis", j["faits"] == 1 and not F["m4_lib"].exists()
      and not F["m4_dl"].exists() and "h_m4" not in qbit.torrents, j)
check("… et les octets libérés sont réels (4 300 o, pas 0)", j["octets_libérés"] == 4300, j["octets_libérés"])

o1 = orph["Old.Release"]["key"]; o2 = orph["Young.Orphan"]["key"]
j = job(s.post(f"{BASE}/api/orphans/delete", json={"clés": [o1]}))
check("seed seul O1 retiré : fichier et torrent partis", j["faits"] == 1 and not F["o1"].exists() and "h_o1" not in qbit.torrents)
j = job(s.post(f"{BASE}/api/orphans/delete", json={"clés": [o2]}))
check("seed seul O2 sans forcer : refusé, fichier gardé", F["o2"].exists() and "h_o2" in qbit.torrents)

s.post(f"{BASE}/api/watchlist", json={"action": "add", "kind": "media", "clés": ["movie:7"]})
w = s.post(f"{BASE}/api/watchlist", json={"action": "add", "kind": "orphan", "clés": [o2]}).json()
check("file d'attente : 2 entrées", len(w["file_attente"]) == 2)
j = job(s.post(f"{BASE}/api/run-now", json={}))
check("passage : M7 (en file, sans règle, rien ne le retient) supprimé", not F["m7_lib"].exists()
      and any(c[1] == "/api/v3/movie/7" for c in radarr.called("DELETE", "/api/v3/movie/")), j)
check("passage : O2 (en file mais seed impayé) gardé", F["o2"].exists())
left = [e["key"] for e in st()["file_attente"]]
check("file d'attente : ne reste que O2", left == [o2], left)

s.post(f"{BASE}/api/protect", json={"clés": ["movie:6"], "protéger": True})
check("protéger M6 : tag keep posé dans Radarr", 1 in radarr.movies[6]["tags"])
check("… et l'état le voit", any(b.startswith("tag protégé") for b in {m["key"]: m for m in st()["médias"]}["movie:6"]["blockers"]))
s.post(f"{BASE}/api/protect", json={"clés": ["movie:6"], "protéger": False})
check("retirer la protection : tag enlevé", 1 not in radarr.movies[6]["tags"])

pv = s.post(f"{BASE}/api/adopt/preview", json={"clés": [u1_key]}).json()["aperçu"]
check("adopter U1 — aperçu : via l'identifiant tmdb", pv and pv[0]["possible"] and pv[0]["correspondance"]["via"].startswith("identifiant"), pv)
j = job(s.post(f"{BASE}/api/adopt", json={"clés": [u1_key]}))
posted = [c[3] for c in radarr.called("POST", "/api/v3/movie") if c[3].get("tmdbId") == 9001]
check("adoption : Radarr a reçu le dossier existant, dans SA vue des chemins, sans recherche",
      posted and posted[0]["path"] == VIEW.remote(MOV / "Vieux Doc")
      and posted[0]["addOptions"]["searchForMovie"] is False, posted[0] if posted else None)
S1 = st()
check("adoption : U1 n'est plus hors *arr, il est géré", not any(m["key"] == u1_key for m in S1["médias"])
      and any(m["title"].startswith("Vieux Doc") and not m["unmanaged"] for m in S1["médias"]))
check("adoption : fichier intact", F["u1"].exists())

u2_key = "plex:movie:plex://movie/9002"
ino_before = F["u2"].stat().st_ino
pv = s.post(f"{BASE}/api/tidy/preview", json={"clés": [u2_key]}).json()["aperçu"]
check("ranger U2 — aperçu : dossier « Nu »", pv and pv[0]["possible"] and pv[0]["dossier"] == str(MOV / "Nu"), pv)
job(s.post(f"{BASE}/api/tidy", json={"clés": [u2_key]}))
moved = MOV / "Nu" / "Nu.mkv"
check("rangement : fichier déplacé avec ses sous-titres, même inode",
      moved.is_file() and (MOV / "Nu" / "Nu.fr.srt").is_file() and not F["u2"].exists() and moved.stat().st_ino == ino_before)
plex.move_file(F["u2"], moved); plex.commit()          # what Plex's next scan would do
s.post(f"{BASE}/api/refresh")
pv = s.post(f"{BASE}/api/adopt/preview", json={"clés": [u2_key]}).json()["aperçu"]
check("après rescan Plex : U2 rangé devient adoptable", pv and pv[0]["possible"] and pv[0]["dossier"] == str(MOV / "Nu"), pv)

hist = s.get(f"{BASE}/api/history").json()
m1 = next(h for h in hist if h["titre"].startswith("Film Jamais Vu"))
same_second = [h for h in hist if h["horodatage"] == m1["horodatage"]]
check("le lot du CLI partage bien une seconde (le piège existe)", len(same_second) >= 2, len(same_second))
check("chaque entrée de journal a un identifiant unique", len({h.get("id") for h in hist}) == len(hist) and all(h.get("id") for h in hist))
series_posts_before = len(sonarr.called("POST", "/api/v3/series"))
r = s.post(f"{BASE}/api/restore", json={"id": m1["id"], "horodatage": m1["horodatage"], "titre": m1["titre"]})
restored = [c[3] for c in radarr.called("POST", "/api/v3/movie") if c[3].get("tmdbId") == 1001]
check("restauration de M1 : Radarr ré-ajoute tmdb 1001 et lance la recherche", r.status_code == 200 and restored
      and restored[0]["addOptions"]["searchForMovie"] is True, r.text[:120])
check("… et Sonarr n'a rien reçu (la série du même lot n'est pas ré-ajoutée)",
      len(sonarr.called("POST", "/api/v3/series")) == series_posts_before)
r = s.post(f"{BASE}/api/restore", json={"horodatage": m1["horodatage"]})
check("ancienne entrée sans id, horodatage ambigu : refus explicite (409), rien n'est ré-ajouté",
      r.status_code == 409 and len(sonarr.called("POST", "/api/v3/series")) == series_posts_before, r.status_code)

rules = S0["règles"]
pv = s.post(f"{BASE}/api/rules/preview", json={"règles": rules}).json()
check("aperçu des règles : une entrée par règle", len(pv["aperçu"]) == len(rules))
before_cfg = config_module.load(CFG)
s.post(f"{BASE}/api/rules", json={"règles": rules})
check("enregistrer les règles à l'identique : même ordre, mêmes réglages",
      [(r.name, r.media, r.enabled, r.never_watched, r.unwatched_days, r.min_age_days) for r in config_module.load(CFG).rules]
      == [(r.name, r.media, r.enabled, r.never_watched, r.unwatched_days, r.min_age_days) for r in before_cfg.rules])
sim = s.post(f"{BASE}/api/seeding/preview", json={"mode": "either", "min_ratio": 1.0, "min_seed_days": 30, "par_tracker": {}}).json()
check("simulation de seed", "libérable_maintenant" in sim.get("simulation", {}), sim)
s.post(f"{BASE}/api/seeding", json={"mode": "either", "min_ratio": 1.0, "min_seed_days": 30, "par_tracker": {"gamma": {"min_seed_days": 45}}})
reloaded = config_module.load(CFG).seeding
check("règle de seed par tracker enregistrée et relue", reloaded.per_tracker == {"gamma": {"min_seed_days": 45}}, reloaded.per_tracker)
check("… et appliquée : O2 attend désormais 42 j", next(g for g in st()["orphelins"] if g["key"] == o2)["unblocks_in_days"] == 42)
s.post(f"{BASE}/api/protect-collections", json={"collections": ["Favoris"]})
check("collections protégées relues", config_module.load(CFG).protect.collections == ["Favoris"])

# automatic run with "apply rules", Plex database auto-discovered
movie_case(9, 1009, "Film Auto", 2024, 4900, 200); plex.commit()
s.post(f"{BASE}/api/schedule", json={"enabled": False, "interval_minutes": 360, "apply_rules": True, "apply_watchlist": True})
s.post(f"{BASE}/api/refresh")
j = job(s.post(f"{BASE}/api/run-now", json={}))
check("passage automatique + règles (plex_db non configuré) : sans erreur", j["erreur"] is None, j["erreur"])
check("… Film Auto supprimé par la règle", not F["m9_lib"].exists() and "h_m9" not in qbit.torrents)
check("… les protégés n'ont pas bougé", all(F[k].exists() for k in ("m2_lib", "m3_lib", "m5_lib", "m6_lib", "s3_a", "s3_b")))
s.post(f"{BASE}/api/schedule", json={"enabled": False, "interval_minutes": 360, "apply_rules": False, "apply_watchlist": True})

S2 = st()
check("derniers travaux listés", len(S2["derniers_travaux"]) >= 6, len(S2["derniers_travaux"]))
check("travaux persistés sur disque", (TMP / "state/jobs.jsonl").read_text().count("\n") >= 6)
check("statistiques par règle", {"films jamais vus", "séries jamais lues", "saisons dormantes"} <= set(S2["stats_règles"]), sorted(S2["stats_règles"]))
all_history = [json.loads(line) for line in (TMP / "state/history.jsonl").read_text().splitlines()]
check("journal : aucune entrée en erreur", not [h for h in all_history if h["erreurs"] and h["type"] != "seed-seul"],
      [(h["titre"], h["erreurs"]) for h in all_history if h["erreurs"]])
server.shutdown()

# ==========================================================================
section("C. Commande serve réelle")
# ==========================================================================

port = socket.socket(); port.bind(("127.0.0.1", 0)); free = port.getsockname()[1]; port.close()
proc = subprocess.Popen([sys.executable, str(HERE / "sweeper.py"), "serve", "--config", str(CFG), "--port", str(free)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=os.environ.copy())
ok = False
for _ in range(120):
    try:
        ok = requests.get(f"http://127.0.0.1:{free}/", timeout=1).status_code == 200
        break
    except requests.RequestException:
        time.sleep(0.25)
proc.terminate()
check("serve démarre et répond", ok, proc.stderr.read().decode()[-200:] if not ok and proc.stderr else "")

# ==========================================================================
section("F. Chemins mal traduits : sweeper ne doit rien faire à l'aveugle")
# ==========================================================================

# Radarr reports its paths, but the mapping sends them nowhere:
# a misconfigured Docker setup. No movie must be deletable.
CFG3 = TMP / "config-blind.toml"
blind_map = f'path_map = [["{VIEW.remote(ROOT)}", "/nulle-part"]]'
text3 = CFG.read_text()
radarr_block = f'[services.radarr]\nurl = "{radarr.url}"\napi_key = "radarr-key"\n{PATH_MAP}'
assert radarr_block in text3
CFG3.write_text(text3.replace(radarr_block, f'[services.radarr]\nurl = "{radarr.url}"\napi_key = "radarr-key"\n{blind_map}'))
movie_case(10, 1010, "Film Aveugle", 2024, 4950, 200); plex.commit()   # obvious candidate if the paths were right
r = subprocess.run([sys.executable, str(HERE / "sweeper.py"), "plan", "--json", "--config", str(CFG3)],
                   capture_output=True, text=True, timeout=300, env=os.environ.copy())
blind_plan = json.loads(r.stdout)
films = [x for x in blind_plan["à_supprimer"] if x["type"] == "movie"]
check("aucun film supprimable quand sweeper ne voit pas leurs fichiers", films == [], [x["titre"] for x in films])
held3 = {x["clé"]: x for x in blind_plan["retenus"]}
check("les films gérés ne sont pas présentés à tort comme « hors *arr » (pas d'adoption en double)",
      not [k for k in held3 if k.startswith("plex:movie:plex://movie/10")], [k for k in held3 if k.startswith("plex:movie:")])
r = subprocess.run([sys.executable, str(HERE / "sweeper.py"), "check", "--no-color", "--config", str(CFG3)],
                   capture_output=True, text=True, timeout=300, env=os.environ.copy())
check("check signale les fichiers introuvables et échoue", r.returncode != 0 and "introuvable" in r.stdout)
r = subprocess.run([sys.executable, str(HERE / "sweeper.py"), "run", "--execute", "--yes", "--config", str(CFG3)],
                   capture_output=True, text=True, timeout=300, env=os.environ.copy())
check("run --execute dans cet état : Film Aveugle toujours là", F["m10_lib"].exists() and "h_m10" in qbit.torrents)
state3 = web.State(config_module.load(CFG3))
try:
    state3.refresh()
except Exception as error:  # noqa: BLE001
    print(f"      (refresh : {type(error).__name__}: {str(error)[:160]})")
blind_info = web.state_payload(state3)["fichiers_introuvables"]
check("l'interface reçoit le diagnostic « fichiers introuvables » avec un exemple",
      blind_info["éléments"] >= 1 and "/nulle-part/" in (blind_info["exemple"] or ""), blind_info)

# ==========================================================================
section("E. Premier lancement et écran Connexions")
# ==========================================================================

port = socket.socket(); port.bind(("127.0.0.1", 0)); dead = port.getsockname()[1]; port.close()
CFG2 = TMP / "config-first-run.toml"
CFG2.write_text(CFG.read_text().replace(f'url = "{radarr.url}"\napi_key = "radarr-key"',
                                        f'url = "http://127.0.0.1:{dead}"\napi_key = "mauvaise-cle"'))
cfg2 = config_module.load(CFG2)
state2 = web.State(cfg2)
try:
    state2.refresh()
except Exception:  # noqa: BLE001 — expected: Radarr unreachable
    pass
server2 = ThreadingHTTPServer(("127.0.0.1", 0), type("H2", (web.Handler,), {
    "state": state2, "token": token, "log_message": lambda self, *a: None}))
threading.Thread(target=server2.serve_forever, daemon=True).start()
B2 = f"http://127.0.0.1:{server2.server_address[1]}"
s2 = requests.Session(); s2.post(f"{B2}/api/login", json={"token": token})
first = s2.get(f"{B2}/api/state")
F1 = first.json()
conn = {c["service"]: c for c in F1["connexions"]}
check("Radarr injoignable : l'état est servi quand même (200), non configuré", first.status_code == 200 and F1["configuré"] is False)
check("… Radarr signalé injoignable, les autres connectés", not conn["radarr"]["ok"]
      and all(conn[n]["ok"] for n in ("sonarr", "qbittorrent", "seerr", "plex")), {n: c["ok"] for n, c in conn.items()})
(TMP / "state-first-run.json").write_text(json.dumps(F1, ensure_ascii=False))
ui = subprocess.run(["node", str(HERE / "test_webui.mjs"), str(TMP / "state-first-run.json")], capture_output=True, text=True, timeout=120)
check("interface au premier lancement (jsdom) : accueil, Connexions ouvertes, aucune erreur", ui.returncode == 0,
      ui.stdout.strip().splitlines()[-1] if ui.stdout.strip() else ui.stderr[-200:])
t = s2.post(f"{B2}/api/connections/test", json={"service": "radarr", "url": radarr.url, "api_key": "fausse"}).json()
check("Tester avec une mauvaise clé : échec expliqué", not t["test"]["ok"] and "401" in t["test"]["détail"], t["test"]["détail"][:60])
r = s2.post(f"{B2}/api/connections", json={"service": "radarr", "url": radarr.url, "api_key": "fausse"})
check("Enregistrer une configuration qui échoue : refusé, rien d'écrit", r.status_code == 400 and "fausse" not in CFG2.read_text())
t = s2.post(f"{B2}/api/connections/test", json={"service": "radarr", "url": radarr.url, "api_key": "radarr-key"}).json()
check("Tester avec la bonne clé : connecté", t["test"]["ok"], t["test"]["détail"])
r = s2.post(f"{B2}/api/connections", json={"service": "radarr", "url": radarr.url, "api_key": "radarr-key"})
check("Enregistrer : configuration écrite, sweeper configuré", r.status_code == 200 and r.json()["configuré"] is True)
check("… fichier de config en 600 (il contient des clés)", oct(CFG2.stat().st_mode & 0o777) == "0o600", oct(CFG2.stat().st_mode & 0o777))
full = json.dumps(s2.get(f"{B2}/api/state").json(), ensure_ascii=False)
check("aucun secret en clair dans l'état servi au navigateur",
      not any(secret in full for secret in ("radarr-key", "sonarr-key", "seerr-key", "s3cret")))
r = s2.post(f"{B2}/api/connections", json={"service": "sonarr", "url": sonarr.url})
check("Enregistrer une URL sans ressaisir la clé : la clé enregistrée est gardée", r.status_code == 200
      and config_module.load(CFG2).service("sonarr").get("api_key") == "sonarr-key")
os.environ["SWEEPER_SONARR_API_KEY"] = "sonarr-key"
s2.post(f"{B2}/api/refresh")
r = s2.post(f"{B2}/api/connections", json={"service": "sonarr", "api_key": "autre"})
check("un champ fourni par variable d'environnement n'est pas modifiable (409)", r.status_code == 409)
check("… et l'écran le signale comme tel", {c["service"]: c for c in s2.get(f"{B2}/api/state").json()["connexions"]}["sonarr"]["sources"]["api_key"] == "env")
del os.environ["SWEEPER_SONARR_API_KEY"]
r = s2.post(f"{B2}/api/connections", json={"service": "qbittorrent", "enabled": False})
F3 = r.json()
q = {c["service"]: c for c in F3["connexions"]}["qbittorrent"]
check("désactiver qBittorrent (setup Usenet) : sweeper reste configuré, sans torrent",
      r.status_code == 200 and F3["configuré"] and q["détail"] == "désactivé" and F3["orphelins"] == [], q["détail"])
r = s2.post(f"{B2}/api/connections", json={"service": "qbittorrent", "enabled": True})
check("le réactiver : identifiants conservés, reconnecté",
      r.status_code == 200 and {c["service"]: c for c in r.json()["connexions"]}["qbittorrent"]["ok"])
server2.shutdown()

# ==========================================================================
section("D. Contrat avec les services")
# ==========================================================================

for fake in (radarr, sonarr, seerr, qbit):
    check(f"{type(fake).__name__} : aucun appel vers une route inconnue", not fake.unknown, fake.unknown)
check("qBittorrent : toutes les requêtes passent par une session authentifiée",
      qbit.calls and qbit.calls[0][1] == "/api/v2/auth/login")

for fake in (radarr, sonarr, seerr, qbit):
    fake.stop()
print(f"\n{'Tout passe.' if not FAILURES else str(len(FAILURES)) + ' échec(s) : ' + ', '.join(FAILURES)}")
print(f"(dossier de test : {TMP})")
sys.exit(1 if FAILURES else 0)
