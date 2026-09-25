#!/usr/bin/env python3
"""sweeper — media retention for Plex + Radarr/Sonarr + Seerr + qBittorrent.

Unlike Maintainerr, sweeper doesn't just delete the media: it also removes the
torrent (and its cross-seed twins, otherwise the hardlink keeps the bytes),
then the request and the media entry in Seerr, which makes the title
requestable again for users.

Everything is a dry run by default. Pass --execute to act.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sweeper import config as config_module  # noqa: E402
from sweeper import report  # noqa: E402
from sweeper.arr import Radarr, Sonarr  # noqa: E402
from sweeper import discovery  # noqa: E402
from sweeper.engine import Sweeper  # noqa: E402
from sweeper.i18n import t  # noqa: E402
from sweeper.linkmap import LinkMap  # noqa: E402
from sweeper.plexdb import load_snapshot  # noqa: E402
from sweeper.qbit import QBittorrent  # noqa: E402
from sweeper.seerr import Seerr  # noqa: E402


DESCRIPTION = """sweeper — rétention média pour Plex + Radarr/Sonarr + Seerr + qBittorrent.

Contrairement à Maintainerr, sweeper ne se contente pas de supprimer le média :
il retire aussi le torrent (et ses jumeaux cross-seed, sans quoi le hardlink
garde les octets) puis la request et la fiche media côté Seerr, ce qui rend le
titre à nouveau demandable.

