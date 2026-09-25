"""Configuration loading (TOML, stdlib only)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HOME = Path.home()

import os
import shutil

from . import i18n
from .i18n import t

# SWEEPER_CONFIG lets the configuration be mounted elsewhere (container: /config)
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("SWEEPER_CONFIG") or Path(__file__).resolve().parent.parent / "config.toml"
)
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.example.toml"

# Environment variables: they take precedence over config.toml, and the
# Connections screen shows them as read-only.
ENV_FIELDS = {
    ("radarr", "url"): "SWEEPER_RADARR_URL",
    ("radarr", "api_key"): "SWEEPER_RADARR_API_KEY",
    ("radarr", "enabled"): "SWEEPER_RADARR_ENABLED",
    ("sonarr", "url"): "SWEEPER_SONARR_URL",
    ("sonarr", "api_key"): "SWEEPER_SONARR_API_KEY",
    ("sonarr", "enabled"): "SWEEPER_SONARR_ENABLED",
    ("seerr", "url"): "SWEEPER_SEERR_URL",
    ("seerr", "api_key"): "SWEEPER_SEERR_API_KEY",
    ("seerr", "enabled"): "SWEEPER_SEERR_ENABLED",
    ("qbittorrent", "url"): "SWEEPER_QBIT_URL",
    ("qbittorrent", "username"): "SWEEPER_QBIT_USERNAME",
    ("qbittorrent", "password"): "SWEEPER_QBIT_PASSWORD",
    ("qbittorrent", "enabled"): "SWEEPER_QBIT_ENABLED",
    ("plex", "database"): "SWEEPER_PLEX_DB",
}


@dataclass
class Rule:
    name: str
    media: str  # "movie" | "series" | "season"
    enabled: bool = True
    never_watched: bool = False
    unwatched_days: int | None = None
    min_age_days: int = 0
    max_distinct_viewers: int | None = None

    def __post_init__(self) -> None:
        if self.media not in ("movie", "series", "season"):
            raise ValueError(t("règle « {name} » : media={media!r} invalide", name=self.name, media=self.media))
        if not self.never_watched and self.unwatched_days is None:
            raise ValueError(
                t("règle « {name} » : il faut never_watched ou unwatched_days", name=self.name)
            )


@dataclass
class SeedingPolicy:
    """Conditions to meet before removing a torrent from the client."""

    mode: str = "either"  # either | both | ignore
    min_ratio: float = 1.0
    min_seed_days: int = 30
    per_tracker: dict[str, dict[str, Any]] = field(default_factory=dict)

    def requirements_for(self, tracker_host: str) -> tuple[float, int]:
        # an exact hostname beats the group, which beats the default
        from .trackers import group_of

        override = self.per_tracker.get(tracker_host)
        if override is None:
            override = self.per_tracker.get(group_of(tracker_host), {})
        return (
            float(override.get("min_ratio", self.min_ratio)),
            int(override.get("min_seed_days", self.min_seed_days)),
        )

    def has_override(self, tracker_host: str) -> bool:
        from .trackers import group_of

        return (
            tracker_host in self.per_tracker or group_of(tracker_host) in self.per_tracker
        )

    def satisfied(self, tracker_host: str, ratio: float, seed_days: float) -> bool:
        if self.mode == "ignore":
            return True
        min_ratio, min_days = self.requirements_for(tracker_host)
        ratio_ok = ratio >= min_ratio
        days_ok = seed_days >= min_days
        return (ratio_ok and days_ok) if self.mode == "both" else (ratio_ok or days_ok)


@dataclass
class Protections:
    tags: list[str] = field(default_factory=lambda: ["keep"])
    titles: list[str] = field(default_factory=list)
    requested_within_days: int = 30
    keep_torrent_if_shared: bool = True
    #: spare what someone started without finishing ("Continue Watching")
    in_progress: bool = True
    #: Plex collections whose members are never candidates (Kometa, favorites…)
    collections: list[str] = field(default_factory=list)


@dataclass
class Limits:
    max_items_per_run: int = 25
    max_gb_per_run: float = 400.0


@dataclass
class Tier:
    """An optional pricing tier: a capacity and what it costs."""

    name: str
    gb: float
    price: float
    currency: str = ""


@dataclass
class Schedule:
    """Automatic run. Off by default: nothing gets deleted behind your back."""

    enabled: bool = False
    interval_minutes: int = 360
    apply_rules: bool = False
    apply_watchlist: bool = True


@dataclass
class Config:
    #: one entry per service; empty means "discover it"
    services: dict[str, dict] = field(default_factory=dict)
    #: None means "look in the usual places for this platform"
    plex_db: Path | None = None
    #: empty means "derive from the applications themselves"
    link_roots: list[Path] = field(default_factory=list)
    #: optional external command printing {"total":…,"used":…,"free":…} in GB
    quota_command: list[str] = field(default_factory=list)
    #: filesystem to measure when no quota command is available
    storage_path: Path = HOME
    state_dir: Path = HOME / ".local/state/sweeper"
    add_import_exclusion: bool = False
    delete_seerr_media: bool = True
    rules: list[Rule] = field(default_factory=list)
    seeding: SeedingPolicy = field(default_factory=SeedingPolicy)
    protect: Protections = field(default_factory=Protections)
    limits: Limits = field(default_factory=Limits)
    tiers: list[Tier] = field(default_factory=list)
    schedule: Schedule = field(default_factory=Schedule)

    def service(self, name: str) -> dict:
        return self.services.get(name, {})
    web_port: int = 29320
    web_host: str = "127.0.0.1"
    #: "en", "fr" or "auto" (from the environment)
    language: str = "auto"
    config_path: Path | None = None
    #: (service, field) pairs supplied by an environment variable
    env_fields: set = field(default_factory=set)


def _expand(value: str) -> Path:
    return Path(value.replace("~", str(HOME))).expanduser()


def _env_bool(value: str) -> bool:
    return value.strip().lower() not in ("0", "false", "no", "non", "off", "")


def load(path: Path | None = None) -> Config:
    explicit = path is not None
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        if explicit:
            # a typo in --config must never create a fresh config with the
            # example rules that `run --execute` would then act on
            raise FileNotFoundError(t("fichier de configuration introuvable : {path}", path=path))
        if EXAMPLE_CONFIG_PATH.is_file():
            # first launch, at the default location: start from the example,
            # editable afterwards from the Connections screen or by hand
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EXAMPLE_CONFIG_PATH, path)
            path.chmod(0o600)
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    services = {k: dict(v) for k, v in raw.get("services", {}).items() if isinstance(v, dict)}
    env_fields = set()
    for (service, key), variable in ENV_FIELDS.items():
        value = os.environ.get(variable)
        if value is None or value == "":
            continue
        services.setdefault(service, {})[key] = _env_bool(value) if key == "enabled" else value
        env_fields.add((service, key))
    paths = raw.get("paths", {})
    behaviour = raw.get("behaviour", {})

    storage = raw.get("storage", {})
    config = Config(
        services=services,
        plex_db=(
            _expand(services["plex"]["database"]) if services.get("plex", {}).get("database")
            else _expand(paths["plex_db"]) if paths.get("plex_db") else None
        ),
        link_roots=[_expand(p) for p in paths.get("link_roots", [])],
        quota_command=list(storage.get("quota_command", [])),
        storage_path=_expand(os.environ.get("SWEEPER_STORAGE_PATH") or storage.get("path", "~")),
        state_dir=_expand(os.environ.get("SWEEPER_STATE_DIR") or paths.get("state_dir", "~/.local/state/sweeper")),
        add_import_exclusion=bool(behaviour.get("add_import_exclusion", False)),
        delete_seerr_media=bool(behaviour.get("delete_seerr_media", True)),
    )

    if seeding := raw.get("seeding"):
        config.seeding = SeedingPolicy(
            mode=seeding.get("mode", "either"),
            min_ratio=float(seeding.get("min_ratio", 1.0)),
            min_seed_days=int(seeding.get("min_seed_days", 30)),
            per_tracker=seeding.get("per_tracker", {}),
        )
    if protect := raw.get("protect"):
        config.protect = Protections(
            tags=protect.get("tags", ["keep"]),
            titles=protect.get("titles", []),
            requested_within_days=int(protect.get("requested_within_days", 30)),
            keep_torrent_if_shared=bool(protect.get("keep_torrent_if_shared", True)),
            in_progress=bool(protect.get("in_progress", True)),
            collections=list(protect.get("collections", [])),
        )
    if limits := raw.get("limits"):
        config.limits = Limits(
            max_items_per_run=int(limits.get("max_items_per_run", 25)),
            max_gb_per_run=float(limits.get("max_gb_per_run", 400.0)),
        )

    config.tiers = sorted(
        (
            Tier(
                name=entry["name"],
                gb=float(entry["gb"]),
                price=float(entry.get("price", 0)),
                currency=entry.get("currency", ""),
            )
            for entry in raw.get("tier", [])
        ),
        key=lambda tier: tier.gb,
    )
    if schedule := raw.get("schedule"):
        config.schedule = Schedule(
            enabled=bool(schedule.get("enabled", False)),
            interval_minutes=max(int(schedule.get("interval_minutes", 360)), 15),
            apply_rules=bool(schedule.get("apply_rules", False)),
            apply_watchlist=bool(schedule.get("apply_watchlist", True)),
        )
    web = raw.get("web", {})
    config.web_port = int(web.get("port", 29320))
    config.web_host = web.get("host", "127.0.0.1")
    # one language for the whole instance: SWEEPER_LANG, then [ui], then the locale
    config.language = os.environ.get("SWEEPER_LANG") or raw.get("ui", {}).get("language") or "auto"
    i18n.set_language(config.language)
    config.config_path = path
    config.env_fields = env_fields

    config.rules = [
        Rule(
            name=entry["name"],
            media=entry["media"],
            enabled=bool(entry.get("enabled", True)),
            never_watched=bool(entry.get("never_watched", False)),
            unwatched_days=entry.get("unwatched_days"),
            min_age_days=int(entry.get("min_age_days", 0)),
            max_distinct_viewers=entry.get("max_distinct_viewers"),
        )
        for entry in raw.get("rule", [])
    ]
    if not config.rules:
        raise ValueError(t("aucune règle [[rule]] définie dans {path}", path=path))
    return config


# --------------------------------------------------------------------------
# Writing rules from the UI. Only the [[rule]] block is rewritten: comments
# and every other setting in the file are preserved.
# --------------------------------------------------------------------------

_RULE_KEYS = (
    "name", "media", "never_watched", "unwatched_days",
    "min_age_days", "max_distinct_viewers", "enabled",
)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_rules(rules: list[Rule]) -> str:
    blocks = []
    for rule in rules:
        lines = ["[[rule]]"]
        for key in _RULE_KEYS:
            value = getattr(rule, key, None)
            if value is None:
                continue
            if key == "never_watched" and not value:
                continue
            if key == "enabled" and value:
                continue  # true is the default
            lines.append(f"{key} = {_toml_value(value)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def write_rules(path: Path, rules: list[Rule]) -> None:
    """Replace the file's [[rule]] range with the given list."""
    lines = path.read_text(encoding="utf-8").splitlines()
    first = last = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[[rule]]":
            if first is None:
                first = index
            last = index
        elif (
            first is not None
            and stripped.startswith("[")
            and stripped != "[[rule]]"
            and not stripped.startswith("[seeding.per_tracker")
        ):
            break
    if first is None:
        raise ValueError(t("aucun bloc [[rule]] à remplacer dans {path}", path=path))

    # the range ends at the last value line: trailing comments and blank
    # lines belong to the next section, so keep them
    end = last + 1
    for index in range(last + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("["):
            break
        if stripped and not stripped.startswith("#"):
            end = index + 1

    rendered = render_rules(rules).splitlines()
    updated = lines[:first] + rendered + lines[end:]
    _save(path, updated)


_SCHEDULE_KEYS = ("enabled", "interval_minutes", "apply_rules", "apply_watchlist")


def write_schedule(path: Path, schedule: Schedule) -> None:
    """Replace the file's [schedule] section, or append it at the end."""
    lines = path.read_text(encoding="utf-8").splitlines()
    rendered = ["[schedule]"] + [
        f"{key} = {_toml_value(getattr(schedule, key))}" for key in _SCHEDULE_KEYS
    ]
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == "[schedule]"), None
    )
    if start is None:
        updated = lines + ["", *rendered]
    else:
        end = start + 1
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("["):
                break
            if stripped and not stripped.startswith("#"):
                end = index + 1
        updated = lines[:start] + rendered + lines[end:]
    _save(path, updated)


