'use strict';

/* ------------------------------------------------------------------- tools */

const nf = new Intl.NumberFormat(LOCALE);   // LANG, LOCALE, t(): see i18n.js
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const dec = x => x.toFixed(1).replace('.', LANG === 'fr' ? ',' : '.');
function go(bytes) {                       // bytes -> "1 234 Go" / "2,1 To" (or GB / TB)
  const gb = (bytes || 0) / 1e9;
  if (gb >= 1000) return dec(gb / 1000) + ' ' + t('To');
  if (gb >= 100) return nf.format(Math.round(gb)) + ' ' + t('Go');
  return dec(gb) + ' ' + t('Go');
}
function small(bytes) {                    // kB / MB for small sizes (log, state)
  const b = bytes || 0;
  if (b >= 1e9) return go(b);
  if (b >= 1e6) return dec(b / 1e6) + ' ' + t('Mo');
  return Math.round(b / 1e3) + ' ' + t('ko');
}
function days(ts) {
  if (!ts) return null;
  return Math.round((Date.now() / 1000 - ts) / 86400);
}
function dateFr(ts) {
  if (!ts) return t('jamais');
  return new Date(ts * 1000).toLocaleDateString(LOCALE,
    { day: '2-digit', month: 'short', year: 'numeric' });
}
function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid) node.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return node;
}
function toast(message, isError = false) {
  const node = el('div', { class: 'toast' + (isError ? ' err' : ''), text: message });
  document.body.append(node);
  setTimeout(() => node.remove(), isError ? 9000 : 4500);
}

async function api(path, options) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json' },
  });
  const data = await response.json().catch(() => ({}));
  // Only the HTTP status says whether the request failed. A state served with
  // 200 may carry `erreur`: that is the cause of the last failed refresh, to be
  // shown — not a reason to throw the response away.
  if (!response.ok) throw new Error(data.erreur || response.statusText);
  return data;
}

/* ------------------------------------------------------------------ state */

let S = null;                 // last /api/state response
let selection = new Set();
let sortKey = 'freed_bytes';
let sortDir = -1;
let viewFilter = 'all';       // the “What to show” selector
let libraryView = 'media';    // Library | Seed-only
let whoFilter = '';
let expanded = new Set();        // series expanded into seasons
let ruleDraft = null;
let preview = [];
let previewTimer = null;
let oSelection = new Set();
let oFilters = new Set();
let oSortKey = 'bytes';
let oSortDir = -1;
let seedDraft = null;
let dragFrom = null;
let seedSimTimer = null;

const KIND = { movie: t('film'), series: t('série'), season: t('saison') };
const dayCount = n => t('{n} j', { n });

/* -------------------------------------------------------------- dashboard */

