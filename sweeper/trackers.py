"""Tracker identity derived from its hostname.

The same tracker often announces under several hosts — `tracker.example.org`
and `tk.example.net`, for instance. Writing one seed rule per host would mean
entering it twice, and discovering the second host the day it causes trouble.

The second-to-last label of the hostname gives the right grouping across all
the trackers seen in practice. It is a heuristic: an exact hostname listed in
the configuration always takes precedence over the group.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def group_of(host: str) -> str:
    """`tk.example.net` -> `example`, `tracker.other.cc` -> `other`."""
    if not host:
        return ""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return host
    return labels[-2]


@dataclass
class TrackerStats:
    group: str
    hosts: set[str] = field(default_factory=set)
    torrents: int = 0
    bytes: int = 0
    held_bytes: int = 0        # bytes held back by the current seed rule
    held_items: int = 0
    max_wait_days: int = 0
    min_ratio: float = 0.0
    min_seed_days: int = 0
    explicit: bool = False     # a dedicated rule exists; otherwise it's the default

    def as_json(self) -> dict:
        return {
            "groupe": self.group,
            "hôtes": sorted(self.hosts),
            "torrents": self.torrents,
            "octets": self.bytes,
            "octets_retenus": self.held_bytes,
            "éléments_retenus": self.held_items,
            "attente_max_jours": self.max_wait_days,
            "min_ratio": self.min_ratio,
            "min_seed_days": self.min_seed_days,
            "règle_propre": self.explicit,
        }


def collect(torrents, inventory, orphan_groups, policy) -> list[TrackerStats]:
    """Footprint of each tracker, and what its rule holds back today."""
    stats: dict[str, TrackerStats] = {}

    def entry(host: str) -> TrackerStats:
        group = group_of(host)
        item = stats.get(group)
        if item is None:
            min_ratio, min_seed_days = policy.requirements_for(host)
            item = TrackerStats(
                group=group,
                min_ratio=min_ratio,
                min_seed_days=min_seed_days,
                explicit=policy.has_override(host),
            )
            stats[group] = item
        item.hosts.add(host)
        return item

    for torrent in torrents:
        if not torrent.tracker_host:
            continue
        item = entry(torrent.tracker_host)
        item.torrents += 1
        item.bytes += torrent.size

    # what is held back on the library side — a season carries the same torrents
    # as its series: count only the series, otherwise the bytes are doubled
    for candidate in inventory:
        if not candidate.held_torrents or candidate.kind == "season":
            continue
        share = candidate.library_bytes // max(len(candidate.held_torrents), 1)
        for _name, host, _ratio, seed_days in candidate.held_torrents:
            item = entry(host)
            item.held_bytes += share
            item.held_items += 1
            wait = max(item.min_seed_days - seed_days, 0)
            item.max_wait_days = max(item.max_wait_days, int(round(wait)))

    # and on the side of data outside the library
    for group_entry in orphan_groups:
        if group_entry.seed_ok or group_entry.blocked:
            continue
        share = group_entry.bytes // max(len(group_entry.torrents), 1)
        for torrent in group_entry.torrents:
            if not torrent.tracker_host:
                continue
            item = entry(torrent.tracker_host)
            item.held_bytes += share
            item.held_items += 1
            wait = max(item.min_seed_days - torrent.seeding_days, 0)
            item.max_wait_days = max(item.max_wait_days, int(round(wait)))

    return sorted(stats.values(), key=lambda s: s.bytes, reverse=True)
