"""English catalogue for sweeper.i18n: French source message -> English.

Keep placeholders ({name}, {n:.0f}…) identical; test_i18n.py checks them.
Blockers keep their " — " separator: the UI shows what precedes it as a
short label.
"""

EN: dict[str, str] = {
    # --- command line: description and help
    "sweeper — rétention média pour Plex + Radarr/Sonarr + Seerr + qBittorrent.\n\n"
    "Contrairement à Maintainerr, sweeper ne se contente pas de supprimer le média :\n"
    "il retire aussi le torrent (et ses jumeaux cross-seed, sans quoi le hardlink\n"
    "garde les octets) puis la request et la fiche media côté Seerr, ce qui rend le\n"
    "titre à nouveau demandable.\n\n"
    "Tout est en simulation par défaut. Il faut --execute pour agir.":
    "sweeper — media retention for Plex + Radarr/Sonarr + Seerr + qBittorrent.\n\n"
    "Unlike Maintainerr, sweeper doesn't just delete the media: it also removes the\n"
    "torrent (and its cross-seed twins, otherwise the hardlink keeps the bytes),\n"
    "then the request and the media entry in Seerr, which makes the title\n"
    "requestable again.\n\n"
    "Everything is a dry run by default. Pass --execute to act.",
    "chemin du config.toml": "path to config.toml",
    "sortie sans couleurs": "plain output, no colours",
    "teste l'accès aux services": "test access to the services",
    "simulation : ce qui partirait": "dry run: what would go",
    "sortie JSON": "JSON output",
    "applique le nettoyage": "apply the clean-up",
    "passe à l'acte": "actually do it",
    "sans confirmation (cron)": "no confirmation (cron)",
    "journal des suppressions": "deletion log",
    "archive config + état (tar.gz)": "archive config + state (tar.gz)",
    "chemin de l'archive": "archive path",
    "inclure le jeton d'accès": "include the access token",
    "génère un config.toml": "generate a config.toml",
    "écrase un fichier existant": "overwrite an existing file",
    "lance l'interface web": "start the web interface",
    "crée les tags de protection": "create the protection tags",

    # --- command line: check
    "ÉCHEC": "FAIL",
    "désactivé": "disabled",
    "Renseigne les services manquants dans {path}, par variables d'environnement "
    "(SWEEPER_*), ou depuis l'écran Connexions de l'interface web (python3 sweeper.py serve).":
    "Fill in the missing services in {path}, with environment variables (SWEEPER_*), "
    "or from the Connections screen of the web interface (python3 sweeper.py serve).",
    "{movies} films, {shows} séries, {views} visionnages": "{movies} movies, {shows} series, {views} plays",
    "base Plex introuvable — indique [services.plex] database dans la configuration":
    "Plex database not found — set [services.plex] database in the configuration",
    "{n} fichier(s) annoncé(s) par Radarr introuvable(s) ici (ex. {example}) — "
    "correspondance de chemins (path_map) à configurer ?":
    "{n} file(s) reported by Radarr cannot be found here (e.g. {example}) — "
    "does a path mapping (path_map) need configuring?",
    "{n} torrent(s) hors des racines surveillées ({gb:.0f} Go) — le calcul d'octets "
    "libérés peut sous-estimer ce qui reste ailleurs.":
    "{n} torrent(s) outside the watched roots ({gb:.0f} GB) — the freed-bytes "
    "figure may underestimate what survives elsewhere.",
    "racines": "roots",
    "{n} racine(s), tous les torrents couverts": "{n} root(s), every torrent covered",
    "{n} règle(s) chargée(s) :": "{n} rule(s) loaded:",
    " (désactivée)": " (disabled)",
    "jamais vu": "never watched",
    "pas vu depuis {days} j": "not watched for {days} d",
    "{media}, {criterion}, en place depuis ≥ {days} j": "{media}, {criterion}, in the library for ≥ {days} d",

    # --- command line: plan / run / history / export / tags
    "Simulation uniquement — relancer avec « run --execute » pour appliquer.":
    "Dry run only — run again with “run --execute” to apply.",
    "Simulation uniquement — ajouter --execute pour appliquer.": "Dry run only — add --execute to apply.",
    "Supprimer {n} élément(s) et libérer {size} ? [oui/non] ": "Delete {n} item(s) and free {size}? [yes/no] ",
    "Annulé.": "Cancelled.",
    "{done}/{total} traité(s), {size} libérés. Journal : {log}": "{done}/{total} processed, {size} freed. Log: {log}",
    "Aucun historique.": "No history.",
    "{n} suppression(s) au total, {size} libérés.": "{n} deletion(s) in total, {size} freed.",
    "Écrit {path} ({kb:.0f} ko) :": "Wrote {path} ({kb:.0f} kB):",
    "{kb:7.1f} ko": "{kb:7.1f} kB",
    "(jeton d'accès exclu — --with-token pour l'inclure)": "(access token left out — --with-token to include it)",
    "tag « {tag} » déjà présent": "tag “{tag}” already exists",
    "tag « {tag} » créé": "tag “{tag}” created",
    "Pose ce tag sur un film ou une série pour l'exclure définitivement.":
    "Put this tag on a movie or a series to exclude it for good.",

    # --- command line: init
    "{path} existe déjà — --force pour l'écraser.": "{path} already exists — pass --force to overwrite it.",
    "Recherche des services…": "Looking for services…",
    "introuvable — renseigne url et api_key à la main": "not found — fill in url and api_key by hand",
    "réglages dans {path}": "settings at {path}",
    "→ indique son url ci-dessous, le port n'est pas dans ce fichier":
    "→ set its url below, the port is not in that file",
    "introuvable (facultatif)": "not found (optional)",
    "identifiants via cross-seed ({path})": "credentials via cross-seed ({path})",
    "introuvable — renseigne url/username/password": "not found — fill in url/username/password",
    "introuvable — renseigne [services.plex] database à la main":
    "not found — set [services.plex] database by hand",
    "racines de hardlink : impossible de les déduire ({error})": "hardlink roots: could not derive them ({error})",
    "racines de hardlink déduites :": "hardlink roots derived:",
    "{path} écrit. Vérifie-le, puis lance : python3 sweeper.py check":
    "Wrote {path}. Check it, then run: python3 sweeper.py check",

    # --- report (plan output)
    "jamais": "never",
    "film": "movie",
    "série": "series",
    "saison": "season",
    ", {n} request Seerr": ", {n} Seerr request(s)",
    ", fiche Seerr": ", Seerr entry",
    "vu: {seen} · {viewers} spectateur(s) · en place depuis {age:.0f} j · {torrents} torrent(s){seerr}":
    "seen: {seen} · {viewers} viewer(s) · in the library for {age:.0f} d · {torrents} torrent(s){seerr}",
    "films": "movies",
    "séries": "series",
    "fiches Seerr": "Seerr entries",
    "Analysé :": "Scanned:",
    "À SUPPRIMER — {n} élément(s), {size} réellement libérés": "TO DELETE — {n} item(s), {size} actually freed",
    "Aucun élément ne remplit les règles ce passage.": "Nothing matches the rules this run.",
    "RETENUS — {n} élément(s) ({size} en bibliothèque)": "HELD BACK — {n} item(s) ({size} in the library)",

    # --- plan blockers (the text before " — " is the short label)
    "fichiers introuvables — {n} fichier(s) que sweeper ne voit pas (ex. {example}) : "
    "correspondance de chemins (path_map) à configurer ?":
    "files not found — {n} file(s) sweeper cannot see (e.g. {example}): "
    "does a path mapping (path_map) need configuring?",
    "hors *arr — à adopter dans Radarr/Sonarr avant toute suppression":
    "outside *arr — adopt it into Radarr/Sonarr before any deletion",
    "collection protégée — « {name} »": "protected collection — “{name}”",
    "visionnage en cours — commencé par {n} comptes sans être fini":
    "in progress — started by {n} accounts, never finished",
    "visionnage en cours — commencé par {n} compte sans être fini":
    "in progress — started by {n} account, never finished",
    "tag protégé — « {tag} »": "protected tag — “{tag}”",
    "titre protégé — « {title} »": "protected title — “{title}”",
    "demande Seerr récente — il y a {days:.0f} j (fenêtre {window} j)":
    "recent Seerr request — {days:.0f} d ago (window {window} d)",
    "copie unique en bibliothèque — « {name} » pointe sur {paths}":
    "only copy in the library — “{name}” points at {paths}",
    "torrent partagé — « {name} » sert aussi du contenu conservé":
    "shared torrent — “{name}” also seeds content that stays",
    "seed insuffisant ({tracker}) — ratio {ratio:.2f} / {days:.0f} j, requis {min_ratio:.2f} ou {min_days} j":
    "seeding not done ({tracker}) — ratio {ratio:.2f} / {days:.0f} d, required {min_ratio:.2f} or {min_days} d",
    "plafond de passage — nombre d'items": "per-run cap — number of items",
    "plafond de passage — volume": "per-run cap — volume",
    "le torrent détient l'unique copie d'un fichier conservé": "the torrent holds the only copy of a file that stays",

    # --- deletion steps (history)
    "(sélection manuelle)": "(manual selection)",
    "(données hors bibliothèque)": "(data outside the library)",
    "Radarr : film et fichier supprimés": "Radarr: movie and file deleted",
    "Sonarr : série et fichiers supprimés": "Sonarr: series and files deleted",
    "Sonarr : {n} fichiers supprimés, saison {season} démonitorée": "Sonarr: {n} files deleted, season {season} unmonitored",
    "qBittorrent : aucun torrent concerné": "qBittorrent: no torrent involved",
    "qBittorrent : {n} torrent(s) retiré(s) avec données ({names})": "qBittorrent: {n} torrent(s) removed with data ({names})",
    "qBittorrent : {n} torrent(s) retiré(s) avec données": "qBittorrent: {n} torrent(s) removed with data",
    "Seerr : {n} request(s) supprimée(s)": "Seerr: {n} request(s) deleted",
    "Seerr : fiche media {id} supprimée (le titre redevient demandable)":
    "Seerr: media entry {id} deleted (the title can be requested again)",
    "obligations de seed non remplies (déblocage dans {days} j)": "seeding obligations not met (unblocks in {days} d)",
    "Radarr : ajouté en place, scan du dossier lancé": "Radarr: added in place, folder scan started",
    "Sonarr : ajoutée en place, scan du dossier lancé": "Sonarr: added in place, folder scan started",
    "Disque : rangé dans {folder}/": "Disk: moved into {folder}/",

    # --- services and configuration
    "TMDB {id} introuvable dans Radarr": "TMDB {id} not found in Radarr",
    "TVDB {id} introuvable dans Sonarr": "TVDB {id} not found in Sonarr",
    "aucun dossier racine configuré — impossible de choisir où placer le titre":
    "no root folder configured — cannot decide where to put the title",
    "fichier de configuration introuvable : {path}": "configuration file not found: {path}",
    "aucune règle [[rule]] définie dans {path}": "no [[rule]] defined in {path}",
    "aucun bloc [[rule]] à remplacer dans {path}": "no [[rule]] block to replace in {path}",
    "règle « {name} » : media={media!r} invalide": "rule “{name}”: invalid media={media!r}",
    "règle « {name} » : il faut never_watched ou unwatched_days": "rule “{name}”: needs never_watched or unwatched_days",
    "langue inconnue : {language}": "unknown language: {language}",
    "base Plex introuvable : {path}": "Plex database not found: {path}",
    "qBittorrent : identifiants refusés": "qBittorrent: credentials rejected",
    "{service} est désactivé dans la configuration": "{service} is disabled in the configuration",
    "base Plex introuvable — indique son chemin": "Plex database not found — enter its path",
    "non configuré — renseigne l'URL, l'utilisateur et le mot de passe, "
    "ou désactive-le si tu n'utilises pas de torrents":
    "not configured — enter the URL, username and password, "
    "or disable it if you don't use torrents",
    "non configuré — renseigne l'URL et la clé d'API": "not configured — enter the URL and the API key",
    "ce fichier n'est pas nu à la racine": "this file is not sitting bare at the root",
    "le dossier « {name} » existe déjà": "the folder “{name}” already exists",

    # --- web server
    "la commande de quota a échoué ({error}) ; mesure du système de fichiers à la place":
    "the quota command failed ({error}); measuring the filesystem instead",
    "connexion impossible — {details}": "cannot connect — {details}",
    " ; ": "; ",
    "{name} : {detail}": "{name}: {detail}",
    "Écoute sur {host}:{port}, protégé par le jeton.": "Listening on {host}:{port}, protected by the token.",
    "base Plex introuvable — indique son chemin dans l'écran Connexions":
    "Plex database not found — enter its path in the Connections screen",
    "file d'attente : « {title} » retiré (clé disparue)": "queue: “{title}” removed (key no longer exists)",
    "jeton invalide": "invalid token",
    "route inconnue": "unknown route",
    "introuvable": "not found",
    "Jeton refusé.": "Token rejected.",
    "{n} élément(s)": "{n} item(s)",
    "{n} lot(s)": "{n} batch(es)",
    "{n} film(s)": "{n} movie(s)",
    "entrée ambiguë : plusieurs suppressions à la même seconde": "ambiguous entry: several deletions in the same second",
    "entrée de journal introuvable": "log entry not found",
    "cette entrée est antérieure au journal détaillé, l'identifiant n'a pas été conservé":
    "this entry predates the detailed log; its identifier was not kept",
    "fichier nu à la racine — Radarr exige un dossier par film ; déplace-le dans un dossier à son nom d'abord":
    "file sitting bare at the root — Radarr requires one folder per movie; move it into a folder named after it first",
    "fichiers répartis dans {n} dossiers : impossible de désigner un dossier unique à adopter":
    "files spread over {n} folders: no single folder to adopt",
    "identifiant {source} connu de Plex": "{source} ID known to Plex",
    "titre, année concordante": "title, matching year",
    "{app} ne connaît pas ce titre : Plex a bien {known}, mais aucune recherche ne le trouve — "
    "il n'est probablement pas référencé sur {source}, dont {app} dépend. À supprimer depuis Plex si besoin.":
    "{app} does not know this title: Plex has {known}, but no lookup finds it — it is probably "
    "not listed on {source}, which {app} relies on. Delete it from Plex if needed.",
    "aucun identifiant ; la recherche par titre propose « {title} ({year}) » mais l'année ne "
    "concorde pas avec Plex ({plex_year}) — à adopter à la main":
    "no identifier; the title search suggests “{title} ({year})” but the year does not "
    "match Plex ({plex_year}) — adopt it by hand",
    "aucune correspondance trouvée": "no match found",
    "l'URL doit commencer par http:// ou https://": "the URL must start with http:// or https://",
    "service inconnu : {name}": "unknown service: {name}",
    "défini par variable d'environnement, non modifiable ici : {fields}":
    "set by an environment variable, cannot be changed here: {fields}",
    "test échoué, rien n'est enregistré : {detail}": "test failed, nothing was saved: {detail}",
    "passage automatique": "automatic run",
    "planificateur : {error}": "scheduler: {error}",
    "Calcul de l'inventaire initial…": "Computing the initial inventory…",
    "Ouvre l'interface : l'écran Connexions permet de configurer les services.":
    "Open the interface: the Connections screen lets you configure the services.",
    "{items} éléments analysés, {batches} lots hors bibliothèque": "{items} items scanned, {batches} batches outside the library",
    "{n} élément(s) en file d'attente": "{n} item(s) in the queue",
    "passage automatique : {when}": "automatic run: {when}",
    "toutes les {n} min": "every {n} min",
    "Écoute sur {host}:{port} uniquement.": "Listening on {host}:{port} only.",
    "Depuis une autre machine, redirige le port par SSH :": "From another machine, forward the port over SSH:",
    "Jeton mémorisé en cookie après la 1re ouverture, stocké dans": "The token is kept in a cookie after the first visit; it is stored in",
    "{path}. Ctrl-C pour arrêter.": "{path}. Ctrl-C to stop.",
}