function renderDash() {
  renderCoverageWarning();
  const q = S.quota, tot = S.totaux, c = S.composition;
  const pct = q.plan_gb ? Math.round(q.used_gb / q.plan_gb * 100) : 0;

  $('#kpis').replaceChildren(
    el('div', { class: 'card kpi' },
      el('div', { class: 'label', text: t('Occupation') }),
      el('div', { class: 'value hero num', text: go(q.used_gb * 1e9) }),
      el('div', { class: 'note' }, t('sur {total} · ', { total: go(q.plan_gb * 1e9) }), el('b', { text: pct + ' %' }))),
    el('div', { class: 'card kpi' },
      el('div', { class: 'label', text: t('Libérable maintenant') }),
      el('div', { class: 'value num', text: go(tot.libérable_maintenant + S.orphelins_totaux.prêts) }),
      el('div', { class: 'note' }, t('dont {size} de seed seul', { size: go(S.orphelins_totaux.prêts) }))),
    el('div', { class: 'card kpi' },
      el('div', { class: 'label', text: t('Sous 30 jours') }),
      el('div', { class: 'value num', text: go(S.libérable_sous_30j) }),
      el('div', { class: 'note' }, t('quand le seed sera payé'))),
  );

  // --- stacked bar: the disk's capacity is 100 % of the width
  const plan = q.plan_gb * 1e9 || 1;
  const parts = [
    ['s1', t('Bibliothèque vue'), c.vue],
    ['s2', t('Bibliothèque jamais vue'), c.jamais_vue],
    ['s3', t('Seed seul, hors bibliothèque'), c.seed_seul],
    ['s4', t('Autre'), (c.hors_suivi || 0) + (c.reste || 0)],
  ];
  const free = Math.max(plan - parts.reduce((a, p) => a + p[2], 0), 0);

  $('#stack').replaceChildren(...parts.map(([cls, label, bytes]) => {
    const width = bytes / plan * 100;
    const node = el('div', { class: cls, style: `width:${width}%`, title: `${label} — ${go(bytes)}` });
    // direct label only if it really fits in the segment
    if (width > 14) {
      node.append(el('span', {
        style: 'position:absolute;inset:0;display:flex;align-items:center;'
          + 'justify-content:center;font-size:12px;font-weight:600;color:#fff',
        text: go(bytes),
      }));
    }
    return node;
  }), el('div', { class: 'free', style: `width:${free / plan * 100}%`, title: `${t('Libre')} — ${go(free)}` }));

  // --- marks for the tiers at or below the current capacity (if any are configured)
  $('#ticks').replaceChildren(...S.paliers
    .filter(tier => tier.gb <= q.plan_gb + 50)
    .map(tier => el('div', { class: 'tick', style: `left:${tier.gb / q.plan_gb * 100}%` },
      el('i'), el('b', { text: tier.nom }),
      el('s', { text: `${tier.prix} ${tier.devise}` }))));

  $('#stackLegend').replaceChildren(
    ...parts.map(([cls, label, bytes]) => el('div', {},
      el('i', { style: `background:var(--series-${cls[1]})` }),
      label, ' ', el('b', { text: go(bytes) }))),
    el('div', {}, el('i', { style: 'background:var(--surface-2);box-shadow:inset 0 0 0 1px var(--border)' }),
      t('Libre') + ' ', el('b', { text: go(free) })));

  renderUsage();
  renderHealth();

  // --- monthly growth
  const months = S.croissance;
  const peak = Math.max(...months.map(m => m.octets), 1);
  $('#growth').replaceChildren(...months.map((m, i) => {
    const show = m.octets === peak || i === months.length - 1;
    return el('div', { class: 'col', title: `${m.mois} — ${go(m.octets)}` },
      el('div', { class: 'cap', text: show ? go(m.octets) : '' }),
      el('div', { class: 'mark', style: `height:${Math.max(m.octets / peak * 110, 2)}px` }));
  }));
  $('#growthX').replaceChildren(...months.map(m =>
    el('div', { class: 'col' }, el('div', { class: 'x', text: m.mois.slice(2).replace('-', '/') }))));

  // --- pricing tiers: optional, shown only when [[tier]] is configured
  $('#tiersCard').hidden = !S.paliers.length;
  $('#tiers').replaceChildren(...S.paliers.map(tier => el('div',
    { class: 'tier' + (tier.actuel ? ' current' : '') },
    el('div', { class: 'n', text: tier.nom + (tier.actuel ? ' — ' + t('actuel') : '') }),
    el('div', { class: 'p num', text: `${tier.prix} ${tier.devise}` }),
    el('div', { class: 'd', text: t('{gb} Go / mois', { gb: nf.format(tier.gb) }) }),
    el('div', { class: 'fit ' + (tient(tier) ? 'yes' : 'no'), text: tient(tier) ? t('tu y rentres') : t('trop juste') }))));

  const current = S.paliers.find(tier => tier.actuel);
  const cheaper = tier => !current || tier.prix < current.prix;
  const best = S.paliers.filter(x => tient(x) && cheaper(x)).sort((a, b) => a.prix - b.prix)[0];
  const saving = tier => t('{month} {cur}/mois, soit {year} {cur} par an', {
    month: current.prix - tier.prix, year: (current.prix - tier.prix) * 12, cur: current.devise });

  if (best) {
    $('#tierHint').textContent = t("Avec {used} occupés, le palier {tier} suffit déjà : {saving} d'économie.",
      { used: go(q.used_gb * 1e9), tier: best.nom, saving: saving(best) });
  } else if (current) {
    // nothing fits right now: say what would need freeing, and whether it is within reach
    const now = tot.libérable_maintenant + S.orphelins_totaux.prêts;
    const soon = now + S.libérable_sous_30j;
    const used = q.used_gb * 1e9;
    const target = S.paliers.filter(cheaper).sort((a, b) => b.gb - a.gb)[0];
    if (!target) {
      $('#tierHint').textContent = t('Tu es déjà au palier le moins cher.');
    } else {
      const need = used - target.gb * 1e9;
      const vars = { need: go(need), tier: target.nom, saving: saving(target), now: go(now), soon: go(soon) };
      $('#tierHint').textContent = now >= need
        ? t("Il manque {need} pour descendre au palier {tier} ({saving}). Tu as {now} récupérables tout de suite et {soon} au total : c'est atteignable maintenant.", vars)
        : soon >= need
          ? t("Il manque {need} pour descendre au palier {tier} ({saving}). Tu as {now} récupérables tout de suite et {soon} sous 30 jours : c'est atteignable sous 30 jours, quand le seed sera payé.", vars)
          : t('Il manque {need} pour descendre au palier {tier} ({saving}). Seuls {soon} sont récupérables à court terme — il faudrait tailler dans la bibliothèque active.', vars);
    }
  } else {
    $('#tierHint').textContent = '';
  }
  renderFullForecast();

  $('#stamp').textContent = t('calculé {time}', { time: new Date(S.calculé_à * 1000)
    .toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' }) });
}

const tient = tier => S.quota.used_gb < tier.gb;

/** How long until the disk is full, at the observed rate.
 *  Two readings: the *net* rate (daily readings, when there are enough) and
 *  the gross *intake* rate (12 months of Plex imports), more pessimistic
 *  since it ignores deletions. */
function renderFullForecast() {
  const q = S.quota, trend = S.occupation.tendance, gross = S.croissance_moyenne;
  const free = (q.plan_gb - q.used_gb) * 1e9;
  const box = $('#fullHint');
  if (free <= 0) { box.textContent = t('Le disque est plein.'); return; }
  const parts = [];
  if (trend.fiable && trend.par_jour > 0) {
    const left = free / trend.par_jour;
    parts.push(t('Au rythme net des {days} derniers jours (+{rate}/j), le disque sera plein dans {eta}.', {
      days: trend.jours, rate: go(trend.par_jour),
      eta: left < 60 ? t('{n} jours', { n: Math.round(left) }) : t('{n} mois', { n: dec(left / 30) }) }));
  } else if (trend.fiable && trend.par_jour <= 0) {
    const rate = { rate: go(Math.abs(trend.par_jour)) };
    parts.push(trend.par_jour < 0
      ? t("L'occupation ne monte pas ({rate}/j en baisse) : pas de saturation en vue.", rate)
      : t("L'occupation ne monte pas ({rate}/j en stabilité) : pas de saturation en vue.", rate));
  }
  if (gross > 0) {
    parts.push(t("Sans aucune suppression, au rythme d'ajout moyen ({rate}/mois), il serait plein dans {n} mois.",
      { rate: go(gross), n: dec(free / gross) }));
  }
  box.textContent = parts.join(' ');
}

/** Warn when torrents lie outside the watched roots: the freed-bytes figure
 * may then underestimate what survives elsewhere. */
function renderCoverageWarning() {
  const gap = S.couverture_hardlinks;
  const box = $('#coverageWarning');
  const blind = S.fichiers_introuvables || { éléments: 0 };
  if (blind.éléments) {
    box.hidden = false;
    box.replaceChildren(
      el('strong', {}, t('⚠ {n} élément(s) dont sweeper ne voit pas les fichiers. ', { n: blind.éléments })),
      t("Radarr/Sonarr/Plex rapportent des chemins qui n'existent pas ici (ex. "), el('code', { text: blind.exemple }),
      t('). Ces éléments sont bloqués : sans voir les fichiers, sweeper ne verrait pas non plus les torrents qui les retiennent. Si sweeper tourne dans un conteneur, ajoute une correspondance '),
      el('code', { text: 'path_map' }), t(' pour chaque service dans config.toml (voir le README).'));
    return;
  }
  if (!gap || !gap.torrents) { box.hidden = true; return; }
  box.hidden = false;
  box.replaceChildren(
    el('strong', {}, t('⚠ {n} torrent(s) hors des dossiers surveillés ({size}). ', { n: gap.torrents, size: go(gap.octets) })),
    t("Le volume réellement libéré par une suppression peut être sous-estimé si l'un de ces fichiers est aussi hardlinké dans la bibliothèque. Ajoute le(s) dossier(s) suivant(s) à paths.link_roots :"),
    el('ul', { style: 'margin:8px 0 0 18px' },
      ...gap.exemples.map(x => el('li', {},
        el('code', { text: x.chemin }), ' — ', x.nom))));
}

/** Readable duration: 95 s -> “1 min”, 7300 s -> “2 h”. */
function ago(seconds) {
  if (seconds == null) return '—';
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h`;
  return dayCount(Math.round(seconds / 86400));
}

/** Health card: services, freshness of the Plex database, cost of a refresh. */
function renderHealth() {
  const h = S.santé || {};
  const box = $('#health');
  if (!h.services) { box.replaceChildren(el('div', { class: 'meta', text: t('Pas encore mesuré.') })); return; }
  const gap = S.couverture_hardlinks || { torrents: 0 };
  box.replaceChildren(
    el('div', { class: 'legend', style: 'margin-top:0' },
      ...h.services.map(x => el('div', { title: x.détail },
        el('i', { style: `background:var(--${x.ok ? 'good' : 'bad'})` }),
        `${x.service} `, el('b', { text: x.ok ? x.détail : t('injoignable') })))),
    el('div', { class: 'legend', style: 'margin-top:12px' },
      el('div', {}, el('i', { style: `background:var(--${gap.torrents ? 'warn' : 'good'})` }),
        t('Racines de hardlink '), el('b', { text: gap.torrents ? t('{n} torrent(s) hors couverture', { n: gap.torrents }) : t('{n} racines, tout couvert', { n: (h.racines || []).length }) })),
      el('div', {}, el('i', { style: 'background:var(--series-1)' }),
        t('Base Plex écrite il y a '), el('b', { text: ago(h.plex_db_écrite_il_y_a) })),
      el('div', {}, el('i', { style: 'background:var(--series-1)' }),
        t('Rafraîchissement '), el('b', { text: `${h.durée_rafraîchissement} s` })),
      el('div', {}, el('i', { style: 'background:var(--series-1)' }),
        t('Serveur en ligne depuis '), el('b', { text: ago(h.démarré_depuis) })),
      el('div', {}, el('i', { style: 'background:var(--series-1)' }),
        t('Journal et état '), el('b', { text: small(h.état_octets) }))));
}

/** Usage chart: one line, plan marks, nothing else.
 *  Drawn in real pixels (1:1 scale): a stretched viewBox distorts text and
 *  markers. The vertical scale starts below the lowest plan, not at zero —
 *  everything happens between the plans. */
let usageResizeBound = false;
function renderUsage() {
  const samples = S.occupation.relevés;
  const trend = S.occupation.tendance;
  const box = $('#usageChart');

  if (samples.length < 2) {
    box.replaceChildren(el('div', { class: 'empty', style: 'padding:26px 0',
      text: t("La courbe se remplit à partir d'aujourd'hui — un relevé par jour.") }));
    $('#usageNote').textContent = samples.length
      ? t("Premier relevé pris aujourd'hui : {used} occupés. Reviens demain pour voir la pente.", { used: go(samples[0].occupé) })
      : '';
    return;
  }

  const W = Math.max(box.clientWidth || 600, 320), H = 170;
  const padL = 10, padR = 74, padT = 14, padB = 24;
  const plan = Math.max(...samples.map(s => s.plan), 1);
  const tiers = S.paliers.filter(tier => tier.gb * 1e9 <= plan * 1.02);
  const lowest = Math.min(...samples.map(s => s.occupé), ...tiers.map(tier => tier.gb * 1e9));
  const floor = Math.max(0, lowest - plan * 0.06);
  const top = plan * 1.02;
  const x = i => padL + (samples.length === 1 ? 0 : i / (samples.length - 1)) * (W - padL - padR);
  const y = v => padT + (1 - (v - floor) / (top - floor)) * (H - padT - padB);
  const esc = v => String(v).replace(/[&<>"']/g, ch =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const f = n => n.toFixed(1);

  // the current capacity is often a tier itself: one mark, one label
  const isPlan = tier => Math.abs(tier.gb * 1e9 - plan) / plan < 0.01;
  const rule = (v, label) => {
    const ty = y(v);
    return `<line x1="${padL}" y1="${f(ty)}" x2="${W - padR}" y2="${f(ty)}" stroke="var(--border-strong)" stroke-width="1"/>`
      + `<text class="rule-label" x="${padL + 4}" y="${f(ty - 5)}" font-size="11" fill="var(--text-3)">${label}</text>`;
  };
  const grid = tiers.map(tier => rule(tier.gb * 1e9,
    `${esc(tier.nom)} · ${nf.format(tier.gb)} ${t('Go')}${isPlan(tier) ? ' ' + t('(palier actuel)') : ''}`)).join('');
  const planLine = tiers.some(isPlan) ? ''
    : rule(plan, `${t('capacité')} · ${nf.format(Math.round(plan / 1e9))} ${t('Go')}`);

  const pts = samples.map((s, i) => `${f(x(i))},${f(y(s.occupé))}`).join(' ');
  const last = samples[samples.length - 1], lx = x(samples.length - 1), ly = y(last.occupé);
  const first = samples[0];

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
      style="display:block;width:100%;height:${H}px;overflow:visible" role="img"
      aria-label="${esc(t('Occupation disque par jour'))}">
    ${grid}${planLine}
    <polyline points="${pts}" fill="none" stroke="var(--series-1)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${f(lx)}" cy="${f(ly)}" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2"/>
    <text x="${f(lx + 9)}" y="${f(ly + 4)}" font-size="12" font-weight="600" fill="var(--text-1)">${go(last.occupé)}</text>
    <text x="${padL}" y="${H - 6}" font-size="11" fill="var(--text-3)">${esc(first.jour)}</text>
    <text x="${W - padR}" y="${H - 6}" font-size="11" fill="var(--text-3)" text-anchor="end">${esc(last.jour)}</text>
  </svg>`;

  if (!usageResizeBound && typeof window !== 'undefined' && window.addEventListener) {
    usageResizeBound = true;
    let timer = null;
    window.addEventListener('resize', () => { clearTimeout(timer); timer = setTimeout(renderUsage, 150); });
  }

  const perDay = trend.par_jour;
  const vars = { days: trend.jours, day: go(Math.abs(perDay)), month: go(Math.abs(perDay) * 30),
    from: samples[0].jour, to: last.jour, n: samples.length };
  $('#usageNote').textContent = trend.fiable
    ? (perDay > 0
      ? t("Sur {days} jours, l'occupation monte de {day} par jour — soit {month} par mois. {from} → {to}, {n} relevés.", vars)
      : perDay < 0
        ? t("Sur {days} jours, l'occupation descend de {day} par jour — soit {month} par mois. {from} → {to}, {n} relevés.", vars)
        : t("Sur {days} jours, l'occupation est stable. {from} → {to}, {n} relevés.", vars))
    : t('{n} relevé(s) — il en faut au moins trois sur deux jours pour annoncer une pente honnête.', vars);
}

/* ---------------------------------------------------------------- library */

/** Blockers carry a stable code next to their (translated) text. */
const has = (item, ...codes) => (item.blocker_codes || []).some(c => codes.includes(c));
const HARD = ['unique_copy', 'unmanaged', 'unseen'];   // never overridden by hand

function itemState(item) {
  if (has(item, 'tag', 'title')) return { cls: 'keep', text: t('protégé') };
  if (has(item, 'in_progress')) return { cls: 'keep', text: t('en cours') };
  if (has(item, 'collection')) return { cls: 'keep', text: t('collection') };
  if (item.unmanaged) return { cls: 'hold', text: t('hors *arr') };
  if (item.actionable) return { cls: 'ok', text: t('candidat') };
  if (item.eligible && item.seed_blocked && item.unblocks_in_days !== null) {
    return {
      cls: 'hold',
      text: item.unblocks_in_days === 0 ? t('imminent') : t('dans {n} j', { n: item.unblocks_in_days }),
    };
  }
  if (item.eligible) return { cls: 'hold', text: t('en attente') };
  return { cls: 'none', text: '—' };
}

/** A series' seasons, by key “season:<id>:<n>”, sorted by number. */
function seasonsOf(seriesKey) {
  const id = seriesKey.split(':')[1];
  return S.médias
    .filter(m => m.kind === 'season' && m.key.startsWith(`season:${id}:`))
    .sort((a, b) => +a.key.split(':')[2] - +b.key.split(':')[2]);
}

function visible() {
  const query = $('#q').value.trim().toLowerCase();
  return S.médias.filter(item => {
    // a season no rule targets stays tucked under its series
    if (item.kind === 'season' && !item.eligible) return false;
    if (query && !item.title.toLowerCase().includes(query)) return false;
    if (whoFilter && !item.requesters.includes(whoFilter)) return false;
    const idle = item.idle_days;
    for (const f of (viewFilter === 'all' ? [] : [viewFilter])) {
      if (f === 'never' && item.last_viewed) return false;
      if (f === 'idle' && !(idle !== null && idle > 180)) return false;
      if (f === 'candidate' && !item.actionable) return false;
      if (f === 'blocked' && !(item.eligible && !item.actionable)) return false;
      if (f === 'soon' && !(item.unblocks_in_days !== null && item.unblocks_in_days <= 30
        && !item.actionable)) return false;
      if (f === 'protected' && itemState(item).cls !== 'keep') return false;
      if (f === 'resuming' && !item.in_progress) return false;
      if (f === 'unmanaged' && !item.unmanaged) return false;
    }
    return true;
  }).sort((a, b) => {
    let x = a[sortKey], y = b[sortKey];
    if (sortKey === 'torrents') { x = a.torrents.length; y = b.torrents.length; }
    if (sortKey === 'state') { x = itemState(a).text; y = itemState(b).text; }
    if (sortKey === 'last_viewed') { x = x || 0; y = y || 0; }
    if (typeof x === 'string') return x.localeCompare(y, LANG) * sortDir;
    return ((x || 0) - (y || 0)) * sortDir;
  });
}

function renderLibrary() {
  renderWhoFilter();
  const rows = visible();
  $('#count').textContent = t('{n} élément(s) · {size}', {
    n: nf.format(rows.length), size: go(rows.reduce((a, r) => a + r.library_bytes, 0)) });
  $('#noRows').hidden = rows.length > 0;

  $('#rows').replaceChildren(...rows.flatMap(item => {
    const seasons = item.kind === 'series' && !item.unmanaged ? seasonsOf(item.key) : [];
    const isOpen = expanded.has(item.key);
    const st = itemState(item);
    const checked = selection.has(item.key);
    const seed = item.torrents.length
      ? item.torrents.map(tor => `${tor.tracker} ratio ${tor.ratio} · ${dayCount(tor.seed_days)}`).join('\n')
      : t('aucun torrent');
    const toggle = seasons.length > 1
      ? el('button', { class: 'link', style: 'margin-right:6px;font-size:13px',
          title: isOpen ? t('Replier les saisons') : t('Choisir des saisons une par une'),
          onclick: e => { e.stopPropagation(); isOpen ? expanded.delete(item.key) : expanded.add(item.key); renderLibrary(); },
        }, `${isOpen ? '▾' : '▸'} ` + t('{n} saisons', { n: seasons.length }))
      : null;
    const tr = el('tr', { class: checked ? 'sel' : '' },
      el('td', {}, el('input', {
        type: 'checkbox', checked, onchange: e => {
          if (e.target.checked) {
            selection.add(item.key);
            // the series takes its seasons along: ticking them too would be redundant
            seasons.forEach(se => selection.delete(se.key));
          } else {
            selection.delete(item.key);
          }
          if (isOpen) { renderLibrary(); return; }
          tr.classList.toggle('sel', e.target.checked);
          renderSelection();
        },
      })),
      el('td', { class: 't' }, toggle, item.title,
        el('div', { class: 'meta' },
          KIND[item.kind] + (item.rule ? ' · ' + t('règle « {name} »', { name: item.rule }) : '')
          + ' · ' + t('{n} fichier(s) · en place depuis {days} j', { n: item.files, days: item.age_days })
          + (item.requesters.length ? ' · ' + t('demandé par {who}', { who: item.requesters.join(', ') }) : '')
          + (item.in_progress
              ? ' · ' + (item.in_progress > 1
                ? t('commencé par {n} comptes', { n: item.in_progress })
                : t('commencé par {n} compte', { n: item.in_progress }))
              : '')
          + (item.collections?.length ? ' · ' + t('collection {names}', { names: item.collections.join(', ') }) : '')
          + (item.unmanaged ? ' · ' + t('aucun *arr ne le gère') : '')
          + (item.torrents.length ? ' · ' + t('{n} torrent(s)', { n: item.torrents.length }) : '')),
        item.blockers.length ? el('div', { class: 'why', text: item.blockers[0] }) : null),
      el('td', { class: 'r num', text: go(item.library_bytes) }),
      el('td', { class: 'r num', title: seed, text: item.freed_bytes ? go(item.freed_bytes) : '—' }),
      el('td', { class: 'hide-s num', text: dateFr(item.last_viewed)
        + (item.viewers ? ' · ' + t('{n} pers.', { n: item.viewers }) : '') }),
      el('td', {}, el('span', { class: 'pill ' + st.cls, text: st.text })));
    if (!isOpen) return [tr];
    // expanded seasons: same columns, indented, selectable one by one
    return [tr, ...seasons.map(se => {
      const sst = itemState(se);
      const schecked = selection.has(se.key);
      const n = se.key.split(':')[2];
      const str = el('tr', { class: 'season-row' + (schecked ? ' sel' : '') },
        el('td', {}, el('input', { type: 'checkbox', checked: schecked, disabled: checked,
          title: checked ? t('La série entière est déjà sélectionnée') : '', onchange: e => {
          e.target.checked ? selection.add(se.key) : selection.delete(se.key);
          str.classList.toggle('sel', e.target.checked);
          renderSelection();
        } })),
        el('td', { class: 't', style: 'padding-left:34px' }, t('Saison {n}', { n }),
          el('div', { class: 'meta' }, t('{n} épisode(s)', { n: se.files })
            + (se.rule ? ' · ' + t('règle « {name} »', { name: se.rule }) : '')
            + (se.blockers.length ? ` · ${se.blockers[0]}` : ''))),
        el('td', { class: 'r num', text: go(se.library_bytes) }),
        el('td', { class: 'r num', text: se.freed_bytes ? go(se.freed_bytes) : '—' }),
        el('td', { class: 'hide-s num', text: dateFr(se.last_viewed)
          + (se.viewers ? ' · ' + t('{n} pers.', { n: se.viewers }) : '') }),
        el('td', {}, el('span', { class: 'pill ' + sst.cls, text: sst.text })));
      return str;
    })];
  }));
  $('#all').checked = rows.length > 0 && rows.every(r => selection.has(r.key));
  renderSelection();
}

/** “Requested by” selector: rebuilt with each state, selection kept. */
function renderWhoFilter() {
  const chip = $('#whoChip');
  chip.hidden = !whoFilter;
  if (whoFilter) { chip.textContent = t('Demandé par {who} ✕', { who: whoFilter }); chip.setAttribute('aria-pressed', 'true'); }
  const node = $('#who');
  const people = S.demandes.demandeurs;
  node.replaceChildren(
    el('option', { value: '' }, t('Demandé par : tout le monde')),
    ...people.map(p => el('option', { value: p.nom, selected: p.nom === whoFilter },
      t('{who} — {size} jamais vus', { who: p.nom, size: go(p.octets_jamais_vus) }))));
  node.value = whoFilter;
}

/** From the Requests section: show what someone requested that nobody used. */
function showRequesterWaste(name) {
  whoFilter = name;
  viewFilter = 'never';
  $('#view').value = 'never';
  $('#q').value = '';
  tab('library');
  setLibraryView('media');
}

/** Library or Seed-only: one list visible at a time. */
function setLibraryView(view) {
  libraryView = view;
  $('#viewMedia').setAttribute('aria-pressed', view === 'media');
  $('#viewOrphans').setAttribute('aria-pressed', view === 'orphans');
  $('#mediaBlock').hidden = view !== 'media';
  $('#orphans').hidden = view !== 'orphans';
  $('#q').hidden = $('#view').hidden = view !== 'media';
  renderLibrary();
  renderOrphans();
}

function renderSelection() {
  const chosen = S.médias.filter(m => selection.has(m.key));
  $('#actionbar').hidden = chosen.length === 0 || $('#library').hidden || libraryView !== 'media';
  $('#adoptBtn').hidden = !chosen.some(m => m.unmanaged);
  const deletable = chosen.some(m => !has(m, ...HARD));
  $('#deleteBtn').disabled = chosen.length > 0 && !deletable;
  $('#deleteBtn').title = deletable ? ''
    : t("Rien de supprimable ici : ces éléments sont hors *arr (adopte-les d'abord) ou détiennent une copie unique.");
  $('#selN').textContent = chosen.length;
  $('#selGb').textContent = go(chosen.reduce((a, m) => a + m.freed_bytes, 0));
}

/* --------------------------------------------------------------- deletion */

function openConfirm() {
  const chosen = S.médias.filter(m => selection.has(m.key));
  const hardBlocked = chosen.filter(m => has(m, ...HARD));
  const seedBlocked = chosen.filter(m => has(m, 'seed'));
  const overridden = chosen.filter(m => !hardBlocked.includes(m)
    && has(m, 'tag', 'title', 'collection', 'in_progress', 'recent_request'));
  const willGo = chosen.filter(m => !hardBlocked.includes(m));

  $('#cSub').textContent = willGo.length
    ? t('{n} élément(s), {size} réellement libérés.', { n: willGo.length, size: go(willGo.reduce((a, m) => a + m.freed_bytes, 0)) })
    : t('Rien à supprimer dans cette sélection.');
  $('#cList').replaceChildren(...willGo.slice(0, 40).map(m => el('li', {},
    m.title, ' — ', go(m.freed_bytes),
    m.torrents.length ? ' · ' + t('{n} torrent(s)', { n: m.torrents.length }) : '',
    m.seerr_requests ? ' · ' + t('{n} request Seerr', { n: m.seerr_requests }) : '')));
  if (willGo.length > 40) $('#cList').append(el('li', { text: t('… et {n} autres', { n: willGo.length - 40 }) }));

  const zero = willGo.filter(m => m.freed_bytes === 0);
  $('#cZero').hidden = zero.length === 0;
  if (zero.length) {
    $('#cZero').textContent = t("{n} élément(s) ne libéreront aucun octet : leurs torrents restent en seed et gardent les fichiers. Tu perdrais le média sans récupérer d'espace, et il deviendrait invisible pour sweeper. Mieux vaut attendre, ou cocher la case ci-dessous pour retirer aussi les torrents.", { n: zero.length });
  }

  const warnings = [];
  const unmanagedRefused = hardBlocked.filter(m => m.unmanaged);
  if (hardBlocked.length) {
    warnings.push(hardBlocked.map(m => t('« {title} » — {why}', { title: m.title, why: m.unmanaged
      ? t("aucun *arr ne le gère : à adopter dans Radarr/Sonarr d'abord")
      : has(m, 'unseen') ? m.blockers[0]
      : t("un torrent détient l'unique copie du fichier") })).join(' · ')
      + t('. Ces refus ne sont jamais contournables.'));
  }
  if (overridden.length) {
    warnings.push(t('Tu passes outre une protection pour {n} élément(s) : ', { n: overridden.length })
      + overridden.slice(0, 4).map(m => t('« {title} » ({why})', { title: m.title, why: m.blockers[0].split(' — ')[0] })).join(', ')
      + (overridden.length > 4 ? '…' : '') + t(". Une sélection manuelle l'autorise ; le passage automatique, jamais."));
  }
  if (seedBlocked.length) {
    warnings.push(t("{n} élément(s) n'ont pas rempli leurs obligations de seed.", { n: seedBlocked.length }));
  }
  $('#cWarn').hidden = warnings.length === 0;
  $('#cWarn').replaceChildren(
    ...warnings.map(w => el('div', { text: w })),
    unmanagedRefused.length ? el('div', { style: 'margin-top:10px' },
      el('button', { class: 'act primary', text: t("Adopter d'abord ({n})", { n: unmanagedRefused.length }),
        onclick: () => { $('#confirm').close(); openAdopt(); } })) : null);
  $('#cForceWrap').hidden = seedBlocked.length === 0;
  $('#cForce').checked = false;
  $('#cGo').disabled = willGo.length === 0;
  $('#confirm').showModal();
}

/** Follow a background job to the end, updating the progress bar. */
async function followJob(job, onTick) {
  while (job && !job.terminé) {
    await new Promise(r => setTimeout(r, 900));
    try {
      job = await api('/api/job?id=' + encodeURIComponent(job.id));
    } catch (error) {
      return { ...job, erreur: error.message, terminé: true };
    }
    if (onTick) onTick(job);
  }
  return job;
}

function jobProgress(job) {
  const pct = job.total ? Math.round(job.faits / job.total * 100) : 0;
  $('#cProgress').hidden = false;
  $('#cBar').style.width = pct + '%';
  const done = job.total && job.faits >= job.total;
  $('#cStatus').textContent = done && !job.terminé
    ? t("{done} traité(s) · {size} libérés — recalcul de l'inventaire…", { done: job.faits, size: go(job.octets_libérés) })
    : t('{done} / {total} traité(s) · {size} libérés', { done: job.faits, total: job.total || '?', size: go(job.octets_libérés) });
}

async function doDelete() {
  const button = $('#cGo');
  button.disabled = true;
  button.textContent = t('Suppression…');
  $('#cCancel').disabled = true;
  try {
    const started = await api('/api/delete', {
      method: 'POST',
      body: JSON.stringify({ clés: [...selection], forcer_seed: $('#cForce').checked }),
    });
    jobProgress(started.job);
    const job = await followJob(started.job, jobProgress);
    const failed = job.résultats.filter(r => r.erreurs.length);
    $('#confirm').close();
    selection.clear();
    if (job.erreur) toast(t('Échec : ') + job.erreur, true);
    else if (failed.length) toast(t('{n} échec(s) — {error}', { n: failed.length, error: failed[0].erreurs[0] }), true);
    else toast(t('{n} élément(s) supprimé(s), {size} libérés.', { n: job.faits, size: go(job.octets_libérés) }));
    await load();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = t('Supprimer définitivement');
    $('#cCancel').disabled = false;
    $('#cProgress').hidden = true;
  }
}

/** Hand over to Radarr/Sonarr what Plex serves without any *arr — after a preview. */
async function openAdopt() {
  const keys = S.médias.filter(m => selection.has(m.key) && m.unmanaged).map(m => m.key);
  if (!keys.length) return;
  $('#aList').replaceChildren(el('li', { text: t('Recherche des correspondances…') }));
  $('#aWarn').hidden = true;
  $('#aGo').disabled = true;
  $('#adoptDialog').showModal();
  try {
    const data = await api('/api/adopt/preview', { method: 'POST', body: JSON.stringify({ clés: keys }) });
    const ok = data.aperçu.filter(x => x.possible);
    const ko = data.aperçu.filter(x => !x.possible);
    $('#aSub').textContent = t('{n} élément(s) adoptable(s)', { n: ok.length }) + (ko.length ? t(', {n} impossible(s)', { n: ko.length }) : '');
    // Sonarr sometimes returns “Title (2023)”: don't repeat the year
    const label = c => (c.année && String(c.titre).endsWith(`(${c.année})`))
      ? c.titre : `${c.titre} (${c.année ?? '?'})`;
    $('#aList').replaceChildren(...ok.map(x => el('li', {},
      el('b', { text: x.titre }), ' → ', label(x.correspondance),
      x.correspondance.sûre ? null
        : el('span', { class: 'pill hold', style: 'margin-left:6px',
            title: t('Appariement par titre et année, pas par identifiant : vérifie avant de confirmer'),
            text: t('par titre — vérifie') }),
      el('span', { class: 'meta', text: ' · ' + t('via {how}', { how: x.correspondance.via }) +
        (x.correspondance.tvdb ? ` · tvdb ${x.correspondance.tvdb}` : '') +
        (x.correspondance.tmdb ? ` · tmdb ${x.correspondance.tmdb}` : '') }),
      el('div', { class: 'meta', text: t('dossier : {path}', { path: x.dossier }) }))));
    if (ko.length) {
      $('#aWarn').hidden = false;
      const bare = ko.filter(x => x.nu);
      $('#aWarn').replaceChildren(
        ...ko.map(x => el('div', {}, el('b', { text: x.titre }), ' — ', x.raison)),
        bare.length ? el('div', { style: 'margin-top:10px' },
          el('button', { class: 'act', onclick: () => tidyBare(bare.map(x => x.clé)) },
            t('Ranger {n} film(s) dans un dossier', { n: bare.length })),
          el('div', { class: 'meta', style: 'margin-top:6px' },
            t("Renommage sur le même disque : réversible, les torrents ne voient rien, Plex retrouve le fichier au nouveau chemin à son prochain scan. Ensuite, relance l'adoption."))) : null);
    }
    $('#aGo').disabled = ok.length === 0;
    $('#aGo').onclick = async () => {
      $('#aGo').disabled = true; $('#aGo').textContent = t('Adoption…');
      try {
        const started = await api('/api/adopt', { method: 'POST', body: JSON.stringify({ clés: keys }) });
        const job = await followJob(started.job, j => { $('#aGo').textContent = `${t('Adoption…')} ${j.faits}/${j.total}`; });
        const failed = job.résultats.filter(r => r.erreurs.length);
        $('#adoptDialog').close();
        selection.clear();
        toast(t('{n} élément(s) adopté(s)', { n: job.faits - failed.length })
          + (failed.length ? t(', {n} échec(s) : {error}', { n: failed.length, error: failed[0].erreurs[0] }) : '.')
          + (started.refusés.length ? ' ' + t('{n} refusé(s).', { n: started.refusés.length }) : ''), failed.length > 0);
        await load();
      } catch (error) { toast(t('Échec : ') + error.message, true); }
      finally { $('#aGo').disabled = false; $('#aGo').textContent = t('Adopter'); }
    };
  } catch (error) {
    $('#aList').replaceChildren(el('li', { text: t('Aperçu indisponible : ') + error.message }));
  }
}

/** Move movies sitting bare at the root into a folder named after them (a rename). */
async function tidyBare(keys) {
  try {
    const pv = await api('/api/tidy/preview', { method: 'POST', body: JSON.stringify({ clés: keys }) });
    const ok = pv.aperçu.filter(x => x.possible), ko = pv.aperçu.filter(x => !x.possible);
    const lines = ok.map(x => `• ${x.titre}\n    → ${x.dossier}/\n    (${x.fichiers.join(', ')})`);
    if (ko.length) lines.push(...ko.map(x => `✗ ${x.titre} : ${x.raison}`));
    if (!ok.length) { toast(t('Rien à ranger : ') + (ko[0]?.raison || '—'), true); return; }
    if (!confirm(t('Ranger {n} film(s) dans un dossier à leur nom ?', { n: ok.length }) + `\n\n${lines.join('\n')}\n\n`
      + t('Renommage pur sur le même disque — réversible.'))) return;
    const started = await api('/api/tidy', { method: 'POST', body: JSON.stringify({ clés: keys }) });
    const job = await followJob(started.job);
    const failed = job.résultats.filter(r => r.erreurs.length);
    toast(t('{n} film(s) rangé(s)', { n: job.faits - failed.length }) + (failed.length ? t(', {n} échec(s) : {error}', { n: failed.length, error: failed[0].erreurs[0] }) : '.'), failed.length > 0);
    $('#adoptDialog').close();
    await load();
    // “plex:movie:<guid>” keys survive the rename: the preview can be reopened
    keys.forEach(k => selection.add(k));
    renderSelection();
    openAdopt();
  } catch (error) { toast(t('Échec : ') + error.message, true); }
}

/** Mark a selection “delete once seeding obligations are met”. */
async function queueUp(keys, kind) {
  if (kind === 'media') {
    // an item outside *arr can never become “ready”: adopt it first
    const byKey = new Map(S.médias.map(m => [m.key, m]));
    const skipped = keys.filter(k => byKey.get(k)?.unmanaged);
    if (skipped.length) toast(t("{n} élément(s) hors *arr ignoré(s) : adopte-les d'abord.", { n: skipped.length }), true);
    keys = keys.filter(k => !byKey.get(k)?.unmanaged);
  }
  if (!keys.length) return;
  try {
    const data = await api('/api/watchlist', {
      method: 'POST', body: JSON.stringify({ action: 'add', kind, clés: keys }),
    });
    toast(t("{n} élément(s) en file d'attente ({total} au total).", { n: keys.length, total: data.file_attente.length }));
    selection.clear();
    oSelection.clear();
    await load();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
  }
}

async function setProtection(on) {
  try {
    await api('/api/protect', {
      method: 'POST',
      body: JSON.stringify({ clés: [...selection], protéger: on }),
    });
    toast(on ? t('Protection posée.') : t('Protection retirée.'));
    selection.clear();
    await load();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
  }
}

/* -------------------------------------------------------------- seed-only */

function orphanState(group) {
  if (group.blocked) return { cls: 'none', text: t('bloqué') };
  if (group.seed_ok) return { cls: 'ok', text: t('seed payé') };
  if (group.unblocks_in_days === null) return { cls: 'hold', text: t('ratio à atteindre') };
  return { cls: 'hold', text: t('dans {n} j', { n: group.unblocks_in_days }) };
}

function visibleOrphans() {
  return S.orphelins.filter(group => {
    for (const f of oFilters) {
      if (f === 'ready' && !group.seed_ok) return false;
      if (f === 'soon' && !(group.unblocks_in_days !== null && group.unblocks_in_days <= 30))
        return false;
    }
    return true;
  }).sort((a, b) => {
    let x = a[oSortKey], y = b[oSortKey];
    if (oSortKey === 'unblocks_in_days') { x = x ?? 9999; y = y ?? 9999; }
    if (typeof x === 'string') return x.localeCompare(y, LANG) * oSortDir;
    return ((x || 0) - (y || 0)) * oSortDir;
  });
}

function renderOrphans() {
  const totals = S.orphelins_totaux;
  $('#orphanKpis').textContent = t("{total} sur le disque qu'aucun Radarr ni Sonarr ne connaît — {ready} dont le seed est payé, {waiting} en attente. Releases remplacées, ou médias supprimés dont le torrent n'avait pas fini de seeder.",
    { total: go(totals.octets), ready: go(totals.prêts), waiting: go(totals.en_attente) });

  const rows = visibleOrphans();
  $('#noOrphans').hidden = rows.length > 0;
  $('#orphanRows').replaceChildren(...rows.map(group => {
    const st = orphanState(group);
    const checked = oSelection.has(group.key);
    const tr = el('tr', { class: checked ? 'sel' : '' },
      el('td', {}, el('input', {
        type: 'checkbox', checked, onchange: e => {
          e.target.checked ? oSelection.add(group.key) : oSelection.delete(group.key);
          tr.classList.toggle('sel', e.target.checked);
          renderOrphanSelection();
        },
      })),
      el('td', { class: 't' }, group.name.slice(0, 64),
        el('div', { class: 'meta' },
          t('{n} fichier(s) · {trackers}', { n: group.files, trackers: group.trackers.join(', ') || t('tracker inconnu') })),
        group.blocked ? el('div', { class: 'why', text: group.blocked }) : null),
      el('td', { class: 'r num', text: go(group.bytes) }),
      el('td', { class: 'r hide-s num', text: group.torrents }),
      el('td', { class: 'r hide-s num', text: group.ratio.toFixed(2) }),
      el('td', { class: 'r hide-s num', text: dayCount(group.seed_days) }),
      el('td', {}, el('span', { class: 'pill ' + st.cls, text: st.text })));
    return tr;
  }));
  $('#allOrphans').checked = rows.length > 0 && rows.every(r => oSelection.has(r.key));
  renderOrphanSelection();
}

function renderOrphanSelection() {
  const chosen = S.orphelins.filter(g => oSelection.has(g.key));
  $('#orphanbar').hidden = chosen.length === 0 || $('#library').hidden || libraryView !== 'orphans';
  $('#oselN').textContent = chosen.length;
  $('#oselGb').textContent = go(chosen.reduce((a, g) => a + g.bytes, 0));
}

async function deleteOrphans() {
  const chosen = S.orphelins.filter(g => oSelection.has(g.key));
  const notReady = chosen.filter(g => !g.seed_ok && !g.blocked);
  let force = false;
  if (notReady.length) {
    force = confirm(t("{n} lot(s) n'ont pas fini de seeder ({size}).", {
      n: notReady.length, size: go(notReady.reduce((a, g) => a + g.bytes, 0)) }) + '\n\n'
      + t('OK  : les retirer quand même — risque de H&R sur le tracker.') + '\n'
      + t('Annuler : ne traiter que ceux dont le seed est payé.'));
  }
  const button = $('#odeleteBtn');
  button.disabled = true;
  button.textContent = t('Retrait…');
  try {
    const keys = force ? [...oSelection]
      : chosen.filter(g => g.seed_ok).map(g => g.key);
    if (!keys.length) { toast(t('Rien à retirer.'), true); return; }
    const started = await api('/api/orphans/delete', {
      method: 'POST', body: JSON.stringify({ clés: keys, forcer_seed: force }),
    });
    const job = await followJob(started.job, j => {
      button.textContent = `${t('Retrait…')} ${j.faits}/${j.total}`;
    });
    const ok = job.résultats.filter(r => !r.erreurs.length);
    toast(t('{n} lot(s) retiré(s), {size} libérés.', { n: ok.length, size: go(job.octets_libérés) }));
    oSelection.clear();
    await load();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = t('Retirer les torrents…');
  }
}

/* ------------------------------------------------------------------ rules */

function renderRules() {
  if (!ruleDraft) ruleDraft = structuredClone(S.règles);
  const dirty = JSON.stringify(ruleDraft) !== JSON.stringify(S.règles);
  $('#saveRules').disabled = !dirty;
  const byName = Object.fromEntries(preview.map(p => [p.nom, p]));

  $('#ruleList').replaceChildren(...ruleDraft.map((rule, index) => {
    const touch = () => { schedulePreview(); renderRules(); };
    const move = (from, to) => {
      if (to < 0 || to >= ruleDraft.length || from === to) return;
      ruleDraft.splice(to, 0, ruleDraft.splice(from, 1)[0]);
      touch();
    };

    const card = el('div', { class: 'rule' + (rule.enabled ? '' : ' off') },
      el('div', { class: 'top' },
        el('span', {
          class: 'grip', title: t('Glisser pour réordonner'),
          onmousedown: () => { card.draggable = true; },
          onmouseup: () => { card.draggable = false; },
        }, '⣿'),
        el('span', {
          class: 'rank', title: t("Rang d'évaluation — la première règle qui correspond l'emporte"), text: String(index + 1),
        }),
        el('span', { class: 'movers' },
          el('button', {
            title: t('Monter'), disabled: index === 0,
            onclick: () => move(index, index - 1),
          }, '↑'),
          el('button', {
            title: t('Descendre'), disabled: index === ruleDraft.length - 1,
            onclick: () => move(index, index + 1),
          }, '↓')),
        el('input', {
          type: 'checkbox', checked: rule.enabled, title: t('Activer la règle'),
          onchange: e => { rule.enabled = e.target.checked; touch(); },
        }),
        el('input', {
          type: 'text', value: rule.name, placeholder: t('Nom de la règle'),
          oninput: e => { rule.name = e.target.value; $('#saveRules').disabled = false; schedulePreview(); },
        }),
        impact(byName[rule.name]),
        ruleHistory(rule.name),
        el('button', {
          class: 'link', text: t('Supprimer'),
          onclick: () => { ruleDraft.splice(index, 1); touch(); },
        })),
      el('div', { class: 'fields' },
        el('label', {}, t('Porte sur '),
          select(['movie', 'series', 'season'], [t('les films'), t('les séries entières'), t('les saisons')],
            rule.media, v => { rule.media = v; touch(); })),
        el('label', {}, t('Critère '),
          select(['never', 'idle'], [t('jamais regardé'), t('pas regardé depuis')],
            rule.never_watched ? 'never' : 'idle', v => {
              rule.never_watched = v === 'never';
              rule.unwatched_days = v === 'never' ? null : (rule.unwatched_days || 180);
              touch();
            })),
        rule.never_watched ? null : el('label', {},
          el('input', {
            type: 'number', min: 1, value: rule.unwatched_days || 180,
            oninput: e => { rule.unwatched_days = +e.target.value; $('#saveRules').disabled = false; schedulePreview(); },
          }), t(' jours')),
        el('label', {}, t('En place depuis au moins '),
          el('input', {
            type: 'number', min: 0, value: rule.min_age_days,
            oninput: e => { rule.min_age_days = +e.target.value; $('#saveRules').disabled = false; schedulePreview(); },
          }), t(' jours')),
        el('label', { title: t('Épargne ce qui a plu à plus de N personnes') },
          t('Épargner si vu par plus de '),
          el('input', {
            type: 'number', min: 0, placeholder: '∞',
            value: rule.max_distinct_viewers ?? '',
            oninput: e => {
              rule.max_distinct_viewers = e.target.value === '' ? null : +e.target.value;
              $('#saveRules').disabled = false;
            },
          }), t(' spectateurs'))));
    return card;
  }));

  if (!ruleDraft.length) {
    $('#ruleList').append(el('div', { class: 'empty', text: t('Aucune règle. Rien ne sera supprimé automatiquement.') }));
  }

  // two active rules on the same type: the second only sees what is left
  const overlaps = {};
  ruleDraft.filter(r => r.enabled).forEach(r => {
    overlaps[r.media] = (overlaps[r.media] || 0) + 1;
  });
  const shadowed = Object.entries(overlaps).filter(([, n]) => n > 1);
  const label = { movie: t('les films'), series: t('les séries'), season: t('les saisons') };
  // explain the mechanism without claiming it changes anything: two rules can
  // target the same media and stay disjoint (never watched / dormant)
  $('#orderNote').textContent = shadowed.length
    ? t("Évaluation de haut en bas, la première qui correspond l'emporte. ")
      + shadowed.map(([media, n]) => t('{n} règles actives sur {what}', { n, what: label[media] })).join(', ')
      + t(" : celles du bas ne voient que ce que celles du haut n'ont pas pris. Les compteurs ci-dessus reflètent l'ordre actuel — si déplacer une règle ne les change pas, c'est que leurs critères ne se recoupent pas.")
    : t("Évaluation de haut en bas, la première qui correspond l'emporte. Une seule règle active par type de média : l'ordre ne change rien pour l'instant.");

  const p = S.protection, s = S.seeding;
  renderCollections();
  const resuming = S.médias.filter(m => m.in_progress);
  const guard = { tags: p.tags.join(', '), days: p.requested_within_days, ratio: s.min_ratio, seed: s.min_seed_days };
  $('#guards').textContent = (s.mode === 'both'
    ? t('Tag de protection « {tags} » · rien de demandé sur Seerr depuis moins de {days} jours · seed requis : ratio ≥ {ratio} et {seed} jours de partage', guard)
    : t('Tag de protection « {tags} » · rien de demandé sur Seerr depuis moins de {days} jours · seed requis : ratio ≥ {ratio} ou {seed} jours de partage', guard))
    + (p.in_progress
        ? ' · ' + t("{n} titre(s) épargnés parce qu'ils sont dans la liste « Continuer à regarder » de quelqu'un.", { n: resuming.length })
        : ' · ' + t('les visionnages en cours ne sont PAS épargnés.'));
}

/** A rule's actual record from the log: what it deleted, and when. */
function ruleHistory(name) {
  const st = (S.stats_règles || {})[name];
  if (!st) return el('span', { class: 'meta', text: t('jamais exécutée') });
  return el('span', { class: 'meta', title: t("D'après le journal des suppressions"),
    text: t('{n} supprimé(s) · {size} · dernier {date}', { n: st.éléments, size: go(st.octets), date: dateFr(st.dernier) }) });
}

/** Small “what this rule would catch” label, computed without saving anything. */
function impact(entry) {
  if (!entry) return el('span', { class: 'meta', text: t('calcul…') });
  if (!entry.éléments) return el('span', { class: 'pill none', text: t('aucun élément') });
  const held = entry.bloqués
    ? t(', dont {n} en attente de seed', { n: entry.bloqués }) : '';
  return el('span', {
    class: 'pill ' + (entry.activée ? 'ok' : 'none'),
    title: entry.exemples.join(' · ') + held,
    text: t('{n} élément(s) · {size}', { n: entry.éléments, size: go(entry.octets) }),
  });
}

/** Ask the server for a preview, once typing settles. */
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    try {
      const data = await api('/api/rules/preview', {
        method: 'POST', body: JSON.stringify({ règles: ruleDraft || S.règles }),
      });
      preview = data.aperçu;
      const active = preview.filter(p => p.activée);
      $('#previewNote').textContent = active.length
        ? t('Ces règles attraperaient {n} élément(s), {size} — avant application des protections et des obligations de seed.',
          { n: active.reduce((a, p) => a + p.éléments, 0), size: go(active.reduce((a, p) => a + p.octets, 0)) })
        : t('Aucune règle active : rien ne serait supprimé automatiquement.');
      if (!$('#rules').hidden) renderRules();
    } catch (error) {
      $('#previewNote').textContent = t('Aperçu indisponible : ') + error.message;
    }
  }, 350);
}

