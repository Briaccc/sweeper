"""Move a film sitting loose at the library root into a folder named after it.

Radarr requires one folder per film: a `Movies/Title.mkv` cannot be adopted in
place. Tidying is a **rename on the same filesystem** — the inode doesn't
change, a hardlinked copy elsewhere (a seeding torrent) sees nothing, and the
operation is reversible. Companion files with the same name (subtitles, nfo)
come along. We refuse if the target already exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .i18n import t


@dataclass
class TidyPlan:
    source: Path
    target_dir: Path
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    refusal: str | None = None

    @property
    def possible(self) -> bool:
        return self.refusal is None


def plan(file: Path, library_roots: list[str]) -> TidyPlan:
    """Compute the moves without doing anything."""
    root = next((r for r in library_roots if str(file).startswith(r + "/")), None)
    if root is None or len(file.relative_to(root).parts) != 1:
        return TidyPlan(file, file.parent, refusal=t("ce fichier n'est pas nu à la racine"))
    stem = file.stem
    target_dir = Path(root) / stem
    if target_dir.exists():
        return TidyPlan(file, target_dir, refusal=t("le dossier « {name} » existe déjà", name=stem))
    siblings = sorted(
        p for p in Path(root).iterdir()
        if p.is_file() and (p.stem == stem or p.name.startswith(stem + "."))
    )
    return TidyPlan(file, target_dir, moves=[(p, target_dir / p.name) for p in siblings])


def apply(tidy: TidyPlan) -> None:
    """Execute the plan: one mkdir, then one rename per file."""
    if not tidy.possible:
        raise RuntimeError(tidy.refusal)
    tidy.target_dir.mkdir(parents=False, exist_ok=False)
    for src, dst in tidy.moves:
        src.rename(dst)