def write_seeding(path: Path, policy: SeedingPolicy) -> None:
    """Rewrite [seeding] and its [seeding.per_tracker.*] subtables."""
    lines = path.read_text(encoding="utf-8").splitlines()
    rendered = [
        "[seeding]",
        f"mode = {_toml_value(policy.mode)}",
        f"min_ratio = {_toml_value(policy.min_ratio)}",
        f"min_seed_days = {_toml_value(policy.min_seed_days)}",
    ]
    for name in sorted(policy.per_tracker):
        override = policy.per_tracker[name]
        if not override:
            continue
        rendered.append("")
        rendered.append(f'[seeding.per_tracker."{name}"]')
        for key in ("min_ratio", "min_seed_days"):
            if key in override:
                rendered.append(f"{key} = {_toml_value(override[key])}")

    start = next((i for i, line in enumerate(lines) if line.strip() == "[seeding]"), None)
    if start is None:
        updated = lines + ["", *rendered]
    else:
        end = start + 1
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            # the per_tracker subtables are part of the block being replaced
            if stripped.startswith("[") and not stripped.startswith("[seeding."):
                break
            if stripped and not stripped.startswith("#"):
                end = index + 1
        updated = lines[:start] + rendered + lines[end:]
    _save(path, updated)


_PROTECT_KEYS = ("tags", "titles", "requested_within_days", "keep_torrent_if_shared",
                 "in_progress", "collections")


