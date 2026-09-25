/* Runs app.js against a real API payload, in a real DOM (jsdom).
 *
 * A hand-rolled fake DOM once let a missing element through (the blank page):
 * an unknown selector returned a node instead of null. jsdom behaves like a
 * browser for everything but layout: any JavaScript error during rendering is
 * a failure, the initial hidden attribute is honoured, and innerHTML goes
 * through a real HTML parser — which makes the injection test real.
 *
 * With --lang en the page is rendered in English, and any French string from
 * the catalogue left on the page is a failure.
 *
 * Usage: node test_webui.mjs <state.json> [--lang en] [--dump-svg <out.svg>]
 */
import { readFileSync, writeFileSync } from 'node:fs';

let JSDOM;
try { ({ JSDOM } = await import('jsdom')); }
catch { console.error('jsdom manquant : lance `npm install` une fois dans ce dossier.'); process.exit(2); }

const args = process.argv.slice(2);
const stateFile = args.find((a) => !a.startsWith('--'));
const dumpSvg = args.includes('--dump-svg') ? args[args.indexOf('--dump-svg') + 1] : null;
const lang = args.includes('--lang') ? args[args.indexOf('--lang') + 1] : 'fr';
const state = JSON.parse(readFileSync(stateFile, 'utf8'));
const html = readFileSync(new URL('./webui/index.html', import.meta.url), 'utf8')
  .replace('<html lang="fr"', `<html lang="${lang}"`);       // what the server does
const i18n = readFileSync(new URL('./webui/i18n.js', import.meta.url), 'utf8');
const app = readFileSync(new URL('./webui/app.js', import.meta.url), 'utf8');
// one evaluation: separate evals would not share their top-level const bindings
const source = i18n + '\n' + app;
const EN = JSON.parse(i18n.match(/const I18N_EN = (\{[\s\S]*?\n\});/)[1]);
const tr = (fr) => (lang === 'en' && EN[fr]) || fr;           // expected text in this language

