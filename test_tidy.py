#!/usr/bin/env python3
"""Tidying moves files, so it is tested on a real throwaway directory."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sweeper import i18n, tidy

i18n.set_language("fr")  # the refusals below are asserted in French


class TidyCases(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Movies"
        self.root.mkdir()
        (Path(self.tmp.name) / "Downloads").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, rel: str, size: int = 10) -> Path:
        p = Path(self.tmp.name) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def test_range_le_film_et_ses_compagnons(self) -> None:
        mkv = self.write("Movies/Film Nu.mkv", 100)
        srt = self.write("Movies/Film Nu.fr.srt", 5)
        nfo = self.write("Movies/Film Nu.nfo", 3)
        other = self.write("Movies/Autre Film.mkv", 7)          # same root, different movie
        plan = tidy.plan(mkv, [str(self.root)])
        self.assertTrue(plan.possible)
        self.assertEqual(plan.target_dir, self.root / "Film Nu")
        self.assertEqual({src.name for src, _ in plan.moves}, {"Film Nu.mkv", "Film Nu.fr.srt", "Film Nu.nfo"})
        tidy.apply(plan)
        self.assertTrue((self.root / "Film Nu" / "Film Nu.mkv").is_file())
        self.assertTrue((self.root / "Film Nu" / "Film Nu.fr.srt").is_file())
        self.assertFalse(mkv.exists())
        self.assertTrue(other.is_file(), "un autre film à la racine ne doit pas bouger")

    def test_preserve_l_inode_donc_les_hardlinks(self) -> None:
        mkv = self.write("Movies/Seed.mkv", 50)
        twin = Path(self.tmp.name) / "Downloads" / "Seed.mkv"
        os.link(mkv, twin)                                       # the torrent's copy
        inode = mkv.stat().st_ino
        tidy.apply(tidy.plan(mkv, [str(self.root)]))
        moved = self.root / "Seed" / "Seed.mkv"
        self.assertEqual(moved.stat().st_ino, inode, "un renommage ne change pas l'inode")
        self.assertEqual(twin.stat().st_nlink, 2, "le torrent garde son hardlink")
        self.assertEqual(twin.read_bytes(), moved.read_bytes())

    def test_refuse_si_deja_dans_un_dossier(self) -> None:
        deep = self.write("Movies/Deja Range/Deja Range.mkv")
        plan = tidy.plan(deep, [str(self.root)])
        self.assertFalse(plan.possible)
        self.assertIn("pas nu", plan.refusal)

    def test_refuse_si_le_dossier_existe(self) -> None:
        mkv = self.write("Movies/Doublon.mkv")
        (self.root / "Doublon").mkdir()
        plan = tidy.plan(mkv, [str(self.root)])
        self.assertFalse(plan.possible)
        self.assertIn("existe déjà", plan.refusal)
        with self.assertRaises(RuntimeError):
            tidy.apply(plan)
        self.assertTrue(mkv.exists(), "rien ne doit avoir bougé après un refus")

    def test_refuse_hors_racine(self) -> None:
        stray = self.write("Downloads/Ailleurs.mkv")
        self.assertFalse(tidy.plan(stray, [str(self.root)]).possible)


if __name__ == "__main__":
    unittest.main(verbosity=2)
