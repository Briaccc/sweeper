"""Fake Radarr, Sonarr, Seerr and qBittorrent for the end-to-end test.

Each fake speaks HTTP like the real service on the routes sweeper uses, keeps
its state in memory, records every call it receives — and actually acts on the
temporary disk when the real one would (Radarr deleting a movie folder,
qBittorrent removing a torrent's files). That is what makes it possible to
verify a deletion end to end: the calls, the files, the bytes.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIDEO = (".mkv", ".mp4", ".avi")


class View:
    """How a service sees the disk. In "container" mode it reports paths from
    another world (/srv/media/…): sweeper must translate them via path_map,
    while the fake itself acts on the real test disk."""

    def __init__(self, local_root: Path | None = None, remote_root: str | None = None) -> None:
        self.local_root = str(local_root) if local_root else None
        self.remote_root = remote_root

    def remote(self, path) -> str:
        path = str(path)
        if self.remote_root and (path == self.local_root or path.startswith(self.local_root + "/")):
            return self.remote_root + path[len(self.local_root):]
        return path

    def local(self, path) -> str:
        path = str(path)
        if self.remote_root and (path == self.remote_root or path.startswith(self.remote_root + "/")):
            return self.local_root + path[len(self.remote_root):]
        return path


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fake:
    """Generic HTTP server: decodes the request, checks the key, delegates to route()."""

    api_key: str | None = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, object]] = []
        self.unknown: list[tuple[str, str]] = []   # calls no route serves
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _handle(self, method: str) -> None:
                url = urllib.parse.urlparse(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                if raw and "json" in ctype:
                    body = json.loads(raw)
                elif raw:
                    body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                else:
                    body = None
                with fake.lock:
                    fake.calls.append((method, url.path, query, body))
                    if fake.api_key and self.headers.get("X-Api-Key") != fake.api_key:
                        return self._reply(401, {"error": "clé d'API invalide"})
                    status, payload, extra = fake.route(method, url.path, query, body, self.headers)
                    if status == 404 and "route inconnue" in str(payload):
                        fake.unknown.append((method, url.path))
                self._reply(status, payload, extra)

            def _reply(self, status, payload, extra=None) -> None:
                if isinstance(payload, (dict, list)):
                    data, ctype = json.dumps(payload).encode(), "application/json"
                elif payload is None:
                    data, ctype = b"", "text/plain"
                else:
                    data, ctype = str(payload).encode(), "text/plain"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, method, path, query, body, headers):
        raise NotImplementedError

    def called(self, method: str, prefix: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == method and c[1].startswith(prefix)]

    def stop(self) -> None:
        self.server.shutdown()


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


# ------------------------------------------------------------------ Radarr ---

class FakeRadarr(Fake):
    api_key = "radarr-key"

    def __init__(self, root_folder: Path, view: View | None = None) -> None:
        self.root_folder = root_folder
        self.v = view or View()
        self.movies: dict[int, dict] = {}
        self.tags: list[dict] = []
        self.catalogue: dict[int, dict] = {}   # tmdbId -> record returned by lookup
        self.next_id = 100
        super().__init__()

    def add_movie(self, id, tmdb, title, year, folder: Path, file: Path, added: float, tags=()):
        self.movies[id] = {
            "id": id, "tmdbId": tmdb, "title": title, "year": year, "path": self.v.remote(folder),
            "movieFile": {"path": self.v.remote(file)}, "sizeOnDisk": file.stat().st_size,
            "added": iso(added), "tags": list(tags), "qualityProfileId": 1,
            "rootFolderPath": self.v.remote(self.root_folder),
        }
        self.catalogue[tmdb] = {"tmdbId": tmdb, "title": title, "year": year, "titleSlug": title.lower()}

    def route(self, method, path, query, body, headers):
        p = path.removeprefix("/api/v3/")
        if method == "GET" and p == "system/status":
            return 200, {"appName": "Radarr", "version": "6.0.0-fake"}, None
        if method == "GET" and p == "movie":
            return 200, list(self.movies.values()), None
        if method == "DELETE" and p.startswith("movie/"):
            movie = self.movies.pop(int(p.split("/")[1]), None)
            if movie is None:
                return 404, {"message": "introuvable"}, None
            if query.get("deleteFiles") == "true":
                _remove(Path(self.v.local(movie["path"])))   # the real Radarr deletes the folder
            return 200, None, None
        if method == "GET" and p == "tag":
            return 200, self.tags, None
        if method == "POST" and p == "tag":
            tag = {"id": len(self.tags) + 1, "label": body["label"]}
            self.tags.append(tag)
            return 201, tag, None
        if method == "PUT" and p == "movie/editor":
            for movie_id in body["movieIds"]:
                tags = set(self.movies[movie_id]["tags"])
                tags = tags | set(body["tags"]) if body["applyTags"] == "add" else tags - set(body["tags"])
                self.movies[movie_id]["tags"] = sorted(tags)
            return 202, None, None
        if method == "GET" and p == "rootfolder":
            return 200, [{"id": 1, "path": self.v.remote(self.root_folder)}], None
        if method == "GET" and p == "qualityprofile":
            return 200, [{"id": 1, "name": "HD-1080p"}], None
        if method == "GET" and p == "movie/lookup":
            term = query.get("term", "")
            if term.startswith("tmdb:"):
                hit = self.catalogue.get(int(term[5:]))
                return 200, [dict(hit)] if hit else [], None
            if term.startswith("imdb:"):
                return 200, [], None
            return 200, [dict(m) for m in self.catalogue.values() if term.lower() in m["title"].lower()], None
        if method == "POST" and p == "movie":
            self.next_id += 1
            movie = dict(body)
            movie["id"] = self.next_id
            remote_folder = movie.get("path") or f"{movie['rootFolderPath'].rstrip('/')}/{movie['title']}"
            folder = Path(self.v.local(remote_folder))
            video = next((f for f in sorted(folder.glob("*")) if f.suffix in VIDEO), None) if folder.is_dir() else None
            movie.update({
                "path": remote_folder, "added": iso(time.time()), "tags": [],
                "movieFile": {"path": self.v.remote(video)} if video else None,
                "sizeOnDisk": video.stat().st_size if video else 0,
            })
            self.movies[movie["id"]] = movie
            return 201, movie, None
        return 404, {"message": f"route inconnue {method} {path}"}, None


# ------------------------------------------------------------------ Sonarr ---

class FakeSonarr(Fake):
    api_key = "sonarr-key"

    def __init__(self, root_folder: Path, view: View | None = None) -> None:
        self.root_folder = root_folder
        self.v = view or View()
        self.series: dict[int, dict] = {}
        self.files: dict[int, dict] = {}
        self.tags: list[dict] = []
        self.catalogue: dict[int, dict] = {}   # tvdbId -> record
        self.next_id = 100
        self.next_file = 1000
        super().__init__()

    def add_series(self, id, tvdb, title, year, folder: Path, seasons: dict[int, list[Path]], added: float):
        self.series[id] = {
            "id": id, "tvdbId": tvdb, "title": title, "year": year, "path": self.v.remote(folder),
            "added": iso(added), "tags": [], "qualityProfileId": 1, "rootFolderPath": self.v.remote(self.root_folder),
            "seasons": [{"seasonNumber": n, "monitored": True} for n in sorted(seasons)],
        }
        for number, files in seasons.items():
            for f in files:
                self.next_file += 1
                self.files[self.next_file] = {"id": self.next_file, "seriesId": id, "seasonNumber": number,
                                              "path": self.v.remote(f), "size": f.stat().st_size}
        self.catalogue[tvdb] = {"tvdbId": tvdb, "title": title, "year": year,
                                "seasons": [{"seasonNumber": n, "monitored": True} for n in sorted(seasons)]}

    def _with_stats(self, series: dict) -> dict:
        files = [f for f in self.files.values() if f["seriesId"] == series["id"]]
        return {**series, "statistics": {"sizeOnDisk": sum(f["size"] for f in files),
                                         "episodeFileCount": len(files)}}

    def route(self, method, path, query, body, headers):
        p = path.removeprefix("/api/v3/")
        if method == "GET" and p == "system/status":
            return 200, {"appName": "Sonarr", "version": "4.0.0-fake"}, None
        if method == "GET" and p == "series":
            return 200, [self._with_stats(s) for s in self.series.values()], None
        if method == "GET" and p == "episodefile":
            sid = int(query["seriesId"])
            return 200, [f for f in self.files.values() if f["seriesId"] == sid], None
        if method == "DELETE" and p == "episodefile/bulk":
            for file_id in body["episodeFileIds"]:
                f = self.files.pop(file_id)
                _remove(Path(self.v.local(f["path"])))
            return 200, None, None
        if p.startswith("series/") and p.split("/")[1].isdigit():
            sid = int(p.split("/")[1])
            if sid not in self.series:
                return 404, {"message": "introuvable"}, None
            if method == "GET":
                return 200, self._with_stats(self.series[sid]), None
            if method == "PUT":
                self.series[sid]["seasons"] = body["seasons"]
                return 202, self._with_stats(self.series[sid]), None
            if method == "DELETE":
                series = self.series.pop(sid)
                for fid in [f["id"] for f in self.files.values() if f["seriesId"] == sid]:
                    self.files.pop(fid)
                if query.get("deleteFiles") == "true":
                    _remove(Path(self.v.local(series["path"])))
                return 200, None, None
        if method == "GET" and p == "tag":
            return 200, self.tags, None
        if method == "POST" and p == "tag":
            tag = {"id": len(self.tags) + 1, "label": body["label"]}
            self.tags.append(tag)
            return 201, tag, None
        if method == "PUT" and p == "series/editor":
            for sid in body["seriesIds"]:
                tags = set(self.series[sid]["tags"])
                tags = tags | set(body["tags"]) if body["applyTags"] == "add" else tags - set(body["tags"])
                self.series[sid]["tags"] = sorted(tags)
            return 202, None, None
        if method == "GET" and p == "rootfolder":
            return 200, [{"id": 1, "path": self.v.remote(self.root_folder)}], None
        if method == "GET" and p == "qualityprofile":
            return 200, [{"id": 1, "name": "HD-1080p"}], None
        if method == "GET" and p == "series/lookup":
            term = query.get("term", "")
            if term.startswith("tvdb:"):
                hit = self.catalogue.get(int(term[5:]))
                return 200, [json.loads(json.dumps(hit))] if hit else [], None
            if term.startswith(("tmdb:", "imdb:")):
                return 200, [], None
            return 200, [json.loads(json.dumps(s)) for s in self.catalogue.values() if term.lower() in s["title"].lower()], None
        if method == "POST" and p == "series":
            self.next_id += 1
            series = dict(body)
            # like the real one: without an explicit path, root folder + title
            series.setdefault("path", f"{series['rootFolderPath'].rstrip('/')}/{series['title']}")
            series.update({"id": self.next_id, "added": iso(time.time()), "tags": []})
            self.series[series["id"]] = series
            folder = Path(self.v.local(series["path"]))
            for f in sorted(folder.rglob("*")) if folder.is_dir() else []:
                if f.suffix in VIDEO:
                    number = next((int(part.split()[-1]) for part in f.relative_to(folder).parts[:-1]
                                   if part.lower().startswith("season")), 1)
                    self.next_file += 1
                    self.files[self.next_file] = {"id": self.next_file, "seriesId": series["id"],
                                                  "seasonNumber": number, "path": self.v.remote(f), "size": f.stat().st_size}
            return 201, self._with_stats(series), None
        return 404, {"message": f"route inconnue {method} {path}"}, None


# ------------------------------------------------------------------- Seerr ---

class FakeSeerr(Fake):
    api_key = "seerr-key"

    def __init__(self) -> None:
        self.media: dict[int, dict] = {}
        self.requests: dict[int, dict] = {}
        super().__init__()

    def add_media(self, id, kind, tmdb=None, tvdb=None, rating_key=None):
        self.media[id] = {"id": id, "mediaType": kind, "tmdbId": tmdb, "tvdbId": tvdb,
                          "status": 5, "ratingKey": str(rating_key) if rating_key else None}

    def add_request(self, id, media_id, who, created: float, seasons=()):
        m = self.media[media_id]
        self.requests[id] = {"id": id, "type": m["mediaType"], "status": 2, "createdAt": iso(created),
                             "requestedBy": {"displayName": who},
                             "seasons": [{"seasonNumber": n} for n in seasons], "media": dict(m)}

    @staticmethod
    def _page(items, query):
        take, skip = int(query.get("take", 20)), int(query.get("skip", 0))
        return {"pageInfo": {"results": len(items)}, "results": items[skip:skip + take]}

    def route(self, method, path, query, body, headers):
        p = path.removeprefix("/api/v1/")
        if method == "GET" and p == "status":
            return 200, {"version": "3.0.0-fake"}, None
        if method == "GET" and p == "media":
            return 200, self._page(list(self.media.values()), query), None
        if method == "GET" and p == "request":
            return 200, self._page(list(self.requests.values()), query), None
        if method == "DELETE" and p.startswith("request/"):
            return (204, None, None) if self.requests.pop(int(p.split("/")[1]), None) else (404, None, None)
        if method == "DELETE" and p.startswith("media/"):
            mid = int(p.split("/")[1])
            if self.media.pop(mid, None) is None:
                return 404, None, None
            for rid in [r["id"] for r in self.requests.values() if r["media"]["id"] == mid]:
                self.requests.pop(rid)
            return 204, None, None
        return 404, {"message": f"route inconnue {method} {path}"}, None


# ------------------------------------------------------------- qBittorrent ---

class FakeQbit(Fake):
    USER, PASSWORD, SID = "admin", "s3cret", "fake-sid"

    def __init__(self, view: View | None = None) -> None:
        self.v = view or View()
        self.torrents: dict[str, dict] = {}
        super().__init__()

    def add(self, hash, name, content: Path, save: Path, ratio, seed_days, tracker, category="radarr"):
        size = sum(f.stat().st_size for f in content.rglob("*") if f.is_file()) if content.is_dir() else content.stat().st_size
        self.torrents[hash] = {"hash": hash, "name": name, "category": category, "state": "stalledUP",
                               "size": size, "ratio": ratio, "seeding_time": int(seed_days * 86400),
                               "content_path": self.v.remote(content), "save_path": self.v.remote(save),
                               "tracker": f"https://{tracker}/announce"}

    def route(self, method, path, query, body, headers):
        p = path.removeprefix("/api/v2/")
        if method == "POST" and p == "auth/login":
            if body and body.get("username") == self.USER and body.get("password") == self.PASSWORD:
                return 200, "Ok.", {"Set-Cookie": f"SID={self.SID}; path=/"}
            return 200, "Fails.", None
        if f"SID={self.SID}" not in (headers.get("Cookie") or ""):
            return 403, "Forbidden", None
        if method == "GET" and p == "app/version":
            return 200, "v5.0.0-fake", None
        if method == "GET" and p == "torrents/info":
            return 200, list(self.torrents.values()), None
        if method == "POST" and p == "torrents/delete":
            for h in body["hashes"].split("|"):
                t = self.torrents.pop(h, None)
                if t and body.get("deleteFiles") == "true":
                    _remove(Path(self.v.local(t["content_path"])))
            return 200, None, None
        return 404, "route inconnue", None