/* ---------------------------------------------------------------- requests */

function renderRequests() {
  const data = S.demandes, tot = data.totaux, people = data.demandeurs;

  $('#reqKpis').replaceChildren(
    el('div', { class: 'card kpi' },
      el('div', { class: 'label', text: t('Volume demandé') }),
      el('div', { class: 'value hero num', text: go(tot.octets) }),
      el('div', { class: 'note' }, t('{n} demandes', { n: nf.format(tot.demandes) }))),
    el('div', { class: 'card kpi' },
      el('div', { class: 'label', text: t('Jamais regardé') }),
      el('div', { class: 'value num', text: go(tot.octets_jamais_vus) }),
      el('div', { class: 'note' },
        el('b', { text: Math.round(tot.octets_jamais_vus / (tot.octets || 1) * 100) + ' %' }),
        t(' du volume demandé'))),
    el('div', { class: 'card kpi' },
      el('div', { class: 'label', text: t('Demandeurs') }),
      el('div', { class: 'value num', text: nf.format(tot.demandeurs) }),
      el('div', { class: 'note' },
        t('{n} actifs sur 90 jours', { n: people.filter(p => p.demandes_90j).length }))),
  );

  // stacked bars: one per requester, scaled to the largest
  const peak = Math.max(...people.map(p => p.octets), 1);
  $('#reqBars').replaceChildren(...people.map(p => {
    const seen = p.octets_vus / peak * 100;
    const unseen = p.octets_jamais_vus / peak * 100;
    return el('div', { class: 'reqbar' },
      el('div', { class: 'who' },
        el('button', { class: 'link', title: t('Filtrer la médiathèque sur {who}', { who: p.nom }),
          onclick: () => showRequesterWaste(p.nom), text: p.nom })),
      el('div', { class: 'lane' },
        seen > 0 ? el('span', { style: `width:${seen}%;background:var(--series-1)`,
          title: t('{size} regardés', { size: go(p.octets_vus) }) }) : null,
        unseen > 0 ? el('span', { style: `width:${unseen}%;background:var(--series-2)`,
          title: t('{size} jamais regardés', { size: go(p.octets_jamais_vus) }) }) : null),
      el('div', { class: 'tot', text: go(p.octets) }));
  }));

  $('#reqRows').replaceChildren(...people.map(p => el('tr', {},
    el('td', { class: 't' },
      el('button', { class: 'link', style: 'font-weight:550;font-size:14px',
        title: t('Voir ce que {who} a demandé sans que personne le regarde', { who: p.nom }),
        onclick: () => showRequesterWaste(p.nom), text: p.nom }),
      el('div', { class: 'meta',
        text: p.dernière_demande
          ? t('dernière demande {date}', { date: dateFr(p.dernière_demande) }) : t('aucune date') })),
    el('td', { class: 'r num', text: nf.format(p.demandes) }),
    el('td', { class: 'r hide-s num', text: p.demandes_90j || '—' }),
    el('td', { class: 'r num', text: go(p.octets) }),
    el('td', { class: 'r num' },
      go(p.octets_jamais_vus),
      el('div', { class: 'meta', text: Math.round(p.part_jamais_vue * 100) + ' %' })),
    el('td', { class: 'meta hide-s',
      text: p.exemples.length ? p.exemples.join(' · ') : '—' }))));

  const top = people[0];
  const share = top ? Math.round(top.octets / (tot.octets || 1) * 100) : 0;
  $('#reqNote').textContent = top
    ? t("{who} pèse {share} % du volume demandé. {n} demande(s) n'ont pas pu être reliées à un média en bibliothèque — supprimées depuis, ou jamais téléchargées.",
      { who: top.nom, share, n: tot.non_retrouvées })
    : '';
}