def _toml_list(values: list) -> str:
    return "[" + ", ".join(_toml_value(v) for v in values) + "]"


def write_protect(path: Path, protect: Protections) -> None:
    """Replace the file's [protect] section, or append it at the end."""
    lines = path.read_text(encoding="utf-8").splitlines()
    rendered = ["[protect]"]
    for key in _PROTECT_KEYS:
        value = getattr(protect, key)
        rendered.append(
            f"{key} = {_toml_list(value) if isinstance(value, list) else _toml_value(value)}"
        )
    start = next((i for i, line in enumerate(lines) if line.strip() == "[protect]"), None)
    if start is None:
        updated = lines + ["", *rendered]
    else:
        end = start + 1
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("["):
                break
            if stripped and not stripped.startswith("#"):
                end = index + 1
        updated = lines[:start] + rendered + lines[end:]
    _save(path, updated)


_SERVICE_ORDER = ("enabled", "url", "api_key", "username", "password", "database",
                  "config_xml", "settings_json", "from_cross_seed", "host", "path_map")


def _toml_any(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_any(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_any(v)}" for k, v in value.items()) + " }"
    return _toml_value(value)


def write_service(path: Path, name: str, values: dict) -> None:
    """Rewrite the [services.<name>] table with these values, keeping the other keys.

    The file may contain API keys: it is reset to owner-only read/write (600)
    on every write.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    current = dict((raw.get("services") or {}).get(name) or {})
    for key, value in values.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    ordered = [k for k in _SERVICE_ORDER if k in current] + sorted(k for k in current if k not in _SERVICE_ORDER)
    rendered = [f"[services.{name}]"] + [f"{k} = {_toml_any(current[k])}" for k in ordered]

    lines = path.read_text(encoding="utf-8").splitlines()
    header = f"[services.{name}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        # insert after the last [services.*] table, otherwise at the end
        anchor = max((i for i, l in enumerate(lines) if l.strip().startswith("[services.")), default=None)
        if anchor is not None:
            end = anchor + 1
            while end < len(lines) and not lines[end].strip().startswith("["):
                end += 1
            updated = lines[:end] + rendered + [""] + lines[end:]
        else:
            updated = lines + ["", *rendered]
    else:
        end = start + 1
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("["):
                break
            if stripped and not stripped.startswith("#"):
                end = index + 1
        updated = lines[:start] + rendered + lines[end:]
    _save(path, updated)


def write_language(path: Path, language: str) -> None:
    """Set [ui] language, adding the table when it is missing."""
    if language not in i18n.LANGUAGES:
        raise ValueError(t("langue inconnue : {language}", language=language))
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "[ui]"), None)
    if start is None:
        updated = lines + ["", "[ui]", f'language = "{language}"']
    else:
        end = start + 1
        while end < len(lines) and not lines[end].strip().startswith("["):
            end += 1
        body = [line for line in lines[start + 1:end] if not line.strip().startswith("language")]
        updated = lines[:start + 1] + [f'language = "{language}"'] + body + lines[end:]
    _save(path, updated)


def _save(path: Path, lines: list[str]) -> None:
    """Write the config, keeping the previous version as .bak.

    The file may hold API keys and passwords: both copies are owner-only (600)
    — on a shared machine the default umask would leave them readable by all.
    """
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    backup.chmod(0o600)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
