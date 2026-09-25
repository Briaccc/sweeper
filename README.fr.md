# sweeper

[![tests](https://github.com/Briaccc/sweeper/actions/workflows/test.yml/badge.svg)](https://github.com/Briaccc/sweeper/actions/workflows/test.yml)

**Rétention pour les médiathèques Plex gérées par Radarr et Sonarr — qui libère
vraiment l'espace disque.**

[English](README.md)

sweeper décide de ce qui peut partir d'après l'historique de lecture réel de
Plex, tous comptes confondus, puis le retire proprement. Il est dans l'esprit de
Maintainerr, avec les deux étapes que Maintainerr ne fait pas :

1. **Il retire aussi le torrent — et ses jumeaux cross-seed.** La bibliothèque
   est en général faite de hardlinks vers les téléchargements : supprimer un
   film dans Radarr efface un nom sur plusieurs, l'inode reste, et pas un octet
   n'est libéré. sweeper remonte chaque hardlink, trouve chaque torrent concerné
   et n'annonce que les octets dont *tous* les noms disparaissent.
2. **Il nettoie Seerr** (Overseerr, Jellyseerr) : il supprime les requests *et*
   la fiche media, pour que le titre redevienne demandable au lieu de rester
   « Disponible » pour toujours.

Tout est en simulation tant que vous ne dites pas le contraire, et sweeper ne
supprime jamais un fichier lui-même : chaque suppression est faite par
l'application qui le possède (Radarr, Sonarr, qBittorrent).

## Sommaire

- [Prérequis](#prérequis)
- [Démarrage rapide](#démarrage-rapide)
- [Connecter les services](#connecter-les-services)
- [Conteneurs et correspondance de chemins](#conteneurs-et-correspondance-de-chemins)
- [L'interface web](#linterface-web)
- [Règles](#règles)
- [Garde-fous](#garde-fous)
- [Ce qui arrive à un élément supprimé](#ce-qui-arrive-à-un-élément-supprimé)
- [Ligne de commande](#ligne-de-commande)
- [Automatisation](#automatisation)
- [Langue](#langue)
- [Sauvegarde](#sauvegarde)
- [Tests](#tests)
- [Limites](#limites)

## Prérequis

| | |
|---|---|
| **Python** | 3.11 ou plus, et `requests` |
| **Plex Media Server** | obligatoire — sweeper lit le fichier de sa base (une copie en lecture seule) : il doit tourner sur la même machine ou avoir le dossier de configuration de Plex monté |
| **Radarr / Sonarr** | au moins l'un des deux (API v3 : Radarr 3+, Sonarr 3 et 4) |
| **qBittorrent** | facultatif — seul client torrent pris en charge ; à désactiver sur une installation 100 % Usenet |
| **Seerr / Overseerr / Jellyseerr** | facultatif — sans lui, les requests ne sont simplement pas nettoyées |

## Démarrage rapide

### Installation directe

```bash
git clone https://github.com/Briaccc/sweeper.git
cd sweeper
pip install -r requirements.txt
python3 sweeper.py serve
```

Au premier lancement, `config.toml` est créé à partir de `config.example.toml`
et le serveur affiche une URL avec un jeton d'accès :

```
  http://127.0.0.1:29320/?token=…
```

Ouvrez-la. Si Radarr, Sonarr, qBittorrent ou Plex tournent sur la même machine
avec une installation standard, sweeper les trouve seul. Sinon l'écran
**Connexions** s'ouvre : saisissez chaque URL et clé d'API, **Tester**, puis
**Enregistrer**.

### Docker

```bash
cp docker-compose.example.yml docker-compose.yml   # puis l'adapter
docker compose up -d
docker compose logs sweeper                        # affiche l'URL avec le jeton
```

L'exemple monte le dossier de configuration de Plex en lecture seule, vos
médias, et un volume `/config` qui contient `config.toml` et l'état. Lisez
[Conteneurs et correspondance de chemins](#conteneurs-et-correspondance-de-chemins)
avant de démarrer.

## Connecter les services

Chaque réglage est cherché dans cet ordre — le premier trouvé l'emporte :

1. **Variables d'environnement** (`SWEEPER_*`, ci-dessous) — pratique avec
   Docker ; un champ fourni ainsi est grisé dans l'interface.
2. **`config.toml`** — ce qu'écrit l'écran Connexions.
3. **Découverte automatique** — `config.xml` de Radarr/Sonarr, `settings.json`
   de Seerr, identifiants qBittorrent dans une config cross-seed, base Plex aux
   emplacements habituels sous Linux, macOS, Windows et dans les conteneurs
   courants.

**Où trouver les clés d'API :** dans Radarr et Sonarr, *Settings → General →
Security → API Key* ; dans Seerr/Overseerr/Jellyseerr, *Settings → General →
API Key*. qBittorrent utilise l'utilisateur et le mot de passe de son interface
web.

**Comment elles sont gardées :** `config.toml` (et son `.bak`) est écrit en
lecture/écriture pour le seul propriétaire (`chmod 600`) et ignoré par git.
L'interface ne réaffiche jamais une clé ou un mot de passe enregistré — seulement
ses quatre derniers caractères — et laisser le champ vide conserve celui qui est
enregistré. Une connexion est testée avant d'être enregistrée.

| Variable | Réglage |
|---|---|
| `SWEEPER_RADARR_URL`, `SWEEPER_RADARR_API_KEY` | Radarr |
| `SWEEPER_SONARR_URL`, `SWEEPER_SONARR_API_KEY` | Sonarr |
| `SWEEPER_SEERR_URL`, `SWEEPER_SEERR_API_KEY` | Seerr / Overseerr / Jellyseerr |
| `SWEEPER_QBIT_URL`, `SWEEPER_QBIT_USERNAME`, `SWEEPER_QBIT_PASSWORD` | qBittorrent |
| `SWEEPER_RADARR_ENABLED`, `SWEEPER_SONARR_ENABLED`, `SWEEPER_SEERR_ENABLED`, `SWEEPER_QBIT_ENABLED` | `false` pour désactiver un service |
| `SWEEPER_PLEX_DB` | chemin de `com.plexapp.plugins.library.db` |
| `SWEEPER_CONFIG` | emplacement de `config.toml` (Docker : `/config/config.toml`) |
| `SWEEPER_STATE_DIR` | emplacement de l'état (Docker : `/config/state`) |
| `SWEEPER_STORAGE_PATH` | le disque dont le tableau de bord montre l'occupation (Docker : le montage des données) |
| `SWEEPER_LANG` | `en` ou `fr` |

**Désactiver un service.** Tout service sauf Plex peut être coupé, depuis
l'écran Connexions ou avec `enabled = false` : pas de qBittorrent sur une
installation Usenet, pas de Sonarr si vous ne gardez que des films. sweeper
travaille alors avec ce qui reste — sans client torrent, il n'y a simplement pas
de torrent à retirer.

**La base Plex** se trouve en général ici :

| Installation | Chemin |
|---|---|
| Paquet Linux | `/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| Docker (linuxserver, officiel) | `<volume de config>/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| macOS | `~/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db` |
| Windows | `%LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases\com.plexapp.plugins.library.db` |

sweeper la copie dans un fichier temporaire avant de la lire : il ne verrouille
jamais la base qu'utilise Plex.

## Conteneurs et correspondance de chemins

sweeper calcule les hardlinks à partir des fichiers eux-mêmes : il doit **voir
les mêmes fichiers** que Radarr, Sonarr et qBittorrent. Le plus simple est de
monter les données au même chemin dans tous les conteneurs (la disposition
habituelle en arbre unique `/data`) : rien d'autre à faire.

Si les chemins diffèrent, indiquez à sweeper comment les traduire, service par
service :

```toml
[services.radarr]
url = "http://radarr:7878"
path_map = [["/movies", "/data/media/movies"]]   # [chemin dans Radarr, chemin vu par sweeper]

[services.qbittorrent]
path_map = [["/downloads", "/data/torrents"]]

[services.plex]
database = "/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
path_map = [["/media", "/data/media"]]
```

Si sweeper ne trouve pas un fichier annoncé par une application, il le dit — un
bandeau dans l'interface, un avertissement dans `sweeper.py check` — et **bloque
ces éléments** : sans voir les fichiers, il ne verrait pas non plus les torrents
qui les retiennent, et ses garde-fous seed et copie unique seraient aveugles.

## L'interface web

Quatre onglets ; l'essentiel devant, le reste replié.

- **Tableau de bord** — trois chiffres (occupation du disque, libérable
  maintenant, libérable sous 30 jours), où partent les octets, et l'occupation
  dans le temps avec une prévision du moment où le disque sera plein.
- **Médiathèque** — une liste, une recherche, un sélecteur « quoi afficher »
  (jamais vu, dormant, candidats, bientôt disponibles, bloqués, en cours,
  protégés, hors *arr) et une
  bascule **Bibliothèque / Seed seul** pour les données que seul un torrent
  retient encore. Les séries se déplient par saison. Sélection, puis
  **Supprimer**, **Mettre en file** (supprimer dès que le seed sera payé),
  **Protéger**, ou **Adopter** ce que Plex sert sans *arr.
- **Règles** — les règles avec l'aperçu de ce qu'elles attraperaient, le passage
  automatique (désactivé par défaut), la file d'attente, les collections Plex
  protégées et les derniers travaux de fond.
- **Plus** — Connexions, Demandes (qui demande quoi, et si quelqu'un le
  regarde), Trackers (obligations de seed), croissance mensuelle, santé,
  historique des suppressions.

**Accès.** Par défaut le serveur n'écoute que sur `127.0.0.1`. Passez par un
reverse proxy (le cookie est marqué `Secure` quand le proxy envoie
`X-Forwarded-Proto: https`) ou par un tunnel SSH :

```bash
ssh -N -L 29320:127.0.0.1:29320 vous@votre-serveur
```

Sans le jeton, seule la page de connexion est servie. Le jeton est affiché au
démarrage et gardé dans le dossier d'état (`~/.local/state/sweeper/token`, ou
`/config/state/token` avec Docker) ; collez-le une fois, il est mémorisé dans un
cookie `HttpOnly`. Supprimez le fichier et relancez pour le révoquer. Les échecs
de connexion répétés sont freinés, et chaque réponse porte une
Content-Security-Policy stricte, `nosniff`, `X-Frame-Options: DENY` et
`no-store`.

## Règles

La première règle qui correspond l'emporte : l'ordre compte — glissez les
règles dans l'interface ou réordonnez-les dans le fichier. Chaque règle combine
un critère de visionnage et un âge minimum en bibliothèque ; sans ce garde-fou,
on supprimerait les nouveautés que personne n'a encore eu le temps de regarder.

```toml
[[rule]]
name = "séries jamais commencées"
media = "series"          # movie | series | season
never_watched = true      # ou : unwatched_days = 180
min_age_days = 45
# max_distinct_viewers = 1  # épargner ce qui a plu à plusieurs
```

Une saison supprimée est aussi démonitorée dans Sonarr, pour qu'elle ne revienne
pas.

## Garde-fous

- **Simulation par défaut.** `run` exige `--execute` ; le passage automatique
  est désactivé tant que vous ne l'activez pas.
- **Obligations de seed.** Un élément dont un torrent n'a pas atteint son ratio
  ou son ancienneté est reporté, jamais supprimé — réglable par tracker. Les
  hôtes d'un même tracker partagent une règle.
- **Copie unique.** Un torrent qui pointe directement sur un fichier de la
  bibliothèque (import par déplacement) n'est jamais supprimé avec ses données.
  Jamais contournable.
- **Fichiers introuvables** (voir la correspondance de chemins) : l'élément est
  bloqué. Jamais contournable.
- **Hors \*arr.** Ce que Plex sert sans Radarr/Sonarr doit d'abord être adopté
  — sweeper confie le dossier à l'application, en place, rien n'est déplacé ni
  téléchargé — puisque sweeper ne supprime jamais de fichier lui-même.
- **Visionnage en cours.** Ce qui figure dans « Continuer à regarder » d'un
  compte est épargné.
- **Tag de protection** (`keep`, créé par `sweeper.py init-tags`), titres
  protégés et **collections Plex protégées**.
- **Demande récente.** Rien de ce qui a été demandé sur Seerr depuis moins de
  30 jours.
- **Season packs.** Un torrent qui sert aussi des épisodes conservés reste.
- **Plafonds par passage.** 25 éléments et 400 Go par défaut.
- **Journal.** Chaque suppression est écrite dans `history.jsonl` avec ses
  chemins, torrents, trackers et identifiant TMDB/TVDB — de quoi réintroduire
  un titre en un clic.

## Ce qui arrive à un élément supprimé

| Étape | Service | Action |
|---|---|---|
| 1 | Radarr / Sonarr | suppression de la fiche et des fichiers (`deleteFiles=true`) |
| 2 | qBittorrent | suppression des torrents concernés **avec leurs données** |
| 3 | Seerr | `DELETE /request/{id}`, puis `DELETE /media/{id}` |

`add_import_exclusion = false` laisse le titre re-téléchargeable si quelqu'un le
redemande ; passez à `true` pour le bannir définitivement.

## Ligne de commande

```bash
python3 sweeper.py serve                # interface web
python3 sweeper.py check                # teste chaque service et la couverture des hardlinks
python3 sweeper.py plan                 # simulation : ce qui partirait (commande par défaut)
python3 sweeper.py plan --json          # idem, pour les scripts
python3 sweeper.py run --execute        # applique, avec confirmation
python3 sweeper.py run --execute --yes  # sans confirmation (cron)
python3 sweeper.py history              # journal des suppressions
python3 sweeper.py init                 # écrit un config.toml d'après ce qui est trouvé
python3 sweeper.py init-tags            # crée le tag « keep » dans Radarr/Sonarr
python3 sweeper.py export               # archive config + état
```

## Automatisation

Le **passage automatique** (onglet Règles) traite la file d'attente et, si vous
le cochez, les règles, à intervalle régulier — par la même file de travaux que
l'interface : il ne peut jamais tomber au milieu d'une suppression que vous
venez de lancer. Il est désactivé par défaut.

Vous préférez cron ? L'un ou l'autre, pas les deux :

```cron
30 4 * * 1 cd /chemin/vers/sweeper && python3 sweeper.py run --execute --yes >> sweeper.log 2>&1
```

Pour que l'interface survive à un redémarrage sans Docker, un service systemd
utilisateur ou une ligne cron `@reboot` qui lance `python3 sweeper.py serve`
suffit.

## Langue

L'interface, la ligne de commande et tous les messages existent en **anglais**
et en **français**. Choisissez la langue avec le sélecteur de l'en-tête — elle
est enregistrée dans `config.toml` (`[ui] language`) — ou imposez-la avec
`SWEEPER_LANG`. Sans réglage, sweeper suit la langue du système.

## Sauvegarde

```bash
python3 sweeper.py export               # sweeper-export-<date>.tar.gz
python3 sweeper.py export --with-token  # inclut le jeton d'accès
```

L'archive contient `config.toml` et l'état : le journal des suppressions (seule
trace de ce qui est parti), la file d'attente, les relevés d'occupation et les
derniers travaux.

## Tests

```bash
npm install     # une fois : jsdom, utilisé seulement par les tests
npm test
```

- `test_linkmap.py` — l'arithmétique des hardlinks, la seule logique qui
  pourrait détruire des données, sur une vraie arborescence temporaire avec de
  vrais inodes.
- `test_tidy.py` — le rangement de fichiers nus dans un dossier, avec de vrais
  renommages.
- `test_i18n.py` — chaque message existe dans les deux langues, marqueurs
  compris, et aucun texte français n'a échappé à la traduction.
- `test_e2e.py` — tout le circuit, de bout en bout, contre de faux Radarr,
  Sonarr, Seerr et qBittorrent qui agissent vraiment sur un disque temporaire,
  une fausse base Plex, de vrais hardlinks et du cross-seed. Chaque commande et
  chaque route de l'API sont exécutées, chaque garde-fou a son cas ; on vérifie
  les fichiers restants, les octets réellement libérés et chaque appel reçu par
  chaque service. Il tourne trois fois : mêmes chemins partout, chemins de
  conteneur avec `path_map`, et chemins de conteneur *sans* correspondance
  (rien ne doit être supprimé). L'interface est rendue dans jsdom dans les deux
  langues au passage.

`node test_webui.mjs <état.json> [--lang en]` rend l'interface à partir de
n'importe quelle réponse `/api/state` enregistrée.

## Limites

- **Plex uniquement** — ni Jellyfin ni Emby : l'historique vient de la base de
  Plex.
- **qBittorrent uniquement** comme client torrent.
- **API v3** de Radarr/Sonarr (Radarr 3+, Sonarr 3 et 4).

## Licence

MIT
