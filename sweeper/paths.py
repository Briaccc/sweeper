"""Translating a service's view of the filesystem into sweeper's, and back.

See sweeper/services.py for why: in containers, each application mounts the same
files under its own paths, and every hardlink calculation depends on paths.
"""

from __future__ import annotations


class PathMap:
    """Translates a service's view of the filesystem into sweeper's, and back.

    Rules are ``(remote, local)`` prefix pairs; the longest matching prefix wins.
    Backslash-separated remote paths (a Windows host) are understood.
    """

    def __init__(self, rules=()) -> None:
        pairs = []
        for rule in rules or ():
            remote, local = (rule["remote"], rule["local"]) if isinstance(rule, dict) else rule
            pairs.append((str(remote), str(local)))
        self.rules = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)

    def __bool__(self) -> bool:
        return bool(self.rules)

    @staticmethod
    def _norm(path: str) -> str:
        return path.replace("\\", "/").rstrip("/")

    def to_local(self, path: str | None) -> str | None:
        if not path or not self.rules:
            return path
        norm = path.replace("\\", "/")
        for remote, local in self.rules:
            prefix = self._norm(remote)
            if norm == prefix or norm.startswith(prefix + "/"):
                return local.rstrip("/") + norm[len(prefix):]
        return path

    def to_remote(self, path: str | None) -> str | None:
        if not path or not self.rules:
            return path
        for remote, local in sorted(self.rules, key=lambda pair: len(pair[1]), reverse=True):
            prefix = local.rstrip("/")
            if path == prefix or path.startswith(prefix + "/"):
                rest = path[len(prefix):]
                if "\\" in remote:
                    rest = rest.replace("/", "\\")
                return remote.rstrip("/\\") + rest
        return path
