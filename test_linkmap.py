#!/usr/bin/env python3
"""Checks the hardlink logic on a throwaway directory tree.

This is the computation that decides whether a torrent can go along with its
data: it is tested here on real inodes, not on mocks.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sweeper.linkmap import LinkMap
from sweeper.qbit import Torrent


def torrent(name: str, content_path: str, **kwargs) -> Torrent:
    defaults = dict(
        hash=name, name=name, category="", state="stalledUP", size=0, ratio=1.0,
        seeding_seconds=86400 * 40, content_path=content_path, save_path="", tracker="",
    )
    defaults.update(kwargs)
    return Torrent(**defaults)


class HardlinkCases(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for folder in ("Downloads", "cross-seed-links", "Movies", "Tv Show"):
            (self.root / folder).mkdir()
        self.roots = [self.root / f for f in
                      ("Downloads", "cross-seed-links", "Movies", "Tv Show")]
        self.library = [self.root / "Movies", self.root / "Tv Show"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, path: Path, size: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def link(self, src: Path, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
        return dst

    def build(self, torrents: list[Torrent]) -> LinkMap:
        return LinkMap(self.roots, self.library).scan().index_torrents(torrents)

    # ------------------------------------------------------------ real cases

    def test_film_hardlinke_libere_seulement_si_torrent_retire(self) -> None:
        """The base case: Downloads + Movies + 2 cross-seed links."""
        source = self.write(self.root / "Downloads/Film.2024/Film.mkv", 1000)
        library = self.link(source, self.root / "Movies/Film (2024)/Film.mkv")
        self.link(source, self.root / "cross-seed-links/a/Film.mkv")
        self.link(source, self.root / "cross-seed-links/b/Film.mkv")

        torrents = [
            torrent("principal", str(self.root / "Downloads/Film.2024")),
            torrent("xseed-a", str(self.root / "cross-seed-links/a")),
            torrent("xseed-b", str(self.root / "cross-seed-links/b")),
        ]
        link_map = self.build(torrents)
        doomed = {str(library)}

        # deleting only the library file frees no bytes
        self.assertEqual(link_map.freed_bytes(doomed, []), 0)
        # all three torrents must be removed for the inode to go away
        found = link_map.torrents_for_files([str(library)])
        self.assertEqual(len(found), 3)
        self.assertEqual(link_map.freed_bytes(doomed, list(found.values())), 1000)

    def test_season_pack_partage_est_signale(self) -> None:
        """An S01 pack with only one episode deleted is still useful."""
        e1 = self.write(self.root / "Downloads/Show.S01/E01.mkv", 500)
        e2 = self.write(self.root / "Downloads/Show.S01/E02.mkv", 500)
        lib1 = self.link(e1, self.root / "Tv Show/Show/Season 01/E01.mkv")
        self.link(e2, self.root / "Tv Show/Show/Season 01/E02.mkv")

        pack = torrent("pack", str(self.root / "Downloads/Show.S01"))
        link_map = self.build([pack])

        impact = link_map.assess(pack, {str(lib1)})
        self.assertTrue(impact.also_seeds_kept)
        self.assertFalse(impact.destroys_kept_data)

        # once the whole season is deleted, the pack can be removed without caveat
        impact = link_map.assess(
            pack, {str(lib1), str(self.root / "Tv Show/Show/Season 01/E02.mkv")}
        )
        self.assertFalse(impact.also_seeds_kept)
        self.assertFalse(impact.destroys_kept_data)

    def test_torrent_pointant_sur_la_bibliotheque_est_bloque(self) -> None:
        """Import by move: the torrent seeds the Plex file itself."""
        kept = self.write(self.root / "Tv Show/Autre/Season 01/E01.mkv", 700)
        direct = torrent("direct", str(self.root / "Tv Show/Autre"))
        link_map = self.build([direct])

        impact = link_map.assess(direct, doomed=set())
        self.assertTrue(impact.destroys_kept_data)
        self.assertIn("E01.mkv", impact.shared_examples)

        # if that file is among the doomed ones, nothing blocks anymore
        self.assertFalse(
            link_map.assess(direct, {str(kept)}).destroys_kept_data
        )

    def test_release_remplacee_est_retirable_sans_risque(self) -> None:
        """Old release still seeding, absent from the library."""
        self.write(self.root / "Downloads/Vieux.Release/film.mkv", 900)
        orphan = torrent("vieux", str(self.root / "Downloads/Vieux.Release"))
        link_map = self.build([orphan])

        impact = link_map.assess(orphan, doomed=set())
        self.assertFalse(impact.destroys_kept_data)
        self.assertFalse(impact.also_seeds_kept)
        self.assertEqual(link_map.freed_bytes(set(), [orphan]), 900)

    def test_fichier_sans_torrent_libere_immediatement(self) -> None:
        library = self.write(self.root / "Movies/Seul (2020)/seul.mkv", 400)
        link_map = self.build([])
        self.assertEqual(link_map.freed_bytes({str(library)}, []), 400)

    def test_inode_compte_une_seule_fois(self) -> None:
        source = self.write(self.root / "Downloads/D/f.mkv", 1234)
        library = self.link(source, self.root / "Movies/M/f.mkv")
        t = torrent("t", str(self.root / "Downloads/D"))
        link_map = self.build([t])
        self.assertEqual(link_map.freed_bytes({str(library)}, [t]), 1234)


class DeviceIdentity(unittest.TestCase):
    """An inode number is only unique within one disk: the index must account for that."""

    def test_meme_inode_sur_deux_disques_ne_sont_pas_des_hardlinks(self) -> None:
        link_map = LinkMap([], [])
        # two unrelated files, same inode number, different disks
        link_map.paths_by_inode = {(1, 42): ["/disk1/a.mkv"], (2, 42): ["/disk2/b.mkv"]}
        link_map.inode_by_path = {"/disk1/a.mkv": (1, 42), "/disk2/b.mkv": (2, 42)}
        link_map.size_by_inode = {(1, 42): 100, (2, 42): 200}
        self.assertEqual(link_map.siblings("/disk1/a.mkv"), ["/disk1/a.mkv"])
        self.assertEqual(link_map.freed_bytes({"/disk1/a.mkv"}, []), 100)

    def test_scan_enregistre_le_disque(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "x.mkv"
            f.write_bytes(b"x")
            link_map = LinkMap([Path(tmp)], []).scan()
            dev, ino = link_map.inode_by_path[str(f)]
            self.assertEqual((dev, ino), (f.stat().st_dev, f.stat().st_ino))


if __name__ == "__main__":
    unittest.main(verbosity=2)
