"""Hardlink index: the cornerstone of the "bytes actually freed" calculation.

In a typical setup, `Movies/` and `Tv Show/` are only hardlinks into
`Downloads/`, and cross-seed adds more of them in `cross-seed-links/`.
Deleting a film in Radarr therefore frees nothing as long as the torrent (and
its cross-seed twins) still exist. For a given file, this index gives all of
its names on disk and every torrent involved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .qbit import Torrent


@dataclass
class TorrentImpact:
    torrent: Torrent
    #: the torrent holds the only copy of a file we are not deleting
    destroys_kept_data: bool = False
    #: the torrent also seeds content kept in the library (season pack)
    also_seeds_kept: bool = False
    shared_examples: list[str] = field(default_factory=list)


class LinkMap:
    def __init__(self, roots: list[Path], library_roots: list[Path]) -> None:
        self.roots = [r for r in roots if r.is_dir()]
        self.library_roots = [str(r) for r in library_roots]
        # keys: (st_dev, st_ino) — see scan()
        self.paths_by_inode: dict[tuple[int, int], list[str]] = {}
        self.inode_by_path: dict[str, tuple[int, int]] = {}
        self.size_by_inode: dict[tuple[int, int], int] = {}
        self._torrent_by_content: list[tuple[str, Torrent]] = []

    # ------------------------------------------------------------------ build

    def scan(self) -> "LinkMap":
        for root in self.roots:
            for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    try:
                        stat = os.lstat(path)
                    except OSError:
                        continue
                    if not os.path.stat.S_ISREG(stat.st_mode):
                        continue
                    # an inode number is only unique within one filesystem;
                    # a hardlink never crosses it. A file's identity is
                    # therefore the (device, inode) pair.
                    key = (stat.st_dev, stat.st_ino)
                    self.paths_by_inode.setdefault(key, []).append(path)
                    self.inode_by_path[path] = key
                    self.size_by_inode[key] = stat.st_size
        return self

    def index_torrents(self, torrents: list[Torrent]) -> "LinkMap":
        # sort by descending length: the most specific path wins
        self._torrent_by_content = sorted(
            ((t.content_path.rstrip("/"), t) for t in torrents if t.content_path),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        return self

    # ----------------------------------------------------------------- lookup

    def siblings(self, path: str) -> list[str]:
        inode = self.inode_by_path.get(path)
        if inode is None:
            try:
                stat = os.lstat(path)
                inode = (stat.st_dev, stat.st_ino)
            except OSError:
                return [path]
        return self.paths_by_inode.get(inode, [path])

    def torrent_for_path(self, path: str) -> Torrent | None:
        for content_path, torrent in self._torrent_by_content:
            if path == content_path or path.startswith(content_path + "/"):
                return torrent
        return None

    def torrents_for_files(self, files: list[str]) -> dict[str, Torrent]:
        """Every torrent serving any of the files, hardlinks included."""
        found: dict[str, Torrent] = {}
        for path in files:
            for sibling in self.siblings(path):
                torrent = self.torrent_for_path(sibling)
                if torrent is not None:
                    found[torrent.hash] = torrent
        return found

    def torrent_files(self, torrent: Torrent) -> list[str]:
        content = torrent.content_path.rstrip("/")
        if not content:
            return []
        if content in self.inode_by_path:
            return [content]
        prefix = content + "/"
        return [p for p in self.inode_by_path if p.startswith(prefix)]

    def _is_library(self, path: str) -> bool:
        return any(
            path == root or path.startswith(root + "/") for root in self.library_roots
        )

    def assess(self, torrent: Torrent, doomed: set[str]) -> TorrentImpact:
        """Check that removing this torrent *with its data* breaks nothing."""
        impact = TorrentImpact(torrent=torrent)
        content = torrent.content_path.rstrip("/")
        for path in self.torrent_files(torrent):
            if path in doomed:
                continue  # a copy of content we are deliberately deleting
            survivors = [
                sibling
                for sibling in self.siblings(path)
                if sibling != path
                and sibling not in doomed
                and not (sibling == content or sibling.startswith(content + "/"))
            ]
            if survivors:
                # the data exists elsewhere: removing the torrent doesn't
                # destroy it, but if that "elsewhere" is the library, we stop
                # seeding content we are keeping (shared season pack).
                if any(self._is_library(sibling) for sibling in survivors):
                    impact.also_seeds_kept = True
                    if len(impact.shared_examples) < 3:
                        impact.shared_examples.append(os.path.basename(path))
            elif self._is_library(path):
                # the torrent points directly at the library file (imported
                # by move): deleting it would destroy the copy.
                impact.destroys_kept_data = True
                if len(impact.shared_examples) < 3:
                    impact.shared_examples.append(os.path.basename(path))
            # otherwise: data present only in the torrent (replaced release,
            # extra that was never imported) — deleting it is safe.
        return impact

    def freed_bytes(self, doomed_files: set[str], removed_torrents: list[Torrent]) -> int:
        """Bytes actually reclaimed: inodes whose every name disappears."""
        removed_names: set[str] = set(doomed_files)
        for torrent in removed_torrents:
            removed_names.update(self.torrent_files(torrent))
        freed = 0
        counted: set[tuple[int, int]] = set()
        for path in removed_names:
            inode = self.inode_by_path.get(path)
            if inode is None or inode in counted:
                continue
            if all(sibling in removed_names for sibling in self.paths_by_inode[inode]):
                freed += self.size_by_inode.get(inode, 0)
                counted.add(inode)
        return freed


def uncovered_torrents(torrents: list[Torrent], roots: list) -> list[Torrent]:
    """Torrents whose `save_path` lies outside every scanned root.

    This is the gap that silently makes the freed-bytes calculation wrong: a
    file hardlinked from somewhere sweeper doesn't look would make a deletion
    appear to free space while a copy survives elsewhere. It is never fatal —
    just a signal to show, not to hide.
    """
    root_strs = [str(r) for r in roots]

    def covered(path: str) -> bool:
        return any(path == root or path.startswith(root + "/") for root in root_strs)

    return [t for t in torrents if t.save_path and not covered(t.save_path)]