const css = readFileSync(new URL('./webui/style.css', import.meta.url), 'utf8');
const dom = new JSDOM(html.replace('<link rel="stylesheet" href="/assets/style.css">', `<style>${css}</style>`),
  { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
const { document } = window;

// --- what the browser would provide ------------------------------------------
const calls = [];
window.fetch = async (url) => {
  calls.push(url);
  let data = state;
  if (url.includes('/api/history')) data = [];
  else if (url.includes('/api/rules/preview')) {
    data = { aperçu: state.règles.map((r) => ({ nom: r.name, activée: r.enabled, éléments: 3, octets: 12e9, bloqués: 1, exemples: ['a', 'b'] })) };
  } else if (url.includes('/api/seeding')) data = { simulation: { libérable_maintenant: 3e11, libérable_sous_30j: 8e11, débloqués: 4 } };
  else if (url.includes('/api/adopt/preview')) data = { aperçu: [] };
  return { ok: true, json: async () => data };
};
window.structuredClone = structuredClone;
const jsErrors = [];
window.addEventListener('error', (e) => jsErrors.push(e.error?.stack || e.message));
window.addEventListener('unhandledrejection', (e) => jsErrors.push(String(e.reason?.stack || e.reason)));

let failures = 0;
function check(label, condition, detail = '') {
  console.log(`  ${condition ? 'OK  ' : 'ÉCHEC'} ${label}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures++;
}
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const go = (bytes) => (bytes / 1e9).toFixed(0) + ' Go';
const pct = (el) => parseFloat(el.style.width) || 0;

// --- run ------------------------------------------------------------------------
try { window.eval(source); }
catch (e) { jsErrors.push(e.stack || String(e)); }
// Previews are debounced (350–400 ms after rendering): waiting a fixed time
// is a bet on the machine's speed. Wait for the condition itself.
async function waitFor(predicate, timeout = 4000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (predicate()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return predicate();
}
await waitFor(() => document.querySelector('#stamp')?.textContent.startsWith(tr('calculé {time}').split(' ')[0]));
await waitFor(() => document.querySelector('#previewNote')?.textContent.length > 0
  || document.querySelector('.toast.err'));

console.log(`Rendu de app.js dans jsdom contre ${stateFile.split('/').pop()} (${lang}) :`);

// --- in English, no catalogued French string may remain on the page ----------------
function frenchLeft() {
  const texts = [];
  const walker = document.createTreeWalker(document.body, window.NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) texts.push(walker.currentNode.nodeValue.replace(/\s+/g, ' ').trim());
  for (const node of document.querySelectorAll('[title],[placeholder],[aria-label]')) {
    for (const a of ['title', 'placeholder', 'aria-label']) if (node.getAttribute(a)) texts.push(node.getAttribute(a).trim());
  }
  const whole = document.body.textContent;
  const exact = texts.filter((x) => EN[x] && EN[x] !== x);
  const inside = Object.keys(EN).filter((k) => k.length >= 16 && !k.includes('{') && EN[k] !== k && whole.includes(k));
  return [...new Set([...exact, ...inside])];
}
if (lang === 'en') {
  const left = frenchLeft();
  check('en anglais : aucune chaîne française du catalogue sur la page', left.length === 0, left.slice(0, 3).join(' | '));
  check('en anglais : les dates et tailles suivent la langue', !/\d,\d (Go|To)/.test(document.body.textContent));
}
if (state.configuré === false) {
  check('non configuré : aucune erreur JavaScript', jsErrors.length === 0, jsErrors[0]?.split('\n')[0]);
  check('non configuré : aucun toast d\'erreur', document.querySelectorAll('.toast.err').length === 0);
  check('non configuré : bandeau d\'accueil visible', document.querySelector('#setupBanner').hidden === false);
  check('non configuré : l\'onglet Plus est ouvert sur Connexions',
    document.querySelector('#plus').hidden === false && document.querySelector('#connDetails').open === true);
  const cards = document.querySelectorAll('#connections .conn');
  check('non configuré : une carte par service (5)', cards.length === 5, `${cards.length}`);
  check('les clés ne sont jamais réaffichées (champs vides)',
    [...document.querySelectorAll('#connections input[data-k="api_key"], #connections input[data-k="password"]')].every((i) => i.value === ''));
  console.log(failures ? `\n${failures} échec(s)` : '\nTout passe.');
  process.exit(failures ? 1 : 0);
}
check('aucune erreur JavaScript pendant le rendu', jsErrors.length === 0, jsErrors[0]?.split('\n')[0]);
// app.js catches its own rendering errors and shows them in a toast: an error
// toast is therefore a rendering error too, just swallowed.
const errorToasts = [...document.querySelectorAll('.toast.err')].map((t) => t.textContent);
check('aucun toast d\'erreur (erreur de rendu avalée par load())', errorToasts.length === 0, errorToasts[0]?.slice(0, 90));
check('load() a appelé /api/state', calls.some((u) => u.includes('/api/state')));

// --- app.js ↔ index.html consistency ------------------------------------------------
const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
const usedIds = new Set([...source.matchAll(/\$\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)/g)].map((m) => m[1]));
const unknown = [...usedIds].filter((id) => !htmlIds.has(id));
check('aucun sélecteur #id inconnu de index.html', unknown.length === 0, unknown.join(', ') || `${htmlIds.size} ids connus`);

// --- dashboard ----------------------------------------------------------------------
check('3 tuiles de KPI, pas plus', $('#kpis').children.length === 3, `${$('#kpis').children.length}`);
check('4 onglets, pas plus', $$('nav button').length === 4, $$('nav button').map((b) => b.textContent).join(' · '));
const segs = [...$('#stack').children];
check('barre empilée : 4 segments + libre', segs.length === 5, `${segs.length}`);
const total = segs.reduce((a, s) => a + pct(s), 0);
check('les segments totalisent 100 %', Math.abs(total - 100) < 0.5, `${total.toFixed(1)} %`);
check('légende présente pour chaque segment', $('#stackLegend').children.length === 5);
check('histogramme : une colonne par mois', $('#growth').children.length === state.croissance.length);
check('un bloc par palier', $('#tiers').children.length === state.paliers.length);
if (state.paliers.length) {
  check('paliers configurés : carte visible et conseil rédigé', $('#tiersCard').hidden === false
    && $('#tierHint').textContent.length > 30, '« ' + $('#tierHint').textContent.slice(0, 64) + '… »');
} else {
  // no [[tier]]: a plain disk view, no pricing anywhere
  check('sans palier : carte des paliers masquée, aucun repère sur la barre',
    $('#tiersCard').hidden === true && $('#ticks').children.length === 0);
  check('sans palier : ni prix ni palier dans le tableau de bord',
    !/palier|tier\b|€|\$/i.test([...$('#dash').querySelectorAll('*')].filter((n) => !n.closest('[hidden]'))
      .map((n) => n.childNodes).flatMap((c) => [...c]).filter((c) => c.nodeType === 3).map((c) => c.nodeValue).join(' ')));
}
check('prévision de saturation rédigée', $('#fullHint').textContent.length > 20, '« ' + $('#fullHint').textContent.slice(0, 70) + '… »');
check('horodatage affiché', $('#stamp').textContent.startsWith(tr('calculé {time}').split(' ')[0]));
const errBox = $('#errorBanner');
if (state.erreur) {
  check('bandeau d\'erreur visible avec la cause', errBox.hidden === false && errBox.textContent.includes(state.erreur));
} else {
  check('bandeau d\'erreur masqué quand tout va bien', errBox.hidden === true);
}
const gap = state.couverture_hardlinks;
check(gap?.torrents ? 'bandeau de couverture visible quand un trou existe' : 'bandeau de couverture masqué quand tout est couvert',
  gap?.torrents ? $('#coverageWarning').hidden === false && $('#coverageWarning').textContent.length > 20 : $('#coverageWarning').hidden === true);

// --- health -------------------------------------------------------------------------
const health = state.santé || {};
check('bloc santé : 4 services', (health.services || []).length === 4, (health.services || []).map((x) => `${x.service}:${x.ok ? 'ok' : 'KO'}`).join(' '));
check('carte Santé rendue (sous Plus)', $('#health').children.length >= 2 && $('#plus').contains($('#health')));
check('derniers travaux exposés', Array.isArray(state.derniers_travaux), `${(state.derniers_travaux || []).length}`);

// --- usage chart --------------------------------------------------------------------
const chart = $('#usageChart');
if (state.occupation.relevés.length < 2) {
  check('courbe : message honnête avec un seul relevé', $('#usageNote').textContent.includes(tr('Premier relevé pris aujourd\'hui : {used} occupés. Reviens demain pour voir la pente.').split('{used}')[0]) || $('#usageNote').textContent === '');
} else {
  const svg = chart.querySelector('svg');
  check('courbe : un SVG est dessiné', !!svg);
  const [VW, VH] = (svg.getAttribute('viewBox') || '0 0 600 170').split(' ').slice(2).map(Number);
  const pts = (svg.querySelector('polyline')?.getAttribute('points') || '').trim().split(/\s+/).map((p) => p.split(',').map(Number));
  check('courbe : coordonnées valides dans le cadre', pts.length >= 2 && pts.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y) && x >= 0 && x <= VW && y >= 0 && y <= VH), `${pts.length} points dans ${VW}×${VH}`);
  check('courbe : échelle 1:1, pas d\'étirement', !svg.hasAttribute('preserveAspectRatio') || svg.getAttribute('preserveAspectRatio') !== 'none');
  const ruleLabels = [...svg.querySelectorAll('text.rule-label')];
  const ys = ruleLabels.map((t) => t.getAttribute('y'));
  check('courbe : repères de palier, aucun superposé', ruleLabels.length >= 1 && new Set(ys).size === ys.length, `${ruleLabels.length} repères`);
  check('courbe : pente annoncée', $('#usageNote').textContent.length > 20, '« ' + $('#usageNote').textContent.slice(0, 58) + '… »');
  const hostile = state.paliers.find((t) => /[<>&"]/.test(t.nom));
  if (hostile) {
    // real HTML parser: the hostile name must stay text, never become an element
    check('courbe : nom de palier hostile rendu comme texte, pas comme balise',
      ruleLabels.some((t) => t.textContent.includes('<b>')) && chart.querySelector('b') === null);
  }
  if (dumpSvg) {
    const colors = { '--series-1': '#2a78d6', '--border-strong': '#c6c2b8', '--text-3': '#86847d', '--text-1': '#0b0b0b', '--surface-1': '#fcfcfb' };
    let out = svg.outerHTML.replace(/var\((--[a-z0-9-]+)\)/g, (_, v) => colors[v] || '#000');
    out = out.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" ').replace('>', `><rect width="100%" height="100%" fill="#fcfcfb"/>`);
    writeFileSync(dumpSvg, out);
    console.log(`  (SVG écrit dans ${dumpSvg})`);
  }
}

// --- library ------------------------------------------------------------------------
const listed = state.médias.filter((m) => !(m.kind === 'season' && !m.eligible));
const rows = $$('#rows > tr');
check('tableau médiathèque rempli', rows.length === listed.length, `${rows.length} lignes pour ${listed.length} listables`);
check('aucune saison repliée listée au premier niveau', !rows.some((tr) => tr.classList.contains('season-row')));
const togglers = $$('#rows td.t button').filter((b) => b.textContent.includes(tr('{n} saisons').replace('{n} ', '')));
const multiSeason = state.médias.filter((m) => m.kind === 'series' && !m.unmanaged
  && state.médias.filter((x) => x.kind === 'season' && x.key.startsWith(`season:${m.key.split(':')[1]}:`)).length > 1);
check('les séries à plusieurs saisons ont un bouton de dépliage', togglers.length === multiSeason.length, `${togglers.length}/${multiSeason.length}`);
check('un seul sélecteur de filtre, 9 vues', $('#view').options.length === 9);
check('le sélecteur de demandeur reste caché tant qu\'aucun n\'est choisi', $('#who').hidden === true && $('#whoChip').hidden === true);
check('6 colonnes dans la médiathèque', $$('#mediaBlock thead th').length === 6);
check('la vue Seed seul est repliée par défaut', $('#orphans').hidden === true && $('#mediaBlock').hidden === false);
const resuming = state.médias.filter((m) => m.in_progress > 0);
check('aucun titre en cours de visionnage n\'est candidat', resuming.every((m) => !m.actionable || !state.protection.in_progress), `${resuming.length} en cours`);
const unmanaged = state.médias.filter((m) => m.unmanaged);
check('éléments hors *arr exposés et jamais supprimables directement', unmanaged.length > 0 && unmanaged.every((m) => !m.actionable), `${unmanaged.length}, ${go(unmanaged.reduce((a, m) => a + m.library_bytes, 0))}`);
check('bouton Adopter et dialogue présents', !!$('#adoptBtn') && !!$('#adoptDialog'));

// --- seed-only -----------------------------------------------------------------------
check('lignes hors bibliothèque', $$('#orphanRows > tr').length === state.orphelins.length, `${state.orphelins.length} lots`);
check('résumé du seed seul en une phrase', $('#orphanKpis').textContent.length > 40);

// --- trackers -----------------------------------------------------------------------
check('une ligne par tracker', $$('#trackerRows > tr').length === state.trackers.length, `${state.trackers.length}`);
check('règle par défaut rendue', $('#seedingGlobal').children.length === 3);
check('hôtes listés pour chaque tracker', state.trackers.every((t) => t.hôtes.length >= 1));

// --- requests -----------------------------------------------------------------------
check('une ligne par demandeur', $$('#reqRows > tr').length === state.demandes.demandeurs.length);
check('3 tuiles de demandes', $('#reqKpis').children.length === 3);
const lanes = $$('#reqBars .lane').map((l) => [...l.children].reduce((a, s) => a + pct(s), 0));
check('barres des demandeurs : la plus grosse atteint 100 %, aucune ne déborde', lanes.length > 0 && Math.abs(Math.max(...lanes) - 100) < 0.5 && lanes.every((w) => w <= 100.01), `max ${Math.max(...lanes).toFixed(1)} %`);
check('note de demandes rédigée', $('#reqNote').textContent.length > 20);

// --- rules & automation --------------------------------------------------------------
const cards = $$('#ruleList > .rule');
check('une carte par règle', cards.length === state.règles.length);
check('chaque règle porte son rang', cards.map((c) => c.querySelector('.rank')?.textContent).join(',') === state.règles.map((_, i) => String(i + 1)).join(','));
check('la 1re ne peut pas monter, la dernière ne peut pas descendre',
  cards[0]?.querySelector('.movers button')?.disabled === true && cards.at(-1)?.querySelectorAll('.movers button')[1]?.disabled === true);
check('poignée de glisser-déposer', cards.every((c) => !!c.querySelector('.grip')));
check('conséquence de l\'ordre expliquée', $('#orderNote').textContent.length > 20);
check('note d\'aperçu rédigée', $('#previewNote').textContent.length > 20, '« ' + $('#previewNote').textContent.slice(0, 60) + '… »');
check('chaque règle affiche son bilan', cards.every((c) => c.textContent.includes(tr('jamais exécutée'))
  || c.textContent.includes(tr('{n} supprimé(s) · {size} · dernier {date}').split(' · ')[0].replace('{n} ', ''))));
check('panneau de planification : 5 contrôles', $('#schedule').children.length === 5);
check('note de planification rédigée', $('#scheduleNote').textContent.length > 20);
check('liste de file rendue', $('#queueList').children.length >= 1);
check('garde-fous mentionnent la protection des visionnages', lang === 'en'
  ? /Continue Watching|NOT spared/.test($('#guards').textContent)
  : /Continuer à regarder|PAS épargnés/.test($('#guards').textContent));
const collNames = Object.keys(state.collections_plex || {});
check('collections Plex listées avec une case chacune', $('#collectionsList').children.length === (collNames.length || 1), `${collNames.length}`);
const protectedNames = new Set(state.protection.collections || []);
check('un membre de collection protégée n\'est jamais candidat', state.médias.every((m) => !m.actionable || !(m.collections || []).some((c) => protectedNames.has(c))));

// --- connections (configured state) --------------------------------------------------
check('Connexions : une carte par service', document.querySelectorAll('#connections .conn').length === (state.connexions || []).length,
  `${(state.connexions || []).length}`);
check('Connexions : aucun secret en clair dans la page',
  !JSON.stringify(state.connexions || []).match(/"(clé|mot_de_passe)":"(?!••••)[^"]/));
check('bandeau d\'accueil masqué une fois configuré', document.querySelector('#setupBanner').hidden === true);

// --- action bars: hidden at rest, and `hidden` must really hide -------------------
const display = (sel) => window.getComputedStyle($(sel)).display;
check('les deux barres d\'action sont cachées au chargement',
  $('#actionbar').hidden && $('#orphanbar').hidden);
check('un élément hidden est bien display:none malgré sa règle display:flex',
  display('#orphanbar') === 'none', `display=${display('#orphanbar')}`);

// --- selecting an item outside *arr: Adopt offered, Delete greyed out -------------
const unmanagedRow = $$('#rows > tr').find((row) => row.textContent.includes(tr('aucun *arr ne le gère')));
if (unmanagedRow) {
  // like the user: open the tab before ticking
  $$('nav button').find((b) => b.dataset.tab === 'library').click();
  check('l\'onglet Médiathèque s\'ouvre', $('#library').hidden === false && $('#dash').hidden === true);
  unmanagedRow.querySelector('input[type=checkbox]').click();
  check('cocher un hors *arr affiche la barre avec « Adopter… »',
    $('#actionbar').hidden === false && $('#adoptBtn').hidden === false);
  check('… et grise « Supprimer » puisque rien n\'est supprimable', $('#deleteBtn').disabled === true,
    $('#deleteBtn').title.slice(0, 50));
  check('la barre Seed seul reste cachée pendant ce temps', $('#orphanbar').hidden === true);
  unmanagedRow.querySelector('input[type=checkbox]').click();
  check('décocher referme la barre', $('#actionbar').hidden === true);
}

console.log(failures ? `\n${failures} échec(s)` : '\nTout passe.');
process.exit(failures ? 1 : 0);
