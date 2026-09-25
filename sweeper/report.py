"""Report formatting (console and JSON)."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter

from .engine import Candidate, Plan
from .i18n import language, t

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED = "\033[32m", "\033[33m", "\033[31m"


def gb(value: int) -> str:
    if language() == "fr":
        return f"{value / 10**9:,.1f} Go".replace(",", " ")
    return f"{value / 10**9:,.1f} GB"


def _date(timestamp: int | None) -> str:
    if not timestamp:
        return t("jamais")
    return dt.date.fromtimestamp(timestamp).isoformat()


def _line(candidate: Candidate, colour: bool) -> str:
    kind = {"movie": t("film"), "series": t("série"), "season": t("saison")}[candidate.kind]
    if candidate.unmanaged:
        kind += "*"   # * = outside *arr, deleted directly on disk
    torrents = len(candidate.impacts)
    seerr = ""
    if candidate.seerr_request_ids:
        seerr = t(", {n} request Seerr", n=len(candidate.seerr_request_ids))
    if candidate.seerr_media_id and candidate.seerr_media_deletable:
        seerr += t(", fiche Seerr")
    head = f"{gb(candidate.freed_bytes):>10}  {kind:<6} {candidate.title[:48]:<48}"
    detail = t(
        "vu: {seen} · {viewers} spectateur(s) · en place depuis {age:.0f} j · {torrents} torrent(s){seerr}",
        seen=_date(candidate.stats.last_viewed), viewers=candidate.stats.distinct_viewers,
        age=candidate.age_days, torrents=torrents, seerr=seerr,
    )
    if colour:
        return f"{head}\n{DIM}            └─ {detail}{RESET}"
    return f"{head}\n            └─ {detail}"


def render(plan: Plan, colour: bool = True, show_skipped: int = 12) -> str:
    out: list[str] = []
    bold = BOLD if colour else ""
    reset = RESET if colour else ""
    green = GREEN if colour else ""
    yellow = YELLOW if colour else ""

    labels = {"films": t("films"), "séries": t("séries"), "fiches_seerr": t("fiches Seerr")}
    out.append(
        f"{bold}{t('Analysé :')}{reset} "
        + " · ".join(f"{v} {labels.get(k, k)}" for k, v in plan.scanned.items())
    )
    out.append("")

    if plan.candidates:
        out.append(f"{bold}{green}" + t(
            "À SUPPRIMER — {n} élément(s), {size} réellement libérés",
            n=len(plan.candidates), size=gb(plan.freed_bytes)) + reset)
        out.append("")
        for candidate in plan.candidates:
            out.append(_line(candidate, colour))
        out.append("")
    else:
        out.append(f"{yellow}{t('Aucun élément ne remplit les règles ce passage.')}{reset}")
        out.append("")

    if plan.skipped:
        reasons = Counter(
            candidate.blockers[0].split(" — ")[0]
            for candidate in plan.skipped
            if candidate.blockers
        )
        held = sum(c.library_bytes for c in plan.skipped)
        out.append(f"{bold}" + t(
            "RETENUS — {n} élément(s) ({size} en bibliothèque)",
            n=len(plan.skipped), size=gb(held)) + reset)
        for reason, count in reasons.most_common():
            out.append(f"  {count:>4} × {reason}")
        if show_skipped:
            out.append("")
            worst = sorted(plan.skipped, key=lambda c: c.library_bytes, reverse=True)
            for candidate in worst[:show_skipped]:
                blocker = candidate.blockers[0] if candidate.blockers else "?"
                out.append(
                    f"  {gb(candidate.library_bytes):>10}  "
                    f"{candidate.title[:44]:<44} {DIM if colour else ''}{blocker[:60]}"
                    f"{reset}"
                )
    return "\n".join(out)


def to_json(plan: Plan) -> str:
    payload = {
        "analysé": plan.scanned,
        "octets_libérés": plan.freed_bytes,
        "à_supprimer": [
            {
                "clé": c.key,
                "type": c.kind,
                "hors_arr": c.unmanaged,
                "titre": c.title,
                "règle": c.rule,
                "octets_libérés": c.freed_bytes,
                "octets_bibliothèque": c.library_bytes,
                "dernier_visionnage": _date(c.stats.last_viewed),
                "spectateurs": c.stats.distinct_viewers,
                "âge_jours": round(c.age_days, 1),
                "fichiers": c.files,
                "torrents": [
                    {
                        "nom": i.torrent.name,
                        "hash": i.torrent.hash,
                        "tracker": i.torrent.tracker_host,
                        "ratio": round(i.torrent.ratio, 2),
                        "seed_jours": round(i.torrent.seeding_days, 1),
                        "catégorie": i.torrent.category,
                    }
                    for i in c.impacts
                ],
                "seerr": {
                    "media_id": c.seerr_media_id,
                    "requests": c.seerr_request_ids,
                    "supprime_fiche": c.seerr_media_deletable,
                },
            }
            for c in plan.candidates
        ],
        "retenus": [
            {
                "clé": c.key,
                "type": c.kind,
                "titre": c.title,
                "octets_bibliothèque": c.library_bytes,
                "blocages": c.blockers,
                "dans_combien_de_jours": c.unblocks_in_days,
            }
            for c in plan.skipped
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
