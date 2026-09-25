"""Watch statistics read straight from Plex's SQLite database.

The database is copied to a temporary file before reading: Plex is always
running, and we want neither to take a lock nor to read a half-written WAL.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import t

# Plex types: 1=movie, 2=show, 3=season, 4=episode
MOVIE, SHOW, SEASON, EPISODE = 1, 2, 3, 4
# Tag types: 2=collection, 314=external IDs (tmdb://, tvdb://, imdb://)
TAG_COLLECTION, TAG_EXTERNAL_ID = 2, 314


@dataclass
class WatchStats:
    last_viewed: int | None = None
    distinct_viewers: int = 0
    views: int = 0

    def merge(self, other: "WatchStats") -> "WatchStats":
        return WatchStats(
            last_viewed=max(
                (v for v in (self.last_viewed, other.last_viewed) if v), default=None
            ),
            distinct_viewers=max(self.distinct_viewers, other.distinct_viewers),
            views=self.views + other.views,
        )


@dataclass
class InProgress:
    """A playback started and never finished: the item is in "Continue Watching"."""

    accounts: int = 0
    last_seen: int | None = None


@dataclass
class PlexShow:
    guid: str
    title: str
    added_at: int
    stats: WatchStats
    in_progress: InProgress = field(default_factory=InProgress)
    external_ids: dict[str, str] = field(default_factory=dict)  # {"tvdb": "…", "tmdb": "…"}
    collections: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    year: int | None = None
    plex_id: int | None = None   # metadata_items.id = the ratingKey Seerr knows
    seasons: dict[int, WatchStats] = field(default_factory=dict)
    episode_paths: list[str] = field(default_factory=list)
    season_of_path: dict[str, int] = field(default_factory=dict)


@dataclass
class PlexMovie:
    guid: str
    title: str
    added_at: int
    size: int
    path: str
    stats: WatchStats
    in_progress: InProgress = field(default_factory=InProgress)
    external_ids: dict[str, str] = field(default_factory=dict)
    collections: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    year: int | None = None
    plex_id: int | None = None   # metadata_items.id = the ratingKey Seerr knows


@dataclass
class PlexSnapshot:
    movies_by_path: dict[str, PlexMovie]
    shows: list[PlexShow]
    episode_index: dict[str, tuple[str, int]]  # path -> (show guid, season)
    total_views: int
    collections: dict[str, int] = field(default_factory=dict)  # name -> member count
    additions_by_month: dict[str, int] = field(default_factory=dict)
    views_by_month: dict[str, int] = field(default_factory=dict)


def _connect(db_path: Path) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory(prefix="sweeper-plex-")
    copy = Path(tmp.name) / "library.db"
    shutil.copy2(db_path, copy)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, copy.with_name(copy.name + suffix))
    conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, tmp


def load_snapshot(db_path: Path, paths=None) -> PlexSnapshot:
    """`paths`: PathMap translating paths as Plex sees them into local paths."""
    local = paths.to_local if paths else (lambda p: p)
    if not db_path.is_file():
        raise FileNotFoundError(t("base Plex introuvable : {path}", path=db_path))
    conn, tmp = _connect(db_path)
    try:
        movie_views = _views_by_guid(conn, MOVIE)
        show_views, season_views = _episode_views(conn)
        resumable = _in_progress(conn)
        ext_ids = _tags_by_item(conn, TAG_EXTERNAL_ID)
        member_of = _tags_by_item(conn, TAG_COLLECTION)

        movies_by_path: dict[str, PlexMovie] = {}
        query = """
            SELECT mp.file AS file, mi.guid AS guid, mi.title AS title,
                   mi.added_at AS added_at, mp.size AS size,
                   mi.id AS item_id, mi.year AS year
            FROM metadata_items mi
            JOIN media_items med ON med.metadata_item_id = mi.id
            JOIN media_parts mp ON mp.media_item_id = med.id
            WHERE mi.metadata_type = ? AND mp.file IS NOT NULL
        """
        for row in conn.execute(query, (MOVIE,)):
            file = local(row["file"])
            movies_by_path[file] = PlexMovie(
                guid=row["guid"] or "",
                title=row["title"] or "",
                added_at=row["added_at"] or 0,
                size=row["size"] or 0,
                path=file,
                stats=movie_views.get(row["guid"], WatchStats()),
                in_progress=resumable.get(row["guid"], InProgress()),
                external_ids=_parse_external_ids(ext_ids.get(row["item_id"], [])),
                collections=member_of.get(row["item_id"], []),
                year=row["year"],
                plex_id=row["item_id"],
            )

        shows: dict[str, PlexShow] = {}
        episode_index: dict[str, tuple[str, int]] = {}
        query = """
            SELECT sh.guid AS show_guid, sh.title AS show_title,
                   sh.id AS show_id, sh.year AS show_year,
                   se."index" AS season_number, ep.added_at AS added_at,
                   ep.guid AS episode_guid, mp.file AS file
            FROM metadata_items sh
            JOIN metadata_items se ON se.parent_id = sh.id
            JOIN metadata_items ep ON ep.parent_id = se.id
            JOIN media_items med ON med.metadata_item_id = ep.id
            JOIN media_parts mp ON mp.media_item_id = med.id
            WHERE sh.metadata_type = ? AND mp.file IS NOT NULL
        """
        for row in conn.execute(query, (SHOW,)):
            file = local(row["file"])
            guid = row["show_guid"] or ""
            season = row["season_number"] if row["season_number"] is not None else -1
            show = shows.get(guid)
            if show is None:
                show = PlexShow(
                    guid=guid,
                    title=row["show_title"] or "",
                    added_at=row["added_at"] or 0,
                    stats=show_views.get(guid, WatchStats()),
                    external_ids=_parse_external_ids(ext_ids.get(row["show_id"], [])),
                    collections=member_of.get(row["show_id"], []),
                    year=row["show_year"],
                    plex_id=row["show_id"],
                )
                shows[guid] = show
            # a show's added_at = date of its most recently imported episode
            show.added_at = max(show.added_at, row["added_at"] or 0)
            show.seasons.setdefault(season, season_views.get((guid, season), WatchStats()))
            show.episode_paths.append(file)
            show.season_of_path[file] = season
            episode_index[file] = (guid, season)
            # a show is "in progress" as soon as a single episode is
            if episode := resumable.get(row["episode_guid"]):
                show.in_progress = InProgress(
                    accounts=max(show.in_progress.accounts, episode.accounts),
                    last_seen=max(
                        (v for v in (show.in_progress.last_seen, episode.last_seen) if v),
                        default=None,
                    ),
                )

        total = conn.execute("SELECT COUNT(*) FROM metadata_item_views").fetchone()[0]
        return PlexSnapshot(
            movies_by_path=movies_by_path,
            shows=list(shows.values()),
            episode_index=episode_index,
            total_views=total,
            collections=_collection_sizes(conn),
            additions_by_month=_additions_by_month(conn),
            views_by_month=_views_by_month(conn),
        )
    finally:
        conn.close()
        tmp.cleanup()


def _views_by_guid(conn: sqlite3.Connection, metadata_type: int) -> dict[str, WatchStats]:
    query = """
        SELECT guid, MAX(viewed_at) AS last_viewed,
               COUNT(DISTINCT account_id) AS viewers, COUNT(*) AS views
        FROM metadata_item_views
        WHERE metadata_type = ? AND guid IS NOT NULL
        GROUP BY guid
    """
    return {
        row["guid"]: WatchStats(row["last_viewed"], row["viewers"], row["views"])
        for row in conn.execute(query, (metadata_type,))
    }


def _episode_views(
    conn: sqlite3.Connection,
) -> tuple[dict[str, WatchStats], dict[tuple[str, int], WatchStats]]:
    """Aggregate episode history into views per show and per season."""
    by_show: dict[str, WatchStats] = {}
    by_season: dict[tuple[str, int], WatchStats] = {}
    query = """
        SELECT grandparent_guid AS show_guid, parent_index AS season_number,
               MAX(viewed_at) AS last_viewed,
               COUNT(DISTINCT account_id) AS viewers, COUNT(*) AS views
        FROM metadata_item_views
        WHERE metadata_type = ? AND grandparent_guid IS NOT NULL
        GROUP BY grandparent_guid, parent_index
    """
    for row in conn.execute(query, (EPISODE,)):
        guid = row["show_guid"]
        season = row["season_number"] if row["season_number"] is not None else -1
        stats = WatchStats(row["last_viewed"], row["viewers"], row["views"])
        by_season[(guid, season)] = stats
        by_show[guid] = by_show.get(guid, WatchStats()).merge(stats)
    return by_show, by_season


def _additions_by_month(conn: sqlite3.Connection) -> dict[str, int]:
    """Bytes imported per month — the measure of actual growth."""
    query = """
        SELECT strftime('%Y-%m', mi.added_at, 'unixepoch') AS mois,
               SUM(mp.size) AS octets
        FROM metadata_items mi
        JOIN media_items med ON med.metadata_item_id = mi.id
        JOIN media_parts mp ON mp.media_item_id = med.id
        WHERE mi.metadata_type IN (?, ?) AND mi.added_at IS NOT NULL
        GROUP BY mois ORDER BY mois
    """
    return {
        row["mois"]: row["octets"] or 0
        for row in conn.execute(query, (MOVIE, EPISODE))
        if row["mois"]
    }


def _views_by_month(conn: sqlite3.Connection) -> dict[str, int]:
    query = """
        SELECT strftime('%Y-%m', viewed_at, 'unixepoch') AS mois, COUNT(*) AS n
        FROM metadata_item_views WHERE viewed_at IS NOT NULL
        GROUP BY mois ORDER BY mois
    """
    return {row["mois"]: row["n"] for row in conn.execute(query) if row["mois"]}


def _in_progress(conn: sqlite3.Connection) -> dict[str, InProgress]:
    """What Plex shows in "Continue Watching".

    A non-zero `view_offset` means someone started and didn't finish. Deleting
    these titles yanks them out of their viewer's resume list, even if the last
    playback was months ago — which is precisely the case a "dormant" rule
    catches by mistake.
    """
    query = """
        SELECT guid, COUNT(DISTINCT account_id) AS accounts,
               MAX(last_viewed_at) AS last_seen
        FROM metadata_item_settings
        WHERE view_offset > 0 AND guid IS NOT NULL
        GROUP BY guid
    """
    return {
        row["guid"]: InProgress(accounts=row["accounts"], last_seen=row["last_seen"])
        for row in conn.execute(query)
    }


def _tags_by_item(conn: sqlite3.Connection, tag_type: int) -> dict[int, list[str]]:
    """Tags of a given type, grouped by item.

    Plex has no "tmdbId" column: external IDs, like collections, are tags linked
    through `taggings`. A single query per type covers the whole library.
    """
    query = """
        SELECT tg.metadata_item_id AS item_id, t.tag AS tag
        FROM taggings tg JOIN tags t ON t.id = tg.tag_id
        WHERE t.tag_type = ?
    """
    result: dict[int, list[str]] = {}
    for row in conn.execute(query, (tag_type,)):
        result.setdefault(row["item_id"], []).append(row["tag"])
    return result


def _parse_external_ids(tags: list[str]) -> dict[str, str]:
    """`tmdb://823754` -> {"tmdb": "823754"}."""
    ids: dict[str, str] = {}
    for tag in tags:
        source, _, value = tag.partition("://")
        if source and value and source not in ids:
            ids[source] = value
    return ids


def _collection_sizes(conn: sqlite3.Connection) -> dict[str, int]:
    query = """
        SELECT t.tag AS name, COUNT(DISTINCT tg.metadata_item_id) AS members
        FROM tags t JOIN taggings tg ON tg.tag_id = t.id
        WHERE t.tag_type = ? GROUP BY t.tag ORDER BY t.tag
    """
    return {row["name"]: row["members"] for row in conn.execute(query, (TAG_COLLECTION,))}