/* ---------------------------------------------------------------- trackers */

function seedingBase() {
  return {
    mode: S.seeding.mode,
    min_ratio: S.seeding.min_ratio,
    min_seed_days: S.seeding.min_seed_days,
    par_tracker: Object.fromEntries(S.trackers
      .filter(tk => tk.règle_propre)
      .map(tk => [tk.groupe, { min_ratio: tk.min_ratio, min_seed_days: tk.min_seed_days }])),
  };
}

function renderTrackers() {
  if (!seedDraft) seedDraft = seedingBase();
  const dirty = JSON.stringify(seedDraft) !== JSON.stringify(seedingBase());
  $('#saveSeeding').disabled = !dirty;
  const touch = () => { renderTrackers(); scheduleSeedSim(); };

  $('#seedingGlobal').replaceChildren(
    el('label', {}, t('Un torrent a payé quand '),
      select(['either', 'both', 'ignore'],
        [t("le ratio OU l'ancienneté est atteint"), t('les deux sont atteints'),
         t('jamais retenu (déconseillé)')],
        seedDraft.mode, v => { seedDraft.mode = v; touch(); })),
    el('label', {}, t('ratio ≥ '),
      el('input', { type: 'number', min: 0, step: 0.1, value: seedDraft.min_ratio,
        oninput: e => { seedDraft.min_ratio = +e.target.value; $('#saveSeeding').disabled = false; scheduleSeedSim(); } })),
    el('label', {}, t('ou '),
      el('input', { type: 'number', min: 0, value: seedDraft.min_seed_days,
        oninput: e => { seedDraft.min_seed_days = +e.target.value; $('#saveSeeding').disabled = false; scheduleSeedSim(); } }),
      t(' jours de partage')));

  $('#trackerRows').replaceChildren(...S.trackers.map(tk => {
    const own = seedDraft.par_tracker[tk.groupe];
    return el('tr', {},
      el('td', { class: 't' }, tk.groupe,
        el('div', { class: 'meta', text: tk.hôtes.join(' · ') })),
      el('td', { class: 'r num', text: nf.format(tk.torrents) }),
      el('td', { class: 'r num', text: go(tk.octets) }),
      el('td', { class: 'r num', title: t('{n} élément(s)', { n: tk.éléments_retenus }),
        text: tk.octets_retenus ? go(tk.octets_retenus) : '—' }),
      el('td', { class: 'r hide-s num', text: tk.attente_max_jours ? dayCount(tk.attente_max_jours) : '—' }),
      el('td', {}, own
        ? el('span', { class: 'fields' },
            el('input', { type: 'number', min: 0, step: 0.1, value: own.min_ratio,
              style: 'width:66px',
              oninput: e => { own.min_ratio = +e.target.value; $('#saveSeeding').disabled = false; scheduleSeedSim(); } }),
            el('input', { type: 'number', min: 0, value: own.min_seed_days,
              style: 'width:66px',
              oninput: e => { own.min_seed_days = +e.target.value; $('#saveSeeding').disabled = false; scheduleSeedSim(); } }),
            el('button', { class: 'link', text: t('défaut'),
              onclick: () => { delete seedDraft.par_tracker[tk.groupe]; touch(); } }))
        : el('button', { class: 'link', text: t('définir une règle'),
            onclick: () => {
              seedDraft.par_tracker[tk.groupe] =
                { min_ratio: seedDraft.min_ratio, min_seed_days: seedDraft.min_seed_days };
              touch();
            } })));
  }));
}