Tout est en simulation par défaut. Il faut --execute pour agir."""


def build_services(cfg: config_module.Config, with_seerr: bool = True):
    from sweeper import services as services_module

    svc = services_module.connect(cfg, with_seerr=with_seerr)
    return svc.radarr, svc.sonarr, svc.seerr, svc.qbit


def plex_snapshot(cfg: config_module.Config):
    from sweeper import services as services_module

    plex_db = services_module.plex_database(cfg)
    if plex_db is None:
        raise SystemExit(t("base Plex introuvable — indique [services.plex] database dans la configuration"))
    return load_snapshot(plex_db, services_module.PathMap(cfg.service("plex").get("path_map", ())))


def cmd_check(args: argparse.Namespace) -> int:
    from sweeper import services as services_module

    cfg = config_module.load(args.config)
    ok = True
    failed = False
    fail = t("ÉCHEC")
    for entry in services_module.probe_all(cfg):
        if not entry["activé"]:
            print(f"  {entry['nom']:<12} —    {t('désactivé')}")
        elif entry["ok"]:
            print(f"  {entry['nom']:<12} OK   {entry['détail']}")
        else:
            ok = False
            failed = failed or entry["service"] != "seerr"
            print(f"  {entry['nom']:<12} {fail:<4} {entry['détail']}")
    if failed:
        # without these the rest of the checks cannot run
        print()
        print(t("Renseigne les services manquants dans {path}, par variables "
                "d'environnement (SWEEPER_*), ou depuis l'écran Connexions de "
                "l'interface web (python3 sweeper.py serve).", path=cfg.config_path))
        return 1
    radarr, sonarr, seerr, qbit = build_services(cfg)
    try:
        snapshot = plex_snapshot(cfg)
        print(f"  {'':<12}      " + t(
            "{movies} films, {shows} séries, {views} visionnages",
            movies=len(snapshot.movies_by_path), shows=len(snapshot.shows),
            views=snapshot.total_views))
    except Exception as error:  # noqa: BLE001
        ok = False
        print(f"  {'Plex':<12} {fail:<4} {type(error).__name__}: {error}")
    try:
        from sweeper.linkmap import LinkMap, uncovered_torrents
        library_roots = sorted(
            {Path(m.path).parent for m in radarr.movies() if m.path}
            | {Path(s.path).parent for s in sonarr.series() if s.path}
        )
        roots = cfg.link_roots or discovery.auto_link_roots(
            radarr, sonarr, qbit,
            discovery.first_existing([Path.home() / ".cross-seed/config.js"]),
        )
        gap = uncovered_torrents(qbit.torrents(), roots)
        unseen = [m.file_path for m in radarr.movies() if m.file_path and not Path(m.file_path).is_file()]
        if unseen:
            ok = False
            print(f"  {'⚠':<12} " + t(
                "{n} fichier(s) annoncé(s) par Radarr introuvable(s) ici (ex. {example}) "
                "— correspondance de chemins (path_map) à configurer ?",
                n=len(unseen), example=unseen[0]))
        if gap:
            print(f"  {'⚠':<12} " + t(
                "{n} torrent(s) hors des racines surveillées ({gb:.0f} Go) — le calcul "
                "d'octets libérés peut sous-estimer ce qui reste ailleurs.",
                n=len(gap), gb=sum(torrent.size for torrent in gap) / 1e9))
            for torrent in gap[:3]:
                print(f"               {torrent.save_path}")
        else:
            print(f"  {t('racines'):<12} OK   " + t(
                "{n} racine(s), tous les torrents couverts", n=len(roots)))
    except Exception as error:  # noqa: BLE001
        ok = False
        print(f"  {t('racines'):<12} {fail:<4} {type(error).__name__}: {error}")

    print()
    print("  " + t("{n} règle(s) chargée(s) :", n=len(cfg.rules)))
    for rule in cfg.rules:
        state = "" if rule.enabled else t(" (désactivée)")
        criterion = (
            t("jamais vu")
            if rule.never_watched
            else t("pas vu depuis {days} j", days=rule.unwatched_days)
        )
        print(f"    · {rule.name}{state} — " + t(
            "{media}, {criterion}, en place depuis ≥ {days} j",
            media=rule.media, criterion=criterion, days=rule.min_age_days))
    return 0 if ok else 1


def _make_plan(cfg: config_module.Config):
    radarr, sonarr, seerr, qbit = build_services(cfg)
    snapshot = plex_snapshot(cfg)
    library_roots = sorted(
        {Path(m.path).parent for m in radarr.movies() if m.path}
        | {Path(s.path).parent for s in sonarr.series() if s.path}
    )
    roots = cfg.link_roots or discovery.auto_link_roots(
        radarr, sonarr, qbit,
        discovery.first_existing([Path.home() / ".cross-seed/config.js"]),
    )
    link_map = LinkMap(roots, library_roots).scan().index_torrents(qbit.torrents())
    engine = Sweeper(cfg, radarr, sonarr, seerr, qbit)
    return engine, engine.build_plan(snapshot, link_map)


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    _engine, plan = _make_plan(cfg)
    if args.json:
        print(report.to_json(plan))
    else:
        print(report.render(plan, colour=not args.no_color))
        print()
        print(t("Simulation uniquement — relancer avec « run --execute » pour appliquer."))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    engine, plan = _make_plan(cfg)
    print(report.render(plan, colour=not args.no_color))
    print()
    if not plan.candidates:
        return 0
    if not args.execute:
        print(t("Simulation uniquement — ajouter --execute pour appliquer."))
        return 0
    if not args.yes:
        answer = input(t(
            "Supprimer {n} élément(s) et libérer {size} ? [oui/non] ",
            n=len(plan.candidates), size=report.gb(plan.freed_bytes),
        ))
        if answer.strip().lower() not in ("oui", "o", "yes", "y"):
            print(t("Annulé."))
            return 1
    results = engine.execute(plan)
    print()
    failures = 0
    for record in results:
        status = t("ÉCHEC") if record["erreurs"] else "OK"
        failures += 1 if record["erreurs"] else 0
        print(f"[{status}] {record['titre']} — {report.gb(record['octets_libérés'])}")
        for step in record["étapes"]:
            print(f"        · {step}")
        for error in record["erreurs"]:
            print(f"        ! {error}")
    print()
    print(t(
        "{done}/{total} traité(s), {size} libérés. Journal : {log}",
        done=len(results) - failures, total=len(results),
        size=report.gb(plan.freed_bytes), log=cfg.state_dir / "history.jsonl",
    ))
    return 1 if failures else 0


def cmd_history(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    path = cfg.state_dir / "history.jsonl"
    if not path.is_file():
        print(t("Aucun historique."))
        return 0
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    total = sum(r.get("octets_libérés", 0) for r in records)
    for record in records[-args.limit :]:
        import datetime as dt

        when = dt.datetime.fromtimestamp(record["horodatage"]).strftime("%Y-%m-%d %H:%M")
        print(f"{when}  {report.gb(record['octets_libérés']):>10}  {record['titre']}")
    print()
    print(t("{n} suppression(s) au total, {size} libérés.", n=len(records), size=report.gb(total)))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Archive the configuration and state: do this before an upgrade.

    The deletion log is the only record of what was removed; the queue and the
    usage readings are months of context. The access token is included only
    when explicitly requested.
    """
    import tarfile
    import time as time_module

    cfg = config_module.load(args.config)
    stamp = time_module.strftime("%Y%m%d-%H%M%S")
    target = Path(args.out) if args.out else Path.cwd() / f"sweeper-export-{stamp}.tar.gz"
    files = []
    if cfg.config_path and cfg.config_path.is_file():
        files.append((cfg.config_path, "config.toml"))
    for name in ("history.jsonl", "watchlist.json", "usage.jsonl", "jobs.jsonl"):
        path = cfg.state_dir / name
        if path.is_file():
            files.append((path, f"state/{name}"))
    if args.with_token and (cfg.state_dir / "token").is_file():
        files.append((cfg.state_dir / "token", "state/token"))
    with tarfile.open(target, "w:gz") as archive:
        for path, arcname in files:
            archive.add(path, arcname=arcname)
    print(t("Écrit {path} ({kb:.0f} ko) :", path=target, kb=target.stat().st_size / 1e3))
    for path, arcname in files:
        print(f"  {arcname:<22} " + t("{kb:7.1f} ko", kb=path.stat().st_size / 1e3))
    if not args.with_token:
        print("  " + t("(jeton d'accès exclu — --with-token pour l'inclure)"))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Probe the machine and write a config.toml from what is found."""
    from sweeper.arr import Radarr, Sonarr
    from sweeper.qbit import QBittorrent

    target = args.config or config_module.DEFAULT_CONFIG_PATH
    if target.exists() and not args.force:
        print(t("{path} existe déjà — --force pour l'écraser.", path=target))
        return 1

    lines: list[str] = []
    found: dict[str, str] = {}
    print(t("Recherche des services…"))

    for app in ("Radarr", "Sonarr"):
        path = discovery.first_existing(discovery.arr_config_candidates(app))
        if path:
            access = discovery.read_arr_config(path)
            found[app.lower()] = access.base_url
            print(f"  {app:<12} {access.base_url}   ({path})")
            lines.append(f"[services.{app.lower()}]\n")
        else:
            print(f"  {app:<12} " + t("introuvable — renseigne url et api_key à la main"))
            lines.append(
                f'[services.{app.lower()}]\n'
                f'# url = "http://127.0.0.1:{7878 if app == "Radarr" else 8989}"\n'
                f'# api_key = "..."\n'
            )

    seerr_path = discovery.first_existing(discovery.seerr_settings_candidates())
    if seerr_path:
        print(f"  {'Seerr':<12} " + t("réglages dans {path}", path=seerr_path))
        print(f"  {'':<12} " + t("→ indique son url ci-dessous, le port n'est pas dans ce fichier"))
        lines.append('[services.seerr]\n# url = "http://127.0.0.1:5055"\n')
    else:
        print(f"  {'Seerr':<12} " + t("introuvable (facultatif)"))
        lines.append('# [services.seerr]\n# url = "http://127.0.0.1:5055"\n')

    cross_seed = discovery.first_existing([Path.home() / ".cross-seed/config.js"])
    if cross_seed:
        print(f"  {'qBittorrent':<12} " + t("identifiants via cross-seed ({path})", path=cross_seed))
        lines.append("[services.qbittorrent]\n")
    else:
        print(f"  {'qBittorrent':<12} " + t("introuvable — renseigne url/username/password"))
        lines.append(
            '[services.qbittorrent]\n# url = "http://127.0.0.1:8080"\n'
            '# username = "admin"\n# password = "..."\n'
        )

    plex = discovery.first_existing(discovery.plex_database_candidates())
    print(f"  {'Plex':<12} {plex or t('introuvable — renseigne [services.plex] database à la main')}")
    lines.append("\n[paths]\n")
    if not plex:
        lines.append('# plex_db = "/path/to/com.plexapp.plugins.library.db"\n')
    lines.append('state_dir = "~/.local/state/sweeper"\n')

    roots: list[Path] = []
    if found.get("radarr") and found.get("sonarr") and cross_seed:
        try:
            radarr = Radarr(discovery.resolve_arr("Radarr", {}))
            sonarr = Sonarr(discovery.resolve_arr("Sonarr", {}))
            qbit = QBittorrent(discovery.resolve_qbit({}))
            roots = discovery.auto_link_roots(radarr, sonarr, qbit, cross_seed)
        except Exception as error:  # noqa: BLE001
            print("  " + t("racines de hardlink : impossible de les déduire ({error})", error=error))
    if roots:
        print("  " + t("racines de hardlink déduites :"))
        for root in roots:
            print(f"      {root}")
        lines.append("# derived automatically at each run; listed here for reference\n")
        lines.append(
            "# link_roots = [" + ", ".join(f'"{r}"' for r in roots) + "]\n"
        )

    example = config_module.DEFAULT_CONFIG_PATH.parent / "config.example.toml"
    rest = ""
    if example.is_file():
        text = example.read_text(encoding="utf-8")
        rest = text[text.index("[behaviour]"):]

    target.write_text(
        "# sweeper — generated by `sweeper.py init`\n"
        "# Review before use; see config.example.toml for every option.\n\n"
        + "\n".join(lines) + "\n" + rest,
        encoding="utf-8",
    )
    print()
    print(t("{path} écrit. Vérifie-le, puis lance : python3 sweeper.py check", path=target))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from sweeper.web import serve

    import signal

    cfg = config_module.load(args.config)
    # PID 1 in a container ignores SIGTERM unless it installs a handler:
    # without this, `docker stop` waits ten seconds and kills the process
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(cfg, host=args.host, port=args.port)
    return 0


def cmd_init_tags(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    radarr, sonarr, _seerr, _qbit = build_services(cfg, with_seerr=False)
    for label in cfg.protect.tags:
        for name, client in (("Radarr", radarr), ("Sonarr", sonarr)):
            existing = {value.lower() for value in client.tags().values()}
            if label.lower() in existing:
                print(f"  {name}: " + t("tag « {tag} » déjà présent", tag=label))
                continue
            client.create_tag(label)
            print(f"  {name}: " + t("tag « {tag} » créé", tag=label))
    print()
    print(t("Pose ce tag sur un film ou une série pour l'exclure définitivement."))
    return 0


def main() -> int:
    from sweeper import i18n

    i18n.set_language("auto")  # the config is not read yet: follow the environment
    parser = argparse.ArgumentParser(
        prog="sweeper", description=t(DESCRIPTION), formatter_class=argparse.RawDescriptionHelpFormatter
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help=t("chemin du config.toml"))
    common.add_argument("--no-color", action="store_true", help=t("sortie sans couleurs"))
    for option in common._actions:
        if option.dest != "help":
            parser._add_action(option)
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", parents=[common], help=t("teste l'accès aux services"))
    check.set_defaults(func=cmd_check)

    plan = sub.add_parser("plan", parents=[common], help=t("simulation : ce qui partirait"))
    plan.add_argument("--json", action="store_true", help=t("sortie JSON"))
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", parents=[common], help=t("applique le nettoyage"))
    run.add_argument("--execute", action="store_true", help=t("passe à l'acte"))
    run.add_argument("--yes", action="store_true", help=t("sans confirmation (cron)"))
    run.set_defaults(func=cmd_run)

    history = sub.add_parser("history", parents=[common], help=t("journal des suppressions"))
    history.add_argument("--limit", type=int, default=30)
    history.set_defaults(func=cmd_history)

    export = sub.add_parser("export", parents=[common], help=t("archive config + état (tar.gz)"))
    export.add_argument("--out", default=None, help=t("chemin de l'archive"))
    export.add_argument("--with-token", action="store_true", help=t("inclure le jeton d'accès"))
    export.set_defaults(func=cmd_export)

    setup = sub.add_parser("init", parents=[common], help=t("génère un config.toml"))
    setup.add_argument("--force", action="store_true", help=t("écrase un fichier existant"))
    setup.set_defaults(func=cmd_init)

    web = sub.add_parser("serve", parents=[common], help=t("lance l'interface web"))
    web.add_argument("--port", type=int, default=None)
    web.add_argument("--host", default=None)
    web.set_defaults(func=cmd_serve)

    tags = sub.add_parser("init-tags", parents=[common], help=t("crée les tags de protection"))
    tags.set_defaults(func=cmd_init_tags)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        args.json = False
        return cmd_plan(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
