"""Data that only serves seeding anymore: present on disk, absent from the
library.

Two ways to end up here:

- a release replaced by a Sonarr/Radarr upgrade, which keeps seeding;
- a media item removed from the library while its torrent had not yet met its
  obligations — the file stays, but no *arr knows about it anymore, so nothing
  will ever offer it up for deletion again.

This module catches the second case, which is a costly blind spot.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Limits, SeedingPolicy
from .i18n import Reason, t
from .linkmap import LinkMap
from .qbit import Torrent

#: a finished torrent qBittorrent reports as healthy; anything else (missing
#: files, a check in progress, an error) deserves a look before removing it
_HEALTHY_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP", "stoppedUP"}


@dataclass
class OrphanGroup:
    """A batch of torrents that share exactly the same bytes."""

    key: str
    name: str
    bytes: int
    torrents: list[Torrent] = field(default_factory=list)
    files: int = 0
    unblocks_in_days: int | None = None
    blocked: str | None = None
    #: every name the batch's torrents use on disk
    paths: set[str] = field(default_factory=set)

    @property
    def seed_ok(self) -> bool:
        return self.unblocks_in_days == 0 and self.blocked is None

    @property
    def oldest_seed_days(self) -> float:
        return max((t.seeding_days for t in self.torrents), default=0.0)

    @property
    def best_ratio(self) -> float:
        return max((t.ratio for t in self.torrents), default=0.0)

    @property
    def trackers(self) -> list[str]:
        return sorted({t.tracker_host for t in self.torrents if t.tracker_host})

    @property
    def idle_days(self) -> float:
        """Days since any torrent of the batch moved data; 0 if one is unknown."""
        seen = [t.last_activity for t in self.torrents]
        if not seen or min(seen) <= 0:
            return 0.0
        return (time.time() - max(seen)) / 86400.0


def _batch_key(inodes: frozenset[tuple[int, int]]) -> str:
    """Stable across refreshes and unique per batch.

    Built from the whole inode set: a folder torrent (video + nfo) and its
    bare-video twin share their video, so any single inode would give both
    batches the same key, and selecting one would select the other.
    """
    return hashlib.sha1(repr(sorted(inodes)).encode()).hexdigest()[:16]


def find(
    link_map: LinkMap,
    torrents: list[Torrent],
    library_roots: list[Path],
    policy: SeedingPolicy,
) -> list[OrphanGroup]:
    """Group by inode set: removing a single torrent from a batch frees nothing."""
    roots = [str(r) for r in library_roots]

    def in_library(path: str) -> bool:
        return any(path == r or path.startswith(r + "/") for r in roots)

    groups: dict[frozenset[tuple[int, int]], OrphanGroup] = {}
    for torrent in torrents:
        paths = link_map.torrent_files(torrent)
        if not paths:
            continue
        inodes = frozenset(
            inode for p in paths if (inode := link_map.inode_by_path.get(p)) is not None
        )
        if not inodes:
            continue
        # a single hardlink inside the library is enough to disqualify the batch
        if any(
            in_library(sibling)
            for inode in inodes
            for sibling in link_map.paths_by_inode.get(inode, [])
        ):
            continue

        group = groups.get(inodes)
        if group is None:
            group = OrphanGroup(
                key=_batch_key(inodes),
                name=" ".join(torrent.name.split()),
                bytes=sum(link_map.size_by_inode.get(i, 0) for i in inodes),
                files=len(inodes),
            )
            groups[inodes] = group
        group.torrents.append(torrent)
        group.paths.update(paths)

    result = list(groups.values())
    for group in result:
        _assess(group, link_map, policy)
    result.sort(key=lambda g: g.bytes, reverse=True)
    return result


def retirable(
    groups: list[OrphanGroup], queued: set[str], min_idle_days: int, limits: Limits
) -> list[OrphanGroup]:
    """Batches the automatic run may remove without anyone asking.

    Seed obligations met, no data moved for `min_idle_days`, finished and
    healthy, and nothing an *arr still waits for (a pending import, say).
    `groups` is sorted biggest first, so the run's limits keep the biggest.
    """
    eligible = _without_shared_files(
        [
            group for group in groups
            if group.seed_ok
            and group.idle_days >= min_idle_days
            and all(
                t.progress >= 1 and t.state in _HEALTHY_STATES and t.hash.lower() not in queued
                for t in group.torrents
            )
        ],
        groups,
    )
    chosen: list[OrphanGroup] = []
    total = 0
    cap = int(limits.max_gb_per_run * 10**9)
    for group in eligible:
        if len(chosen) >= limits.max_items_per_run:
            break
        if cap and total + group.bytes > cap and chosen:
            continue
        chosen.append(group)
        total += group.bytes
    return _without_shared_files(chosen, groups)   # the limits may have split a pair


def _without_shared_files(
    chosen: list[OrphanGroup], groups: list[OrphanGroup]
) -> list[OrphanGroup]:
    """Two torrents can use the very same file: removing one with its data would
    pull it from under the other, so a batch stays while such a neighbour does."""
    while True:
        chosen_keys = {g.key for g in chosen}
        kept_paths = {p for g in groups if g.key not in chosen_keys for p in g.paths}
        safe = [g for g in chosen if not g.paths & kept_paths]
        if len(safe) == len(chosen):
            return chosen
        chosen = safe


def _assess(group: OrphanGroup, link_map: LinkMap, policy: SeedingPolicy) -> None:
    waits: list[float] = []
    for torrent in group.torrents:
        impact = link_map.assess(torrent, doomed=set())
        if impact.destroys_kept_data:
            group.blocked = Reason("unique_copy", t("le torrent détient l'unique copie d'un fichier conservé"))
            return
        min_ratio, min_days = policy.requirements_for(torrent.tracker_host)
        if policy.mode == "ignore" or policy.satisfied(
            torrent.tracker_host, torrent.ratio, torrent.seeding_days
        ):
            waits.append(0.0)
            continue
        if policy.mode == "both" and torrent.ratio < min_ratio:
            group.unblocks_in_days = None  # depends on a ratio: not predictable
            return
        waits.append(max(min_days - torrent.seeding_days, 0))
    group.unblocks_in_days = int(round(max(waits))) if waits else 0