/** Simulate the policy being edited, without writing anything. */
function scheduleSeedSim() {
  clearTimeout(seedSimTimer);
  seedSimTimer = setTimeout(async () => {
    try {
      const data = await api('/api/seeding/preview',
        { method: 'POST', body: JSON.stringify(seedDraft) });
      const sim = data.simulation;
      const nowDelta = sim.libérable_maintenant - (S.totaux.libérable_maintenant
        + S.orphelins_totaux.prêts);
      const sign = nowDelta >= 0 ? '+' : '−';
      $('#seedingSim').textContent = t('Avec ces réglages : {now} libérables tout de suite ({delta}), {soon} sous 30 jours, {n} élément(s) débloqués par rapport à maintenant.', {
        now: go(sim.libérable_maintenant), delta: sign + go(Math.abs(nowDelta)),
        soon: go(sim.libérable_sous_30j), n: sim.débloqués });
    } catch (error) {
      $('#seedingSim').textContent = t('Simulation indisponible : ') + error.message;
    }
  }, 400);
}

async function saveSeeding() {
  try {
    S = await api('/api/seeding', { method: 'POST', body: JSON.stringify(seedDraft) });
    seedDraft = null;
    toast(t('Obligations de seed enregistrées, inventaire recalculé.'));
    renderAll();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
  }
}

