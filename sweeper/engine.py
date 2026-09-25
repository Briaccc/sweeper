"""Building deletion candidates and executing the cleanup."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .arr import EpisodeFile, Radarr, Series, Sonarr
from .config import Config, Rule, SeedingPolicy
from .i18n import Reason, code_of, t
from .linkmap import LinkMap, TorrentImpact
from .plexdb import PlexShow, PlexSnapshot, WatchStats
from .qbit import QBittorrent, Torrent
from .seerr import Seerr, SeerrMedia, index_by_external_id, index_by_rating_key

DAY = 86400


def _arr_timestamp(value: str) -> int:
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


@dataclass
class Candidate:
    kind: str  # movie | series | season
    key: str
    title: str
    arr_id: int
    rule: str  # "" if no rule matches
    files: list[str]
    library_bytes: int
    added_at: int
    stats: WatchStats
    season: int | None = None
    external_id: int | None = None   # tmdbId for a film, tvdbId for a series
    in_progress: int = 0             # number of accounts that started without finishing
    #: Plex serves it but no *arr manages it. Sweeper never deletes a file
    #: itself: it must first be "adopted" into Radarr/Sonarr.
    unmanaged: bool = False
    plex_ids: dict = field(default_factory=dict)   # {"tmdb": 123, "tvdb": 456, …}
    collections: list[str] = field(default_factory=list)  # the item's Plex collections
    year: int | None = None
    plex_id: int | None = None
    episode_file_ids: list[int] = field(default_factory=list)
    impacts: list[TorrentImpact] = field(default_factory=list)
    seerr_media_id: int | None = None
    seerr_request_ids: list[int] = field(default_factory=list)
    seerr_media_deletable: bool = False
    blockers: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    #: days left until the seed obligations are met.
    #: None = not applicable, or unblocking not predictable (waiting on ratio).
    unblocks_in_days: int | None = None
    held_torrents: list[tuple[str, str, float, float]] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return bool(self.rule) and not self.blockers

    @property
    def eligible(self) -> bool:
        """A rule matches, even if a blocker prevents acting on it."""
        return bool(self.rule)

    @property
    def age_days(self) -> float:
        return (time.time() - self.added_at) / DAY if self.added_at else 0.0

    @property
    def idle_days(self) -> float | None:
        if not self.stats.last_viewed:
            return None
        return (time.time() - self.stats.last_viewed) / DAY

    @property
    def removable_torrents(self) -> list[Torrent]:
        return [i.torrent for i in self.impacts]

    @property
    def seed_blocked(self) -> bool:
        return any(code_of(b) == "seed" for b in self.blockers)


@dataclass
class Plan:
    candidates: list[Candidate]
    skipped: list[Candidate]
    scanned: dict[str, int]
    inventory: list[Candidate] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(c.freed_bytes for c in self.candidates)


class Sweeper:
    def __init__(
        self,
        config: Config,
        radarr: Radarr,
        sonarr: Sonarr,
        seerr: Seerr | None,
        qbit: QBittorrent,
    ) -> None:
        self.config = config
        self.radarr = radarr
        self.sonarr = sonarr
        self.seerr = seerr
        self.qbit = qbit
        self._episode_files: dict[int, list[EpisodeFile]] = {}
        self._scanned: dict[str, int] = {}
        self.seerr_inventory: dict[int, SeerrMedia] = {}
        self._library_roots: list[str] = []
        self._seerr_by_rating_key: dict = {}
        self._link_map: LinkMap | None = None
        self.unseen: list[str] = []   # files reported by an *arr but not visible here

    def episode_files(self, series_id: int) -> list[EpisodeFile]:
        if series_id not in self._episode_files:
            self._episode_files[series_id] = self.sonarr.episode_files(series_id)
        return self._episode_files[series_id]

    # ------------------------------------------------------------- evaluation

    def _matches(self, rule: Rule, stats: WatchStats, added_at: int) -> bool:
        if not rule.enabled:
            return False
        age_days = (time.time() - added_at) / DAY if added_at else 0.0
        if age_days < rule.min_age_days:
            return False
        if rule.never_watched:
            if stats.last_viewed:
                return False
        elif rule.unwatched_days is not None:
            if not stats.last_viewed:
                return False  # handled by a dedicated never_watched rule
            if (time.time() - stats.last_viewed) / DAY < rule.unwatched_days:
                return False
        if rule.max_distinct_viewers is not None:
            if stats.distinct_viewers > rule.max_distinct_viewers:
                return False
        return True

    def _first_rule(self, media: str, stats: WatchStats, added_at: int) -> Rule | None:
        for rule in self.config.rules:
            if rule.media == media and self._matches(rule, stats, added_at):
                return rule
        return None

    # ---------------------------------------------------------------- building

    def build_inventory(
        self, snapshot: PlexSnapshot, link_map: LinkMap
    ) -> list[Candidate]:
        """Evaluate the *whole* catalogue, rule or no rule.

        The interface also needs to show what no rule catches: that's where the
        user goes looking for things to delete by hand.
        """
        self.unseen = []
        movies = self.radarr.movies()
        series = self.sonarr.series()
        radarr_tags = self.radarr.tags()
        sonarr_tags = self.sonarr.tags()
        seerr_inventory: dict[int, SeerrMedia] = (
            self.seerr.inventory() if self.seerr else {}
        )
        seerr_movies, seerr_shows = index_by_external_id(seerr_inventory)
        self._seerr_by_rating_key = index_by_rating_key(seerr_inventory)
        self.seerr_inventory = seerr_inventory
        self._scanned = {
            "films": len(movies),
            "séries": len(series),
            "fiches_seerr": len(seerr_inventory),
        }

        inventory: list[Candidate] = []

        for movie in movies:
            if not movie.file_path:
                continue
            plex_movie = snapshot.movies_by_path.get(movie.file_path)
            if plex_movie is None:
                # Plex hasn't indexed this file (yet): we don't judge it. But if
                # sweeper can't even see it on disk, it's a path problem: count
                # it so it can be reported.
                if not os.path.isfile(movie.file_path):
                    self.unseen.append(movie.file_path)
                continue
            added_at = _arr_timestamp(movie.added)
            rule = self._first_rule("movie", plex_movie.stats, added_at)
            candidate = Candidate(
                kind="movie",
                key=f"movie:{movie.id}",
                title=f"{movie.title} ({movie.year})" if movie.year else movie.title,
                arr_id=movie.id,
                rule=rule.name if rule else "",
                files=[movie.file_path],
                library_bytes=movie.size or plex_movie.size,
                added_at=added_at,
                stats=plex_movie.stats,
                external_id=movie.tmdb_id,
                in_progress=plex_movie.in_progress.accounts,
                collections=list(plex_movie.collections),
                year=movie.year or plex_movie.year,
                plex_id=plex_movie.plex_id,
            )
            self._attach_protections(
                candidate, [radarr_tags.get(t, "") for t in movie.tag_ids]
            )
            self._attach_seerr(candidate, seerr_movies.get(movie.tmdb_id))
            inventory.append(candidate)

        show_by_series = _match_series_to_plex(series, snapshot)
        for item in series:
            show = show_by_series.get(item.id)
            if show is None:
                continue
            tags = [sonarr_tags.get(t, "") for t in item.tag_ids]
            seerr_media = seerr_shows.get(item.tvdb_id)
            added_at = _arr_timestamp(item.added)

            rule = self._first_rule("series", show.stats, added_at)
            files = self.episode_files(item.id)
            candidate = Candidate(
                kind="series",
                key=f"series:{item.id}",
                title=item.title,
                arr_id=item.id,
                rule=rule.name if rule else "",
                files=[f.path for f in files],
                library_bytes=item.size or sum(f.size for f in files),
                added_at=added_at,
                stats=show.stats,
                external_id=item.tvdb_id,
                in_progress=show.in_progress.accounts,
                collections=list(show.collections),
                year=show.year,
                plex_id=show.plex_id,
                episode_file_ids=[f.id for f in files],
            )
            self._attach_protections(candidate, tags)
            self._attach_seerr(candidate, seerr_media)
            inventory.append(candidate)

            # each season becomes an item in its own right: selectable by hand in
            # the library view even without a "season" rule. It is only
            # eligible (rule set) if a "season" rule targets it.
            inventory.extend(
                self._season_candidates(item, show, tags, seerr_media, added_at)
            )

        self._library_roots = list(link_map.library_roots)
        self._link_map = link_map
        inventory.extend(self._unmanaged_candidates(snapshot, link_map, movies, series))

        for candidate in inventory:
            self._check_visible(candidate, link_map)
            self._attach_torrents(candidate, link_map)
        return inventory

    @staticmethod
    def _check_visible(candidate: Candidate, link_map: LinkMap) -> None:
        """Refuse to act on files sweeper cannot see.

        This is the symptom of a missing path mapping (containers): the app
        reports /movies/…, and sweeper has nothing at that location. It then sees
        neither the hardlinks nor the torrents holding the file — its seed and
        single-copy safeguards go blind, whereas Radarr would delete anyway.
        """
        missing = [f for f in candidate.files
                   if f not in link_map.inode_by_path and not os.path.isfile(f)]
        if missing:
            candidate.blockers.insert(0, Reason("unseen", t(
                "fichiers introuvables — {n} fichier(s) que sweeper ne voit pas "
                "(ex. {example}) : correspondance de chemins (path_map) à configurer ?",
                n=len(missing), example=missing[0])))

    def _unmanaged_candidates(
        self, snapshot: PlexSnapshot, link_map: LinkMap, movies, series
    ) -> list[Candidate]:
        """What Plex serves but no *arr knows about.

        Manual additions, or series removed from Sonarr while keeping the files.
        No rule ever saw them, yet that is where the largest item in a library
        can sit forgotten. They are evaluated using Plex data alone, but never
        deleted: sweeper always delegates deletion to the app that manages the
        files. The "adopt" button hands them over to Radarr/Sonarr, after which
        the normal flow applies.
        """
        managed = {m.file_path for m in movies if m.file_path}
        for item in series:
            managed.update(f.path for f in self.episode_files(item.id))
        # An item that Plex links to an ID already known to Radarr or Sonarr is
        # managed: if the paths don't match, it's a missing path mapping, not an
        # orphan to "adopt".
        known_tmdb = {str(m.tmdb_id) for m in movies if m.tmdb_id}
        known_tvdb = {str(s.tvdb_id) for s in series if s.tvdb_id}

        def in_library(path: str) -> bool:
            return any(
                path == root or path.startswith(root + "/")
                for root in link_map.library_roots
            )

        def size_of(paths) -> int:
            return sum(
                link_map.size_by_inode.get(link_map.inode_by_path.get(p), 0)
                for p in paths
            )

        found: list[Candidate] = []
        for path, movie in snapshot.movies_by_path.items():
            # Plex keeps an item until its next scan: a file sweeper just
            # deleted is still listed there. Without this check, it would
            # immediately reappear as "outside *arr", the ghost of a file that
            # no longer exists.
            if path in managed or not in_library(path) or not os.path.isfile(path):
                continue
            if movie.external_ids.get("tmdb") in known_tmdb:
                continue
            rule = self._first_rule("movie", movie.stats, movie.added_at)
            found.append(
                Candidate(
                    kind="movie",
                    key=f"plex:movie:{movie.guid or path}",
                    title=movie.title or Path(path).stem,
                    arr_id=0,
                    rule=rule.name if rule else "",
                    files=[path],
                    library_bytes=movie.size or size_of([path]),
                    added_at=movie.added_at,
                    stats=movie.stats,
                    in_progress=movie.in_progress.accounts,
                    unmanaged=True,
                    plex_ids=dict(movie.external_ids),
                    collections=list(movie.collections),
                    year=movie.year,
                    plex_id=movie.plex_id,
                )
            )

        for show in snapshot.shows:
            paths = [p for p in show.episode_paths if in_library(p) and os.path.isfile(p)]
            if not paths or any(p in managed for p in paths):
                continue  # managed by Sonarr, at least partly: not our business
            if show.external_ids.get("tvdb") in known_tvdb:
                continue
            rule = self._first_rule("series", show.stats, show.added_at)
            found.append(
                Candidate(
                    kind="series",
                    key=f"plex:series:{show.guid}",
                    title=show.title,
                    arr_id=0,
                    rule=rule.name if rule else "",
                    files=paths,
                    library_bytes=size_of(paths),
                    added_at=show.added_at,
                    stats=show.stats,
                    in_progress=show.in_progress.accounts,
                    unmanaged=True,
                    plex_ids=dict(show.external_ids),
                    collections=list(show.collections),
                    year=show.year,
                    plex_id=show.plex_id,
                )
            )

        for candidate in found:
            self._attach_protections(candidate, [])
            # without tmdb/tvdb on the *arr side, the Plex ratingKey links the Seerr request
            self._attach_seerr(candidate, self._seerr_by_rating_key.get(candidate.plex_id))
            candidate.blockers.append(Reason("unmanaged", t(
                "hors *arr — à adopter dans Radarr/Sonarr avant toute suppression"
            )))
        return found

    def build_plan(self, snapshot: PlexSnapshot, link_map: LinkMap) -> Plan:
        inventory = self.build_inventory(snapshot, link_map)
        eligible = [c for c in inventory if c.eligible]
        candidates = [c for c in eligible if c.actionable]
        skipped = [c for c in eligible if not c.actionable]

        candidates.sort(key=lambda c: c.freed_bytes, reverse=True)
        candidates = self._apply_limits(candidates, skipped)
        return Plan(
            candidates=candidates,
            skipped=skipped,
            scanned=dict(self._scanned),
            inventory=inventory,
        )

    def plan_for_keys(
        self, inventory: list[Candidate], keys: list[str], force_seeding: bool = False
    ) -> Plan:
        """Plan built from a manual selection made in the interface."""
        by_key = {c.key: c for c in inventory}
        # a season whose whole series is also selected is redundant: the series
        # takes its episodes with it, and going back over the season afterwards
        # would find nothing left to delete (Sonarr 404). Drop it.
        selected_series = {
            key.split(":")[1] for key in keys if key.startswith("series:")
        }
        keys = [
            key for key in keys
            if not (key.startswith("season:") and key.split(":")[1] in selected_series)
        ]
        chosen: list[Candidate] = []
        refused: list[Candidate] = []
        for key in keys:
            candidate = by_key.get(key)
            if candidate is None:
                continue
            blocking = [
                blocker
                for blocker in candidate.blockers
                # a manual selection overrides seed rules and caps, never the
                # protection against data loss — nor the lack of an app to
                # carry out the deletion
                if code_of(blocker) in ("unique_copy", "unmanaged", "unseen")
                or (not force_seeding and code_of(blocker) == "seed")
            ]
            if not blocking and force_seeding and candidate.seed_blocked:
                candidate = self._without_seed_hold(candidate)
            (refused if blocking else chosen).append(candidate)
        return Plan(
            candidates=chosen,
            skipped=refused,
            scanned=dict(getattr(self, "_scanned", {})),
            inventory=inventory,
        )

    def _season_candidates(
        self,
        item: Series,
        show: PlexShow,
        tags: list[str],
        seerr_media: SeerrMedia | None,
        added_at: int,
    ) -> list[Candidate]:
        files_by_season: dict[int, list[EpisodeFile]] = {}
        for episode_file in self.episode_files(item.id):
            files_by_season.setdefault(episode_file.season, []).append(episode_file)

        result: list[Candidate] = []
        for season, files in sorted(files_by_season.items()):
            stats = show.seasons.get(season, WatchStats())
            rule = self._first_rule("season", stats, added_at)
            candidate = Candidate(
                kind="season",
                key=f"season:{item.id}:{season}",
                title=f"{item.title} — saison {season}",
                arr_id=item.id,
                rule=rule.name if rule else "",
                files=[f.path for f in files],
                library_bytes=sum(f.size for f in files),
                added_at=added_at,
                stats=stats,
                season=season,
                external_id=item.tvdb_id,
                episode_file_ids=[f.id for f in files],
            )
            self._attach_protections(candidate, tags)
            self._attach_seerr(candidate, seerr_media)
            result.append(candidate)
        return result

    def _without_seed_hold(self, candidate: Candidate) -> Candidate:
        """Copy of the candidate whose seed-held torrents become part of the plan.

        Overriding seed obligations means accepting to remove those torrents.
        Without adding them back, only the library copy would go: no bytes
        freed, and one more orphaned file.
        """
        import copy as copy_module

        if self._link_map is None:
            return candidate
        clone = copy_module.copy(candidate)
        clone.blockers = [b for b in candidate.blockers if code_of(b) != "seed"]
        clone.impacts, clone.held_torrents = [], []
        saved = self.config.seeding
        self.config.seeding = SeedingPolicy(mode="ignore")
        try:
            self._attach_torrents(clone, self._link_map)
        finally:
            self.config.seeding = saved
        return clone

    def _attach_protections(self, candidate: Candidate, tags: Iterable[str]) -> None:
        protect = self.config.protect
        protected = sorted(set(candidate.collections) & set(protect.collections))
        if protected:
            candidate.blockers.append(Reason("collection", t("collection protégée — « {name} »", name=protected[0])))
        if protect.in_progress and candidate.in_progress:
            people = candidate.in_progress
            candidate.blockers.append(Reason("in_progress", t(
                "visionnage en cours — commencé par {n} comptes sans être fini" if people > 1
                else "visionnage en cours — commencé par {n} compte sans être fini", n=people
            )))
        tag_set = {t.lower() for t in tags if t}
        for protected in protect.tags:
            if protected.lower() in tag_set:
                candidate.blockers.append(Reason("tag", t("tag protégé — « {tag} »", tag=protected)))
        for title in protect.titles:
            if title.lower() in candidate.title.lower():
                candidate.blockers.append(Reason("title", t("titre protégé — « {title} »", title=title)))

    def _attach_seerr(self, candidate: Candidate, media: SeerrMedia | None) -> None:
        if media is None:
            return
        candidate.seerr_media_id = media.id
        if candidate.kind == "season":
            requests = [
                r for r in media.requests if candidate.season in (r.seasons or [])
            ]
            other_seasons = any(
                candidate.season not in (r.seasons or []) for r in media.requests
            )
            candidate.seerr_media_deletable = not other_seasons
        else:
            requests = list(media.requests)
            candidate.seerr_media_deletable = self.config.delete_seerr_media
        candidate.seerr_request_ids = [r.id for r in requests]

        window = self.config.protect.requested_within_days
        if window > 0:
            recent = [
                r
                for r in requests
                if r.created_at and (time.time() - r.created_at) / DAY < window
            ]
            if recent:
                days = min((time.time() - r.created_at) / DAY for r in recent)
                candidate.blockers.append(Reason("recent_request", t(
                    "demande Seerr récente — il y a {days:.0f} j (fenêtre {window} j)",
                    days=days, window=window,
                )))

    def _attach_torrents(self, candidate: Candidate, link_map: LinkMap) -> None:
        doomed = set(candidate.files)
        torrents = link_map.torrents_for_files(candidate.files)
        policy = self.config.seeding
        impacts: list[TorrentImpact] = []
        for torrent in torrents.values():
            impact = link_map.assess(torrent, doomed)
            name = " ".join(torrent.name.split())[:40]
            if impact.destroys_kept_data:
                candidate.blockers.append(Reason("unique_copy", t(
                    "copie unique en bibliothèque — « {name} » pointe sur {paths}",
                    name=name, paths=", ".join(impact.shared_examples[:2]),
                )))
                continue
            if impact.also_seeds_kept and self.config.protect.keep_torrent_if_shared:
                candidate.blockers.append(Reason("shared_torrent", t(
                    "torrent partagé — « {name} » sert aussi du contenu conservé", name=name
                )))
                continue
            if not policy.satisfied(
                torrent.tracker_host, torrent.ratio, torrent.seeding_days
            ):
                min_ratio, min_days = policy.requirements_for(torrent.tracker_host)
                candidate.blockers.append(Reason("seed", t(
                    "seed insuffisant ({tracker}) — ratio {ratio:.2f} / {days:.0f} j, "
                    "requis {min_ratio:.2f} ou {min_days} j",
                    tracker=torrent.tracker_host or "?", ratio=torrent.ratio,
                    days=torrent.seeding_days, min_ratio=min_ratio, min_days=min_days,
                )))
                candidate.held_torrents.append(
                    (name, torrent.tracker_host, torrent.ratio, torrent.seeding_days)
                )
                continue
            impacts.append(impact)
        candidate.impacts = impacts
        candidate.freed_bytes = link_map.freed_bytes(
            doomed, [i.torrent for i in impacts]
        )
        candidate.unblocks_in_days = self._forecast(candidate)

    def _forecast(self, candidate: Candidate) -> int | None:
        """In how many days will the required seed time be reached?

        Only time can be forecast: the ratio depends on demand on the tracker.
        In "both" mode a ratio that is still too low makes the date uncertain;
        better to announce nothing than to announce something wrong.
        """
        policy = self.config.seeding
        if policy.mode == "ignore" or not candidate.held_torrents:
            return None
        waits = []
        for _name, tracker, ratio, seed_days in candidate.held_torrents:
            min_ratio, min_days = policy.requirements_for(tracker)
            if policy.mode == "both" and ratio < min_ratio:
                return None  # depends on a ratio yet to be reached: not predictable
            waits.append(max(min_days - seed_days, 0))
        return int(round(max(waits))) if waits else None

    def _apply_limits(
        self, candidates: list[Candidate], skipped: list[Candidate]
    ) -> list[Candidate]:
        limits = self.config.limits
        kept: list[Candidate] = []
        total = 0
        cap = int(limits.max_gb_per_run * 10**9)
        for candidate in candidates:
            if len(kept) >= limits.max_items_per_run:
                candidate.blockers.append(Reason("cap_items", t("plafond de passage — nombre d'items")))
                skipped.append(candidate)
                continue
            if cap and total + candidate.freed_bytes > cap and kept:
                candidate.blockers.append(Reason("cap_volume", t("plafond de passage — volume")))
                skipped.append(candidate)
                continue
            kept.append(candidate)
            total += candidate.freed_bytes
        return kept

    def simulate_seeding(
        self, inventory: list[Candidate], orphan_groups: list, link_map, policy
    ) -> dict:
        """What a different seed policy would yield, without saving anything.

        Works on copies: the displayed inventory is left untouched.
        """
        import copy as copy_module

        from . import orphans as orphans_module

        saved = self.config.seeding
        self.config.seeding = policy
        try:
            now = soon = 0
            unlocked = 0
            for item in inventory:
                if not item.eligible:
                    continue
                clone = copy_module.copy(item)
                clone.blockers = [
                    b for b in item.blockers if code_of(b) != "seed"
                ]
                clone.impacts = []
                clone.held_torrents = []
                self._attach_torrents(clone, link_map)
                if clone.actionable:
                    now += clone.freed_bytes
                    if not item.actionable:
                        unlocked += 1
                elif (
                    clone.unblocks_in_days is not None and clone.unblocks_in_days <= 30
                ):
                    soon += clone.freed_bytes
            for group in orphan_groups:
                clone = copy_module.copy(group)
                clone.unblocks_in_days = None
                clone.blocked = None
                orphans_module._assess(clone, link_map, policy)
                if clone.seed_ok:
                    now += clone.bytes
                    if not group.seed_ok:
                        unlocked += 1
                elif (
                    clone.unblocks_in_days is not None and clone.unblocks_in_days <= 30
                ):
                    soon += clone.bytes
            return {
                "libérable_maintenant": now,
                "libérable_sous_30j": soon,
                "débloqués": unlocked,
            }
        finally:
            self.config.seeding = saved

    def preview_rules(
        self, inventory: list[Candidate], rules: list[Rule]
    ) -> list[dict]:
        """What these rules would catch, without saving or deleting anything.

        Reuses the in-memory inventory: no network calls, instant response.
        """
        saved = self.config.rules
        self.config.rules = rules
        try:
            summary = {
                rule.name: {
                    "nom": rule.name,
                    "activée": rule.enabled,
                    "éléments": 0,
                    "octets": 0,
                    "bloqués": 0,
                    "exemples": [],
                }
                for rule in rules
            }
            for item in inventory:
                if item.kind == "season" and not any(
                    r.media == "season" and r.enabled for r in rules
                ):
                    continue
                rule = self._first_rule(item.kind, item.stats, item.added_at)
                if rule is None:
                    continue
                entry = summary[rule.name]
                entry["éléments"] += 1
                entry["octets"] += item.freed_bytes or item.library_bytes
                if item.blockers:
                    entry["bloqués"] += 1
                if len(entry["exemples"]) < 5:
                    entry["exemples"].append(item.title)
            return list(summary.values())
        finally:
            self.config.rules = saved

    # -------------------------------------------------------------- execution

    def execute(self, plan: Plan, progress=None) -> list[dict]:
        results = []
        for candidate in plan.candidates:
            # record enough to re-download: a deletion is permanent, and the
            # log is the only trace left.
            record = {
                # the timestamp isn't enough: a batch deletes several items in
                # the same second, and "re-request" must target the right one
                "id": uuid.uuid4().hex[:12],
                "horodatage": int(time.time()),
                "type": candidate.kind,
                "titre": candidate.title,
                "règle": candidate.rule or t("(sélection manuelle)"),
                "octets_libérés": candidate.freed_bytes,
                "identifiant_externe": candidate.external_id,
                "saison": candidate.season,
                "dernier_visionnage": candidate.stats.last_viewed,
                "spectateurs": candidate.stats.distinct_viewers,
                "fichiers": candidate.files,
                "torrents": [
                    {
                        "nom": " ".join(i.torrent.name.split()),
                        "hash": i.torrent.hash,
                        "tracker": i.torrent.tracker_host,
                        "ratio": round(i.torrent.ratio, 2),
                        "seed_jours": round(i.torrent.seeding_days, 1),
                    }
                    for i in candidate.impacts
                ],
                "seerr": {
                    "media": candidate.seerr_media_id,
                    "requests": list(candidate.seerr_request_ids),
                },
                "étapes": [],
                "erreurs": [],
            }
            try:
                self._delete_from_arr(candidate, record)
                self._delete_torrents(candidate, record)
                self._clean_seerr(candidate, record)
            except Exception as error:  # noqa: BLE001 - log it and carry on
                record["erreurs"].append(f"{type(error).__name__}: {error}")
            results.append(record)
            self._append_history(record)
            if progress is not None:
                progress(record)
        return results

    def _delete_from_arr(self, candidate: Candidate, record: dict) -> None:
        exclusion = self.config.add_import_exclusion
        if candidate.kind == "movie":
            self.radarr.delete_movie(candidate.arr_id, add_exclusion=exclusion)
            record["étapes"].append(t("Radarr : film et fichier supprimés"))
        elif candidate.kind == "series":
            self.sonarr.delete_series(candidate.arr_id, add_exclusion=exclusion)
            record["étapes"].append(t("Sonarr : série et fichiers supprimés"))
        else:
            self.sonarr.delete_episode_files(candidate.episode_file_ids)
            self.sonarr.unmonitor_season(candidate.arr_id, candidate.season or 0)
            record["étapes"].append(t(
                "Sonarr : {n} fichiers supprimés, saison {season} démonitorée",
                n=len(candidate.episode_file_ids), season=candidate.season,
            ))

    def _delete_torrents(self, candidate: Candidate, record: dict) -> None:
        hashes = [i.torrent.hash for i in candidate.impacts]
        if not hashes:
            record["étapes"].append(t("qBittorrent : aucun torrent concerné"))
            return
        self.qbit.delete(hashes, delete_files=True)
        names = ", ".join(i.torrent.name[:30] for i in candidate.impacts[:3])
        record["étapes"].append(t(
            "qBittorrent : {n} torrent(s) retiré(s) avec données ({names})",
            n=len(hashes), names=names,
        ))

    def _clean_seerr(self, candidate: Candidate, record: dict) -> None:
        if self.seerr is None:
            return
        for request_id in candidate.seerr_request_ids:
            self.seerr.delete_request(request_id)
        if candidate.seerr_request_ids:
            record["étapes"].append(t(
                "Seerr : {n} request(s) supprimée(s)", n=len(candidate.seerr_request_ids)
            ))
        if (
            candidate.seerr_media_id
            and candidate.seerr_media_deletable
            and self.config.delete_seerr_media
        ):
            self.seerr.delete_media(candidate.seerr_media_id)
            record["étapes"].append(t(
                "Seerr : fiche media {id} supprimée (le titre redevient demandable)",
                id=candidate.seerr_media_id,
            ))

    def delete_orphan_groups(
        self, groups: list, force: bool = False, progress=None
    ) -> list[dict]:
        """Remove torrents outside the library, along with their data.

        No calls to Radarr/Sonarr/Seerr: these bytes are no longer referenced
        anywhere, only the torrent client needs updating.
        """
        results = []
        for group in groups:
            record = {
                "id": uuid.uuid4().hex[:12],
                "horodatage": int(time.time()),
                "type": "seed-seul",
                "titre": group.name,
                "règle": t("(données hors bibliothèque)"),
                "octets_libérés": group.bytes,
                "torrents": [
                    {
                        "nom": " ".join(t.name.split()),
                        "hash": t.hash,
                        "tracker": t.tracker_host,
                        "ratio": round(t.ratio, 2),
                        "seed_jours": round(t.seeding_days, 1),
                    }
                    for t in group.torrents
                ],
                "étapes": [],
                "erreurs": [],
            }
            try:
                if group.blocked:
                    raise RuntimeError(group.blocked)
                if not force and not group.seed_ok:
                    raise RuntimeError(t(
                        "obligations de seed non remplies (déblocage dans {days} j)",
                        days=group.unblocks_in_days,
                    ))
                self.qbit.delete([t.hash for t in group.torrents], delete_files=True)
                record["étapes"].append(t(
                    "qBittorrent : {n} torrent(s) retiré(s) avec données", n=len(group.torrents)
                ))
            except Exception as error:  # noqa: BLE001
                record["erreurs"].append(f"{type(error).__name__}: {error}")
                record["octets_libérés"] = 0
            results.append(record)
            self._append_history(record)
            if progress is not None:
                progress(record)
        return results

    def _append_history(self, record: dict) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        history = self.config.state_dir / "history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _match_series_to_plex(
    series: list[Series], snapshot: PlexSnapshot
) -> dict[int, PlexShow]:
    """Link a Sonarr series to its Plex entry via the path prefix."""
    by_path = sorted(
        ((item.path.rstrip("/"), item) for item in series if item.path),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    shows_by_guid = {show.guid: show for show in snapshot.shows}
    votes: dict[int, dict[str, int]] = {}
    for episode_path, (show_guid, _season) in snapshot.episode_index.items():
        for prefix, item in by_path:
            if episode_path.startswith(prefix + "/"):
                votes.setdefault(item.id, {})
                votes[item.id][show_guid] = votes[item.id].get(show_guid, 0) + 1
                break
    result: dict[int, PlexShow] = {}
    for series_id, guid_votes in votes.items():
        guid = max(guid_votes, key=guid_votes.get)
        if guid in shows_by_guid:
            result[series_id] = shows_by_guid[guid]
    return result
