# sweeper

[![tests](https://github.com/Briaccc/sweeper/actions/workflows/test.yml/badge.svg)](https://github.com/Briaccc/sweeper/actions/workflows/test.yml)

**Retention for Plex libraries managed by Radarr and Sonarr — that actually
frees the disk space.**

[Français](README.fr.md)

sweeper decides what can go from the real Plex watch history of every account,
then removes it properly. It is in the spirit of Maintainerr, with the two steps
Maintainerr leaves out:

1. **It removes the torrent too — and its cross-seed twins.** Your library is
   usually hardlinked to your downloads: deleting a movie in Radarr removes one
   name out of several, the inode stays, and not a single byte is freed.
   sweeper follows every hardlink, finds each torrent involved and reports only
   the bytes whose *every* name disappears.
2. **It cleans up Seerr** (Overseerr, Jellyseerr): it deletes the requests *and*
   the media entry, so the title can be requested again instead of staying
   "Available" forever.

Everything is a dry run until you say otherwise, and sweeper never deletes a
file itself: every deletion is carried out by the application that owns it
(Radarr, Sonarr, qBittorrent).

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Connecting the services](#connecting-the-services)
- [Containers and path mapping](#containers-and-path-mapping)
- [The web interface](#the-web-interface)
- [Rules](#rules)
- [Safeguards](#safeguards)
- [What happens to a deleted item](#what-happens-to-a-deleted-item)
- [Command line](#command-line)
- [Automation](#automation)
- [Language](#language)
- [Backups](#backups)
- [Tests](#tests)
- [Limitations](#limitations)

## Requirements

| | |
|---|---|
| **Python** | 3.11 or newer, plus `requests` |
| **Plex Media Server** | required — sweeper reads its library database file (a read-only copy), so it must run on the same machine or have Plex's config folder mounted |
| **Radarr / Sonarr** | at least one of them (API v3, i.e. Radarr 3+ and Sonarr 3/4) |
| **qBittorrent** | optional — the only torrent client supported; disable it on a Usenet-only setup |
| **Seerr / Overseerr / Jellyseerr** | optional — without it, requests are simply not cleaned up |

## Quick start

### Native

```bash
git clone https://github.com/Briaccc/sweeper.git
cd sweeper
pip install -r requirements.txt
python3 sweeper.py serve
```

On first start, `config.toml` is created from `config.example.toml` and the
server prints a URL with an access token:

```
  http://127.0.0.1:29320/?token=…
```

Open it. If Radarr, Sonarr, qBittorrent or Plex run on the same machine with a
standard install, sweeper finds them by itself. Otherwise the **Connections**
screen opens: enter each URL and API key, click **Test**, then **Save**.

### Docker

```bash
cp docker-compose.example.yml docker-compose.yml   # then edit it
docker compose up -d
docker compose logs sweeper                        # shows the URL with the token
```

The example mounts Plex's config folder read-only, your media and a `/config`
volume that holds `config.toml` and the state. Read
[Containers and path mapping](#containers-and-path-mapping) before you start.

## Connecting the services

Each setting is resolved in this order — the first one found wins:

1. **Environment variables** (`SWEEPER_*`, below) — handy for Docker; a field
   set this way is greyed out in the interface.
2. **`config.toml`** — what the Connections screen writes.
3. **Auto-discovery** — Radarr/Sonarr's `config.xml`, Seerr's `settings.json`,
   qBittorrent credentials from a cross-seed config, the Plex database in the
   usual places for Linux, macOS, Windows and common container mounts.

**Where to find the API keys:** in Radarr and Sonarr, *Settings → General →
Security → API Key*; in Seerr/Overseerr/Jellyseerr, *Settings → General → API
Key*. qBittorrent uses its web UI username and password.

**How they are kept:** `config.toml` (and its `.bak`) is written owner-only
(`chmod 600`) and ignored by git. The interface never shows a saved key or
password again — only its last four characters — and leaving the field empty
keeps the saved one. A connection is tested before it is saved.

| Variable | Setting |
|---|---|
| `SWEEPER_RADARR_URL`, `SWEEPER_RADARR_API_KEY` | Radarr |
| `SWEEPER_SONARR_URL`, `SWEEPER_SONARR_API_KEY` | Sonarr |
| `SWEEPER_SEERR_URL`, `SWEEPER_SEERR_API_KEY` | Seerr / Overseerr / Jellyseerr |
| `SWEEPER_QBIT_URL`, `SWEEPER_QBIT_USERNAME`, `SWEEPER_QBIT_PASSWORD` | qBittorrent |
| `SWEEPER_RADARR_ENABLED`, `SWEEPER_SONARR_ENABLED`, `SWEEPER_SEERR_ENABLED`, `SWEEPER_QBIT_ENABLED` | `false` to disable a service |
| `SWEEPER_PLEX_DB` | path to `com.plexapp.plugins.library.db` |
| `SWEEPER_CONFIG` | where `config.toml` lives (Docker: `/config/config.toml`) |
| `SWEEPER_STATE_DIR` | where the state lives (Docker: `/config/state`) |
| `SWEEPER_STORAGE_PATH` | the disk whose usage the dashboard shows (Docker: your data mount) |
| `SWEEPER_LANG` | `en` or `fr` |

**Disabling a service.** Every service except Plex can be turned off, from the
Connections screen or with `enabled = false`: no qBittorrent on a Usenet-only
setup, no Sonarr if you only keep movies. sweeper then works with what is
left — without a torrent client there are simply no torrents to remove.

**The Plex database** is usually at:

| Install | Path |
|---|---|
| Linux package | `/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| Docker (linuxserver, official) | `<config volume>/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| macOS | `~/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| Windows | `%LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases\com.plexapp.plugins.library.db` |

sweeper copies it to a temporary file before reading, so it never locks the
database Plex is using.

## Containers and path mapping

sweeper computes hardlinks from the files themselves, so it must **see the same
files** Radarr, Sonarr and qBittorrent see. The simplest way is to mount your
data at the same path in every container (the usual `/data` single-tree
layout): nothing else to do.

If the paths differ, tell sweeper how to translate them, per service:

```toml
[services.radarr]
url = "http://radarr:7878"
path_map = [["/movies", "/data/media/movies"]]   # [path in Radarr, path sweeper sees]

[services.qbittorrent]
path_map = [["/downloads", "/data/torrents"]]

[services.plex]
database = "/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
path_map = [["/media", "/data/media"]]
```

If sweeper cannot find a file an application reports, it says so — a banner in
the interface, a warning in `sweeper.py check` — and **blocks those items**:
without seeing the files, it could not see the torrents holding them either, and
its seed and only-copy safeguards would be blind.

## The web interface

Four tabs; the essentials up front, the rest folded away.

- **Dashboard** — three figures (usage, freeable now, freeable within 30 days),
  where the bytes go, hosting plans with the saving and a saturation forecast,
  usage over time.
- **Library** — one list, a search box, a "what to show" selector (never
  watched, dormant, candidates, blocked, being watched, protected, outside
  *arr), and a **Library / Seed-only** toggle for data only a torrent still
  holds. Series expand into seasons. Select, then **Delete**, **Queue** (delete
  once seeding obligations are met), **Protect**, or **Adopt** what Plex serves
  without any *arr.
- **Rules** — the rules with a live preview of what they would catch, the
  automatic run (off by default), the queue, protected Plex collections.
- **More** — Connections, Requests (who asks for what, and whether anyone
  watches it), Trackers (seeding obligations), monthly growth, health, deletion
  history.

**Access.** The server listens on `127.0.0.1` only by default. Reach it through
a reverse proxy (it marks the cookie `Secure` when the proxy sends
`X-Forwarded-Proto: https`) or an SSH tunnel:

```bash
ssh -N -L 29320:127.0.0.1:29320 you@your-server
```

Without the token only the sign-in page is served. The token is printed at
start-up and kept in the state folder (`~/.local/state/sweeper/token`, or
`/config/state/token` in Docker); paste it once and it is remembered in an
`HttpOnly` cookie. Delete the file and restart to revoke it. Repeated failed
sign-ins are slowed down, and every response carries a strict
Content-Security-Policy, `nosniff`, `X-Frame-Options: DENY` and `no-store`.

## Rules

The first rule that matches wins, so order matters — drag rules in the
interface or reorder them in the file. Each rule pairs a watch criterion with a
minimum age in the library; without that guard you would delete new arrivals
nobody has had time to watch.

```toml
[[rule]]
name = "series nobody ever started"
media = "series"          # movie | series | season
never_watched = true      # or: unwatched_days = 180
min_age_days = 45
# max_distinct_viewers = 1  # spare what several people watched
```

A deleted season is also unmonitored in Sonarr so it does not come back.

## Safeguards

- **Dry run by default.** `run` needs `--execute`; the automatic run is off
  until you turn it on.
- **Seeding obligations.** An item whose torrent has not reached its ratio or
  seeding time is postponed, never deleted — configurable per tracker. Hosts of
  the same tracker share one rule.
- **Only copy.** A torrent that points straight at a library file (imported by
  move) is never deleted with its data. Never overridable.
- **Files sweeper cannot see** (see path mapping) block the item. Never
  overridable.
- **Outside \*arr.** What Plex serves without Radarr/Sonarr must be adopted
  first — sweeper hands the folder to the app, in place, nothing is moved or
  downloaded — because sweeper never deletes files itself.
- **Being watched.** Anything on someone's *Continue Watching* is spared.
- **Protection tag** (`keep`, created with `sweeper.py init-tags`), protected
  titles and **protected Plex collections**.
- **Recent request.** Nothing requested on Seerr in the last 30 days.
- **Season packs.** A torrent that also seeds episodes you keep stays.
- **Caps per run.** 25 items and 400 GB by default.
- **Log.** Every deletion is written to `history.jsonl` with its paths,
  torrents, trackers and TMDB/TVDB ID — enough to bring a title back with one
  click.

## What happens to a deleted item

| Step | Service | Action |
|---|---|---|
| 1 | Radarr / Sonarr | delete the entry and its files (`deleteFiles=true`) |
| 2 | qBittorrent | delete the torrents involved **with their data** |
| 3 | Seerr | `DELETE /request/{id}`, then `DELETE /media/{id}` |

`add_import_exclusion = false` keeps the title downloadable if someone asks for
it again; set it to `true` to ban it for good.

## Command line

```bash
python3 sweeper.py serve                # web interface
python3 sweeper.py check                # test every service and the hardlink coverage
python3 sweeper.py plan                 # dry run: what would go (the default command)
python3 sweeper.py plan --json          # same, for scripts
python3 sweeper.py run --execute        # apply, with confirmation
python3 sweeper.py run --execute --yes  # no confirmation (cron)
python3 sweeper.py history              # deletion log
python3 sweeper.py init                 # write a config.toml from what is found
python3 sweeper.py init-tags            # create the "keep" tag in Radarr/Sonarr
python3 sweeper.py export               # archive config + state
```

## Automation

The **automatic run** (Rules tab) processes the queue and, if you tick it, the
rules, at a regular interval — through the same job queue as the interface, so
it can never land in the middle of a deletion you started. It is off by
default.

Prefer cron? Use one or the other, not both:

```cron
30 4 * * 1 cd /path/to/sweeper && python3 sweeper.py run --execute --yes >> sweeper.log 2>&1
```

To keep the interface running across reboots without Docker, a systemd user
service or an `@reboot` cron line starting `python3 sweeper.py serve` will do.

## Language

The interface, the command line and every message exist in **English** and
**French**. Pick the language with the selector in the header — it is saved in
`config.toml` (`[ui] language`) — or force it with `SWEEPER_LANG`. Left unset,
sweeper follows the system locale.

## Backups

```bash
python3 sweeper.py export               # sweeper-export-<date>.tar.gz
python3 sweeper.py export --with-token  # include the access token
```

The archive holds `config.toml` and the state: the deletion log (the only
record of what went), the queue, usage readings and recent jobs.

## Tests

```bash
npm install     # once: jsdom, used only by the tests
npm test
```

- `test_linkmap.py` — the hardlink arithmetic, the one piece of logic that could
  destroy data, on a real temporary tree with real inodes.
- `test_tidy.py` — moving bare files into folders, with real renames.
- `test_i18n.py` — every message exists in both languages, placeholders
  included, and no French text escaped translation.
- `test_e2e.py` — the whole thing, end to end, against fake Radarr, Sonarr,
  Seerr and qBittorrent that really act on a temporary disk, a fake Plex
  database, real hardlinks and cross-seed. Every CLI command and API route
  runs; every safeguard has its case; the files left, the bytes actually freed
  and every call each service received are checked. It runs three times: same
  paths everywhere, container-style paths with `path_map`, and container paths
  *without* mapping (nothing may be deleted). The interface is rendered in
  jsdom in both languages along the way.

`node test_webui.mjs <state.json> [--lang en]` renders the interface against
any saved `/api/state` payload.

## Limitations

- **Plex only** — no Jellyfin or Emby: watch history comes from Plex's database.
- **qBittorrent only** as a torrent client.
- Radarr/Sonarr **API v3** (Radarr 3+, Sonarr 3 and 4).
- Hosting plans (`[[tier]]`) and a provider quota command (`quota_command`)
  are optional extras for the dashboard; without them sweeper measures the
  filesystem.

## License

MIT