/* ------------------------------------------------------------ automation */

function renderSchedule() {
  const sc = S.planification;
  const push = async patch => {
    try {
      await api('/api/schedule', { method: 'POST', body: JSON.stringify({ ...sc, ...patch }) });
      await load();
    } catch (error) { toast(t('Échec : ') + error.message, true); }
  };

  $('#schedule').replaceChildren(
    el('label', {},
      el('input', { type: 'checkbox', checked: sc.enabled,
        onchange: e => push({ enabled: e.target.checked }) }),
      t('Activer le passage automatique')),
    el('label', {}, t('toutes les '),
      el('input', { type: 'number', min: 15, step: 15, value: sc.interval_minutes,
        onchange: e => push({ interval_minutes: +e.target.value }) }), t(' minutes')),
    el('label', { title: t('Traite ce que tu as marqué à la main') },
      el('input', { type: 'checkbox', checked: sc.apply_watchlist,
        onchange: e => push({ apply_watchlist: e.target.checked }) }),
      t("Vider la file d'attente")),
    el('label', { title: t('Applique aussi les règles de rétention ci-dessus') },
      el('input', { type: 'checkbox', checked: sc.apply_rules,
        onchange: e => push({ apply_rules: e.target.checked }) }),
      t('Appliquer les règles')),
    el('button', { class: 'act', style: 'margin-left:auto',
      onclick: async e => {
        e.target.disabled = true;
        e.target.textContent = t('Passage…');
        try {
          const started = await api('/api/run-now', { method: 'POST', body: '{}' });
          const job = await followJob(started.job, j => {
            e.target.textContent = `${t('Passage…')} ${j.faits}`;
          });
          toast(t('Passage terminé : {n} élément(s), {size} libérés.', { n: job.faits, size: go(job.octets_libérés) }));
          await load();
        } catch (error) { toast(t('Échec : ') + error.message, true); }
        finally { e.target.disabled = false; e.target.textContent = t('Lancer un passage maintenant'); }
      } }, t('Lancer un passage maintenant')));

  renderRecentJobs();
  const ready = S.en_attente_prêts;
  const last = S.dernier_passage
    ? new Date(S.dernier_passage * 1000).toLocaleString(LOCALE)
    : t('jamais');
  $('#scheduleNote').textContent = sc.enabled
    ? t('Prochain passage dans moins de {min} min. Dernier passage : {last}. Prêts à partir : {media} média(s) et {batches} lot(s), {size}.',
      { min: sc.interval_minutes, last, media: ready.médias, batches: ready.lots, size: go(ready.octets) })
    : t('Désactivé — rien ne partira tout seul. {n} élément(s) de la file sont déjà éligibles ({size}).',
      { n: ready.médias + ready.lots, size: go(ready.octets) });
}

/** Recent background jobs: what the server has done since it started. */
function renderRecentJobs() {
  const jobs = S.derniers_travaux || [];
  const box = $('#recentJobs');
  if (!jobs.length) { box.replaceChildren(); return; }
  box.replaceChildren(
    el('div', { class: 'meta', style: 'margin-bottom:6px', text: t('Derniers travaux (depuis le démarrage du serveur)') }),
    ...jobs.map(j => el('div', { class: 'queue-row' },
      el('span', { class: 'pill ' + (j.erreur || j.échecs ? 'hold' : j.terminé ? 'ok' : 'none'),
        text: j.erreur ? t('erreur') : !j.terminé ? t('en cours') : j.échecs ? t('{n} échec(s)', { n: j.échecs }) : 'ok' }),
      el('span', {}, `${t(j.type)} — ${j.libellé}`,
        el('div', { class: 'meta', text: new Date(j.début * 1000).toLocaleString(LOCALE) + ` · ${j.durée} s` + (j.erreur ? ` · ${j.erreur}` : '') })),
      el('span', { class: 'g', text: j.total ? `${j.faits}/${j.total} · ${go(j.octets_libérés)}` : go(j.octets_libérés) }))));
}

function renderQueue() {
  const queue = S.file_attente;
  if (!queue.length) {
    $('#queueList').replaceChildren(
      el('div', { class: 'empty', style: 'padding:22px 0',
        text: t('File vide. Sélectionne des éléments puis « Mettre en file ».') }));
    return;
  }
  const byKey = new Map(S.médias.map(m => [m.key, m]));
  const orphanByKey = new Map(S.orphelins.map(g => [g.key, g]));
  const when = entry => {
    if (entry.kind === 'orphan') {
      const g = orphanByKey.get(entry.key);
      if (!g) return { cls: 'none', text: t('disparu') };
      return g.seed_ok ? { cls: 'ok', text: t('prêt') }
        : g.unblocks_in_days == null ? { cls: 'hold', text: t('ratio à atteindre') }
        : { cls: 'hold', text: t('dans {n} j', { n: g.unblocks_in_days }) };
    }
    const m = byKey.get(entry.key);
    if (!m) return { cls: 'none', text: t('disparu') };
    if (!m.blockers.length) return { cls: 'ok', text: t('prêt') };
    if (m.seed_blocked && m.unblocks_in_days != null) return { cls: 'hold', text: t('dans {n} j', { n: m.unblocks_in_days }) };
    return { cls: 'hold', text: m.blockers[0].split(' — ')[0] };
  };
  $('#queueList').replaceChildren(...queue.map(entry => el('div', { class: 'queue-row' },
    el('span', { class: 'pill ' + (entry.kind === 'orphan' ? 'none' : 'keep'),
      text: entry.kind === 'orphan' ? t('seed seul') : t('média') }),
    el('span', { text: entry.titre.slice(0, 54) }),
    (() => { const w = when(entry); return el('span', { class: 'pill ' + w.cls, text: w.text }); })(),
    el('span', { class: 'meta', text: entry.depuis ? t('en file depuis {n} j', { n: days(entry.depuis) }) : '' }),
    el('span', { class: 'g', text: go(entry.octets) }),
    el('button', { class: 'link', text: t('retirer'),
      onclick: async () => {
        await api('/api/watchlist', { method: 'POST',
          body: JSON.stringify({ action: 'remove', kind: entry.kind, clés: [entry.key] }) });
        await load();
      } }))));
}

/** Plex collection checkboxes; a change writes [protect]. */
function renderCollections() {
  const all = S.collections_plex || {};
  const protectedSet = new Set(S.protection.collections || []);
  const names = Object.keys(all).sort((a, b) => a.localeCompare(b, LANG));
  if (!names.length) {
    $('#collectionsList').replaceChildren(el('span', { class: 'meta', text: t('Aucune collection dans Plex.') }));
    return;
  }
  $('#collectionsList').replaceChildren(...names.map(name => el('label', {},
    el('input', { type: 'checkbox', checked: protectedSet.has(name),
      onchange: async e => {
        const next = new Set(protectedSet);
        e.target.checked ? next.add(name) : next.delete(name);
        try {
          S = await api('/api/protect-collections', { method: 'POST',
            body: JSON.stringify({ collections: [...next] }) });
          toast(t('Collections protégées : {n}.', { n: next.size }));
          renderAll();
        } catch (error) { toast(t('Échec : ') + error.message, true); }
      } }),
    `${name} `, el('span', { class: 'meta', text: `(${all[name]})` }))));
}

function select(values, labels, current, onChange) {
  return el('select', { onchange: e => onChange(e.target.value) },
    ...values.map((v, i) => el('option', { value: v, selected: v === current }, labels[i])));
}

async function saveRules() {
  try {
    S = await api('/api/rules', { method: 'POST', body: JSON.stringify({ règles: ruleDraft }) });
    ruleDraft = null;
    toast(t('Règles enregistrées et inventaire recalculé.'));
    renderAll();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
  }
}

/* ----------------------------------------------------------------- history */

async function renderHistory() {
  const records = await api('/api/history');
  $('#noHist').hidden = records.length > 0;
  const freed = records.reduce((a, r) => a + (r.octets_libérés || 0), 0);
  const zero = records.filter(r => !r.erreurs.length && !r.octets_libérés).length;
  $('#histSummary').textContent = t('{n} suppression(s), {size} libérés', { n: records.length, size: go(freed) })
    + (zero ? t(' — dont {n} sans aucun gain (le torrent gardait les fichiers).', { n: zero }) : '.');

  $('#histRows').replaceChildren(...records.map(r => el('tr', {},
    el('td', { class: 'num', text: new Date(r.horodatage * 1000).toLocaleString(LOCALE) }),
    el('td', { class: 't' }, r.titre,
      el('div', { class: 'meta',
        text: t('règle « {name} »', { name: r.règle })
          + (r.torrents?.length ? ' · ' + t('{n} torrent(s) retiré(s)', { n: r.torrents.length }) : '')
          + (r.saison != null ? ' · ' + t('saison {n}', { n: r.saison }) : '') })),
    el('td', { class: 'r num', text: r.octets_libérés ? go(r.octets_libérés) : '—' }),
    el('td', { class: 'meta hide-s' }, (r.erreurs.length ? r.erreurs : r.étapes).join(' · ')),
    el('td', {}, r.identifiant_externe && r.type !== 'seed-seul'
      ? el('button', { class: 'link', text: t('re-demander'),
          onclick: e => restore(r, e.target) })
      : el('span', { class: 'meta', text: '—' })))));
}

/** Bring back a deleted title. Radarr/Sonarr start a new search. */
async function restore(record, button) {
  const question = record.type === 'movie'
    ? t('Réintroduire le film « {title} » ?', { title: record.titre })
    : t('Réintroduire la série « {title} » ?', { title: record.titre });
  if (!confirm(question
    + '\n\n' + t('Il sera rajouté à {app} avec le profil et le dossier majoritaires, et une recherche partira aussitôt — donc un téléchargement.',
      { app: record.type === 'movie' ? 'Radarr' : 'Sonarr' }))) return;
  button.disabled = true;
  button.textContent = t('ajout…');
  try {
    const data = await api('/api/restore', {
      method: 'POST', body: JSON.stringify({ id: record.id, horodatage: record.horodatage, titre: record.titre }),
    });
    toast(t('« {title} » réintroduit, recherche lancée.', { title: data.titre }));
    await renderHistory();
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
    button.disabled = false;
    button.textContent = t('re-demander');
  }
}

/* ------------------------------------------------------------------ cycle */

/** The last refresh failed: the figures shown are stale. */
function renderErrorBanner() {
  const box = $('#errorBanner');
  if (!S.erreur) { box.hidden = true; return; }
  box.hidden = false;
  const when = S.calculé_à ? new Date(S.calculé_à * 1000).toLocaleString(LOCALE) : t('jamais');
  box.replaceChildren(
    el('strong', {}, t('⚠ Le dernier rafraîchissement a échoué. ')),
    t('Les chiffres affichés datent du {when}. Cause : ', { when }),
    el('code', { text: S.erreur }),
    el('div', { class: 'meta', style: 'margin-top:6px' },
      t('Vérifie le service en cause dans la carte Santé, puis « Actualiser ».')));
}

/* ------------------------------------------------------------ connections */

const CONN_HELP = {
  radarr: 'Radarr → Settings → General → Security → API Key',
  sonarr: 'Sonarr → Settings → General → Security → API Key',
  seerr: t('Seerr/Overseerr/Jellyseerr → Settings → General → API Key (facultatif)'),
  qbittorrent: t("Identifiants de l'interface web de qBittorrent (facultatif : sans torrent, désactive-le)"),
  plex: t('Fichier com.plexapp.plugins.library.db, lisible par sweeper (volume monté en lecture seule dans Docker)'),
};
const CONN_PLACEHOLDER = {
  radarr: 'http://127.0.0.1:7878', sonarr: 'http://127.0.0.1:8989',
  seerr: 'http://127.0.0.1:5055', qbittorrent: 'http://127.0.0.1:8080',
};

function renderConnections() {
  const list = S.connexions || [];
  const box = $('#connections');
  if (!list.length) { box.replaceChildren(el('div', { class: 'meta', text: t('Pas encore sondé.') })); return; }
  box.replaceChildren(...list.map(c => {
    const env = f => c.sources?.[f] === 'env';
    const input = (key, label, type, value, placeholder) => el('label', { class: 'f' }, label,
      el('input', { type, 'data-k': key, value: value ?? '', placeholder: placeholder || '',
        disabled: env(key), autocomplete: 'off', spellcheck: 'false',
        title: env(key) ? t("Défini par variable d'environnement") : '' }));
    const fields = [];
    if (c.service === 'plex') {
      fields.push(input('database', t('Base de données Plex'), 'text', c.base, '…/Plug-in Support/Databases/com.plexapp.plugins.library.db'));
    } else {
      fields.push(input('url', 'URL', 'text', c.url, CONN_PLACEHOLDER[c.service]));
      if (c.service === 'qbittorrent') {
        fields.push(input('username', t('Utilisateur'), 'text', c.utilisateur, 'admin'));
        fields.push(input('password', t('Mot de passe'), 'password', '', c.mot_de_passe ? t('enregistré ({mask})', { mask: c.mot_de_passe }) : ''));
      } else {
        fields.push(input('api_key', t("Clé d'API"), 'password', '',
          c.clé ? t('enregistrée ({mask})', { mask: c.clé }) : c.sources?.api_key === 'auto' && c.ok ? t('découverte automatiquement') : t("clé d'API")));
      }
    }
    const res = el('span', { class: 'res' });
    const node = el('div', { class: 'conn' },
      el('div', { class: 'top' },
        el('b', { text: c.nom }),
        el('span', { class: 'pill ' + (!c.activé ? 'none' : c.ok ? 'ok' : 'hold'),
          text: !c.activé ? t('désactivé') : c.ok ? t('connecté · {detail}', { detail: c.détail }) : t('injoignable') }),
        c.correspondances ? el('span', { class: 'meta', text: t('{n} correspondance(s) de chemins', { n: c.correspondances }) }) : null,
        c.service !== 'plex' ? el('label', { class: 'meta', style: 'margin-left:auto;display:flex;gap:6px;align-items:center' },
          el('input', { type: 'checkbox', 'data-k': 'enabled', checked: c.activé, disabled: env('enabled') }), t('activé')) : null),
      el('div', { class: 'grid2' }, ...fields),
      el('div', { class: 'meta', style: 'margin-top:6px', text: CONN_HELP[c.service] }),
      !c.ok && c.activé ? el('div', { class: 'why', text: c.détail }) : null,
      el('div', { class: 'btns' },
        el('button', { class: 'act', text: t('Tester'), onclick: () => saveConnection(node, c.service, false, res) }),
        el('button', { class: 'act primary', text: t('Enregistrer'), onclick: () => saveConnection(node, c.service, true, res) }),
        res));
    return node;
  }));
}

async function saveConnection(node, service, save, res) {
  const body = { service };
  node.querySelectorAll('input[data-k]').forEach(i => {
    if (i.disabled) return;
    body[i.dataset.k] = i.type === 'checkbox' ? i.checked : i.value;
  });
  res.textContent = save ? t('Test puis enregistrement…') : t('Test…');
  try {
    const data = await api(save ? '/api/connections' : '/api/connections/test', { method: 'POST', body: JSON.stringify(body) });
    if (!save) {
      res.textContent = data.test.ok ? `✓ ${data.test.détail}` : `✗ ${data.test.détail}`;
      return;
    }
    S = data;
    renderAll();
    toast(t('{service} enregistré.', { service }) + (S.configuré ? '' : ' ' + t('Il reste des services à configurer.')));
  } catch (error) {
    res.textContent = '✗ ' + error.message;
  }
}

/** First run: nothing could be computed yet. Lead to Connections. */
function renderSetupBanner() {
  const box = $('#setupBanner');
  if (S.configuré) { box.hidden = true; return; }
  const broken = (S.connexions || []).filter(c => c.activé && !c.ok && c.service !== 'seerr').map(c => c.nom);
  box.hidden = false;
  box.replaceChildren(
    el('strong', {}, t('Bienvenue. ')),
    broken.length ? t("sweeper n'arrive pas encore à joindre : {names}. ", { names: broken.join(', ') }) : t('Configuration en cours. '),
    el('button', { class: 'link', text: t('Ouvrir les connexions'),
      onclick: () => { tab('plus'); $('#connDetails').open = true; } }));
}

function renderAll() {
  renderSetupBanner();
  renderConnections();
  renderErrorBanner();
  // as long as nothing could be computed, there is nothing else to draw
  if (!S.configuré) {
    $('#stamp').textContent = t('non configuré');
    return;
  }
  renderDash();
  renderLibrary();
  renderOrphans();
  renderTrackers();
  renderRequests();
  renderRules();
  renderSchedule();
  renderQueue();
  renderQueueBadge();
  schedulePreview();
}

/** Badge on the Rules tab: the queue lives there. */
function renderQueueBadge() {
  const tab = $$('nav button').find(b => b.dataset.tab === 'rules');
  if (!tab) return;
  tab.textContent = t('Règles');
  if (S.file_attente.length) {
    tab.append(el('span', { class: 'badge', text: String(S.file_attente.length) }));
  }
}

async function load() {
  try {
    S = await api('/api/state');
    ruleDraft = null;
    renderAll();
    if (!S.configuré && !load.opened) {
      load.opened = true;
      tab('plus');
      $('#connDetails').open = true;
    }
  } catch (error) {
    toast(t("Impossible de charger l'état : ") + error.message, true);
  }
}

function tab(name) {
  $$('nav button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === name));
  ['dash', 'library', 'rules', 'plus'].forEach(id => { $('#' + id).hidden = id !== name; });
  if (name === 'plus') { renderHistory(); scheduleSeedSim(); }
  // one action bar at a time, the open tab's
  renderSelection();
  renderOrphanSelection();
}

/* ---------------------------------------------------------------- wiring */

$$('nav button').forEach(b => b.onclick = () => tab(b.dataset.tab));
$('#refresh').onclick = async e => {
  e.target.disabled = true;
  e.target.textContent = t('Calcul…');
  try { S = await api('/api/refresh', { method: 'POST' }); ruleDraft = null; renderAll(); }
  catch (error) { toast(t('Échec : ') + error.message, true); }
  finally { e.target.disabled = false; e.target.textContent = t('Actualiser'); }
};
$('#lang').value = LANG;
$('#lang').onchange = async e => {
  e.target.disabled = true;
  try {
    await api('/api/language', { method: 'POST', body: JSON.stringify({ langue: e.target.value }) });
    location.reload();                 // the page is served in the new language
  } catch (error) {
    toast(t('Échec : ') + error.message, true);
    e.target.value = LANG;
    e.target.disabled = false;
  }
};
$('#q').oninput = renderLibrary;
$('#who').onchange = e => { whoFilter = e.target.value; renderLibrary(); };
$('#view').onchange = e => { viewFilter = e.target.value; renderLibrary(); };
$('#viewMedia').onclick = () => setLibraryView('media');
$('#viewOrphans').onclick = () => setLibraryView('orphans');
$('#whoChip').onclick = () => { whoFilter = ''; renderLibrary(); };
$$('thead th[data-sort]').forEach(th => th.onclick = () => {
  const key = th.dataset.sort;
  sortDir = sortKey === key ? -sortDir : -1;
  sortKey = key;
  renderLibrary();
});
$('#all').onchange = e => {
  const rows = visible();
  rows.forEach(r => e.target.checked ? selection.add(r.key) : selection.delete(r.key));
  renderLibrary();
};
$$('#orphans .chip').forEach(chip => chip.onclick = () => {
  const f = chip.dataset.o;
  oFilters.has(f) ? oFilters.delete(f) : oFilters.add(f);
  chip.setAttribute('aria-pressed', oFilters.has(f));
  renderOrphans();
});
$$('thead th[data-osort]').forEach(th => th.onclick = () => {
  const key = th.dataset.osort;
  oSortDir = oSortKey === key ? -oSortDir : -1;
  oSortKey = key;
  renderOrphans();
});
$('#allOrphans').onchange = e => {
  visibleOrphans().forEach(g =>
    e.target.checked ? oSelection.add(g.key) : oSelection.delete(g.key));
  renderOrphans();
};
$('#odeleteBtn').onclick = deleteOrphans;
$('#deleteBtn').onclick = openConfirm;
$('#queueBtn').onclick = () => queueUp([...selection], 'media');
$('#adoptBtn').onclick = openAdopt;
$('#aCancel').onclick = () => $('#adoptDialog').close();
$('#oqueueBtn').onclick = () => queueUp([...oSelection], 'orphan');
$('#protectBtn').onclick = () => setProtection(true);
$('#unprotectBtn').onclick = () => setProtection(false);
$('#cCancel').onclick = () => $('#confirm').close();
$('#cGo').onclick = doDelete;
$('#addRule').onclick = () => {
  ruleDraft.push({
    name: t('nouvelle règle'), media: 'movie', enabled: false,
    never_watched: true, unwatched_days: null, min_age_days: 45, max_distinct_viewers: null,
  });
  renderRules();
};
$('#saveRules').onclick = saveRules;
$('#saveSeeding').onclick = saveSeeding;

translatePage();
load();
