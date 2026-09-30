'use strict';

// ─────────────────────────── state ───────────────────────────
const S = {
  screen: 'login',
  // The class is already on <body> (the inline script in index.html sets it before first paint);
  // here it is only read back as state so the Settings selector knows which one is active.
  theme: document.body.classList.contains('theme-summer') ? 'summer' : 'classic',
  os: { text: '', isWindows11: false },
  fiddler: { installed: false, certTrusted: false },
  // extractWorkers must start as a NUMBER: setModel only parses input when the current value is one,
  // so seeding it '' would store the raw string. The backend now parses a numeric string instead of
  // throwing on it (Backend.NumOr), but seed numbers anyway — that is what keeps setModel coercing.
  settings: { serverHost: '', serverPort: 21000, source: 'cdn', fiddlerDecrypt: true, installCert: true, createShortcut: true, fiddlerNoUac: true, gentleExtract: true, extractWorkers: 0, installRoot: '', language: 'en', enhancements: true },
  // Which profile the registry/LocalLow currently hold. null until Settings asks the backend.
  profile: null,
  versions: [], installed: [], selectedVersionId: null, serverConfigured: false, runAtStartup: false,

  installStep: 0,
  // voices: the wizard's voice-language ticks {lang: bool}, kept by language across versions (the
  // names are the same on both) and seeded lazily — see selectedVoicePacks.
  wiz: { versionId: null, source: 'cdn', localPath: '', decrypt: true, cert: true, shortcut: true, shortcutName: '', voices: {} },
  // Local folder of the install in progress — snapshot at start (wiz is mutable), needed by Resume.
  activeInstallLocal: null,
  // The languages an "add voice pack" job (install.addVoices) is adding — null for a full install. It
  // picks the overlay's words (title, the done box without login details) and the toasts.
  activeInstallVoices: null,
  // The very rpc that started the running install job ({type, payload}), replayed by Resume: the
  // wizard's ticks are mutable meanwhile, and a resume must continue exactly the job that paused.
  activeInstallReq: null,
  installing: false, installDone: false, installFraction: 0, installMsg: '', installPhase: '', installError: null,
  // 'pause' | 'cancel' while an install.cancel rpc is in flight — it decides what the backend's
  // install.cancelled event means; installPaused keeps the (frozen) overlay up until Resume/Give up.
  installStopIntent: null, installPaused: false,
  activeInstallId: null, // snapshot of the id actually installing — wiz.versionId is mutable mid-install

  cmdUid: '',
  cmd: { exp: '10000', character: '10000002', artifact: '71544 20', weapon: '13501', level: '60', mora: '100000', primogems: '10000' },
  // Seeded in boot() once the language is loaded (the "console ready" line is translated).
  log: [],
  // Content catalogue for the command pickers, keyed on the version it was fetched for. Any status
  // other than 'ok' falls back to the raw-id cards, so the screen never depends on this rpc.
  gd: { version: null, status: 'idle', error: '', avatars: [], weapons: [], sets: [] },
  // artifactMode: 'equip' = the pieces are equipped DIRECTLY on the in-game character, at the chosen
  // level (equip add — default: "item add" has no level parameter, so +20 pieces only come this way);
  // 'inv' = the pieces land in the inventory unequipped, at +0 (item add).
  sel: { avatarId: null, weaponId: null, weaponLevel: '90', weaponPromote: '6', setName: null, artifactLevel: '20', artifactMode: 'equip' },
  ingame: { promote: '6' }, // "In-game character" card: ascension level for break
  helpOpen: false,          // player instructions, collapsed by default
  picker: null,          // {kind:'avatar'|'weapon'|'artifact', q, el, type, r} while the modal is open
  rawOpen: false, rawCmd: '',
  sending: false,        // one GM round-trip at a time — a double-click must not fire two sets

  // "Starting the game" overlay: {versionId, msg} while the session starts; null once the client has
  // actually appeared (play.appeared) or the session ended/failed.
  playing: null,
  // Generic "are you sure?" dialog: {title, body, yes, act, arg, at, checkbox?} — confirmYes runs
  // ACTIONS[act](arg, checkboxChecked, selected). A chooser adds options [{id, label, sub, danger?}] +
  // selected, with bodyFor / yesFor {id: text} picking the body and the Yes label of the selected
  // option (the Prepare-the-server dialog); wide = the 600 px box for a dialog with option cards.
  confirm: null,
  // "OK only" information window: {title, body}. For events the user must READ (today: the game
  // closed because Fiddler was closed) — a toast would vanish on its own.
  notice: null,

  // Server status exactly as the agent reports it: versions[id] = the raw object
  // {up, present, bootstrapped, provisioned, txtFixes, account, ...}, info = the rest (agent,
  // advertisedIp, busy, error). state: 'idle' | 'checking' | 'ok' | 'error'.
  srv: { state: 'idle', error: '', versions: {}, info: null },
  // The connected server's own word on the pre-made in-game login, per version, from the status reads
  // of THIS target (ingestStatus; srvRetarget clears it): name, or '' = the server said there is NONE
  // (a keep / fixes server) — an answered '' never falls back to the catalogue.
  srvAccount: {},
  // The same answer as the backend persisted it for the current server (init state `serverAccounts`),
  // for the first render before the first status read of a session.
  serverAccounts: {},
  // Long server operation in progress + its streamed log. null = nothing running.
  srvJob: null,          // {kind, version, target, host, purge, progress, enabled, lines: [], running, error} — target: see jobHere
  srvAdvOpen: false,
  // "Copy an account's progress" form (server advanced settings): null or {version, from, to}.
  acctCopy: null,

  // ── modes / start screen ──
  // mode: '' (not chosen yet — the start screen asks) | 'player' | 'admin'. A UX gate only: the agent
  // bearer token is the security boundary (the backend refuses admin rpcs without it).
  mode: '',
  // muted / animOff: the two start-screen media pills (remembered in localStorage + state.json).
  // hidden: the window sits in the tray — the media pause, whatever the pills say.
  // agentPort: the optional agent port typed on the admin panel ('' = the TXT record / the port known
  // for the host / 18080) — a text field, parsed when Test / Enter send it.
  login: { panel: null, address: '', token: '', agentPort: '', testing: false, test: null, muted: false, animOff: false, hidden: false },
  defaults: { servers: [], mode: '' },           // baked by publish.ps1 (quick-pick servers)
  serverAddr: { host: '', port: 21000, agentPort: 18080, source: 'default' },
  // The server address typed in Settings / the install wizard and not saved yet (null = none): it is
  // saved through server.setAddress, never through settings.save (see saveAddressEdit).
  addrEdit: null,
  // tokenScope: 'any' (baked into the build) | host the stored token is for | ''. mode: 'direct' |
  // 'ssh' — how the configured host's token connects (an ssh-mode server.json is legacy: nothing in
  // the UI creates one any more, the agentbar only names it).
  admin: { hasToken: false, tokenScope: '', mode: 'direct' },
  isWindows: true,
  // The voice languages this build knows, in the game's own order (init state voiceLanguages) — the
  // catalogue's packs (versions[].voices) already come in it; kept so nothing here hard-codes a name.
  voiceLanguages: [],
  // Player side: last /public/status of the configured server + the accounts created through Relic:
  // accounts = the login shown per version, accountLists = every one this launcher created there
  // (per version, oldest first -- agent 3.7 lets a player create several).
  pub: { status: null },
  accounts: {},
  accountLists: {},
  // The player's "Your account on this server" card (see accountBlank). version = the chooser's pick,
  // reset only with the server (srvRetarget) — never by applyInit, whose selectedVersionId is the
  // Library's and says nothing about which version the player wants an account on.
  account: accountBlank(),
  // Server Admin mode's "Create a player account" card (see acctNewBlank).
  acctNew: acctNewBlank(),
  // The account.create rpc in flight, either card's: {origin, target, version, ...} | null. Kept
  // across a server switch ON PURPOSE: its account.* events still arrive, and must then land nowhere
  // but a toast — never in the new server's form (see the account.* handlers).
  acctPending: null,
  // Admin side: policy / secrets / local agent / install-agent overlay / removal overlay.
  policy: { data: null, loading: false },
  secrets: { version: null, data: null, form: {}, loading: false },
  // Official in-version hotpatch mirror (agent 3.1): the last GET /server/hotpatch of the selected version.
  // voiceSel = the admin's voice-pack ticks {language: bool} (agent 3.3) — render() rewrites #app, so
  // they live here; voiceKey = what they were taken from (server + version + the server's selection,
  // see syncVoiceSel). busy = the hotpatch / voice job the last admin /status named (trackHotpatchBusy);
  // voiceStop = the version whose "Stop mirroring" was pressed — dead until that job's state changes.
  hotpatch: hotpatchBlank(),
  // "Agent settings" card (agent 3.6: GET/POST /agent/config, POST /agent/restart, the stack folders
  // through POST /server/relocate) — see agentCfgBlank. Per server: srvRetarget resets it.
  agentCfg: agentCfgBlank(),
  // The agent installed on THIS PC (Windows). init = the init-state fact {installed, host, port,
  // autostart} | null (off Windows), refreshed by every reply that carries the init state, so the start
  // screen can offer it before any mode is chosen; status = the admin-only localagent.status of the
  // Server page card; busy = an enter / uninstall in flight (the UAC prompt of the firewall step can
  // hold it for a minute — both buttons stay dead meanwhile).
  localAgent: { status: null, init: null, busy: false },
  agentInstall: null,   // {open, target, form, running, lines, done, error, result}
  // Versions an agent install marked for download (deploy.done `fetch`), still to start: one stack
  // download job at a time, the next one started when the previous ends (startNextFetch).
  fetchQueue: [],
  remove: null,         // {versionId, running, lines, msg, fraction, error, done, leftovers}

  toast: null,
};

const isInstalled = (id) => S.installed.some((i) => i.id === id) || S.versions.some((v) => v.id === id && v.installed);
const versionById = (id) => S.versions.find((v) => v.id === id);
// In-game enhancements (F1 menu) apply to a version when the setting is on (default) AND this build
// ships the DLL for it (versions[].hasEnhancements) — the same rule as Backend.EnhancementsHintFor.
const eeWanted = (id) => S.settings.enhancements !== false && !!((versionById(id) || {}).hasEnhancements);
// The version's IN-GAME login (the account of the prepared save: "aether" on 1.6, "Aetherr" on 2.8).
// It comes from the backend — see GameAccounts in Relic.Core — so it is NOT hand-written here; a
// version without a known account returns '' and every place that shows it hides itself.
// Policy-aware (2026-08-21): an account the player created through Relic on THIS server wins; then
// the server's own word (agent 3.4: a server prepared with "keep my progress" or "fixes only" has NO
// pre-made account and answers '' — that hides the login everywhere, it never falls back); then the
// answer the backend remembered for this server; the catalogue/payload value only before any answer.
const hasOwn = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k);
const accountOf = (id) => {
  const mine = S.accounts && S.accounts[id];
  if (mine && mine.name) return mine.name;
  if (hasOwn(S.srvAccount, id)) return S.srvAccount[id] || '';
  if (hasOwn(S.serverAccounts, id)) return S.serverAccounts[id] || '';
  const v = versionById(id); return (v && v.account) || '';
};
// The catalogue's (or the shipped manifest's) account of the pre-made save — what a "default" Prepare
// imports. Not accountOf(): that is the server's CURRENT word, '' on a keep / fixes server, which is
// exactly what the Prepare dialog is about to change.
const catalogueAccountOf = (id) => ((versionById(id) || {}).account) || '';
// True when the server verifies passwords for this version (the "any" wording must change). Read from
// the normalised status (srvFacts carries passwordVerify for BOTH shapes — the admin /status and the
// public snapshot), so an admin whose stack verifies passwords sees the right word too; before, only
// the player-mode snapshot counted and admin mode always said "any".
const passwordVerified = (id) => { const f = srvFacts(id); return !!(f && f.passwordVerify); };
// True when the server ADVERTISES the official 2021 hotpatch mirror for this version (the client
// then downloads the fixes once at login): the public snapshot in player mode (its `enabled` already
// means "enabled and not pending"), the admin's /status otherwise — there `pending` (database half
// owed, or an unusable mirror URL) means no client is told anything yet, so no badge.
// ingestStatus keeps the field in S.srv.versions for both shapes.
const hotpatchServed = (id) => {
  const pub = S.pub.status && S.pub.status.versions && S.pub.status.versions[id];
  if (pub && pub.hotpatch && pub.hotpatch.enabled) return true;
  const e = S.srv.versions && S.srv.versions[id];
  return !!(e && e.hotpatch && e.hotpatch.enabled && !e.hotpatch.pending);
};
const accountIsMine = (id) => !!(S.accounts && S.accounts[id] && S.accounts[id].name);
// The value shown for the in-game password, the same everywhere: "any" (the server does not verify
// it — the normal case) or, when it does, whose it is. Never an example value: "e.g. 123" read as a
// required password.
const passwordWord = (id) => passwordVerified(id) ? (accountIsMine(id) ? t('account.pwYours') : t('lib.creds.verifiedAsk')) : t('lib.creds.any');
const selected = () => versionById(S.selectedVersionId) || S.versions[0];
const heroImg = (id) => `assets/hero-${id}.webp`;
const isSummer = () => S.theme === 'summer';
// Archipelago theme: the hero background follows the selected version (1.6 has its own summer art).
const summerHero = (id) => `assets/summer-${id === '1.6' ? '1.6' : '2.8'}.webp`;
function applyTheme(t) {
  S.theme = t === 'summer' ? 'summer' : 'classic';
  document.body.className = 'theme-' + S.theme;
  try { localStorage.setItem('relic-theme', S.theme); } catch (e) { /* no localStorage */ }
}
const splashImg = (id) => `assets/splash/${id}.webp`;
// Weapon/artifact sprites are keyed by the game's own icon name (UI_EquipIcon_*, UI_RelicIcon_*),
// fetched by build/make_catalog.py. Missing art degrades to the tile's placeholder underneath.
const iconImg = (n) => `assets/icons/${n}.webp`;

// ─────────────────────────── catalogue ───────────────────────────
const EL = { pyro: '#e4664e', hydro: '#4cb2e6', anemo: '#6fd0b0', electro: '#b48ee0', dendro: '#8fbf50', cryo: '#9fd8e8', geo: '#e0a94a' };
const ELEMENTS = ['Pyro', 'Hydro', 'Anemo', 'Electro', 'Dendro', 'Cryo', 'Geo'];
const elColor = (e) => EL[String(e).toLowerCase()] || '#9b7d3a';
// Label tables hold translation KEYS, translated at use: they are evaluated at module load, before
// the language file exists.
const WTYPES = [
  { key: 'sword', many: 'cmd.wtype.sword.many', one: 'cmd.wtype.sword.one' },
  { key: 'claymore', many: 'cmd.wtype.claymore.many', one: 'cmd.wtype.claymore.one' },
  { key: 'polearm', many: 'cmd.wtype.polearm.many', one: 'cmd.wtype.polearm.one' },
  { key: 'catalyst', many: 'cmd.wtype.catalyst.many', one: 'cmd.wtype.catalyst.one' },
  { key: 'bow', many: 'cmd.wtype.bow.many', one: 'cmd.wtype.bow.one' },
];
const wtype = (t) => WTYPES.find((w) => w.key === String(t).toLowerCase());
const SLOT_KEYS = ['flower', 'plume', 'sands', 'goblet', 'circlet'];
const SLOT_NAMES = ['cmd.slot.flower', 'cmd.slot.plume', 'cmd.slot.sands', 'cmd.slot.goblet', 'cmd.slot.circlet'];
const slotName = (i) => (SLOT_NAMES[i] ? t(SLOT_NAMES[i]) : t('cmd.slot.piece', { n: i + 1 }));

const curAvatar = () => S.gd.avatars.find((a) => a.id === S.sel.avatarId) || null;
const curWeapon = () => S.gd.weapons.find((w) => w.id === S.sel.weaponId) || null;
const curSet = () => S.gd.sets.find((s) => s.name === S.sel.setName) || null;
const initial = (s) => (String(s || '').trim().charAt(0) || '?').toUpperCase();
const stars = (n) => `<span class="stars">${'★'.repeat(Math.max(0, Math.min(5, n)))}</span>`;
const clampInt = (v, lo, hi, dflt) => { const n = parseInt(v, 10); return isNaN(n) ? dflt : Math.max(lo, Math.min(hi, n)); };

// The catalogue is produced by a separate data file whose field names may drift, so every record is
// read through pick(): a renamed or missing field degrades to a blank instead of throwing.
const pick = (o, ...keys) => { for (const k of keys) { const v = o && o[k]; if (v !== undefined && v !== null && v !== '') return v; } return ''; };
const num = (v) => { const n = parseInt(v, 10); return isNaN(n) ? 0 : n; };
const aliasText = (v) => (Array.isArray(v) ? v.join(' ') : String(v || ''));
// One lowercased haystack per record, built once at load: the picker's keystroke filter must stay a
// plain substring test, never a per-key rebuild of the record list.
const searchKey = (...parts) => parts.filter(Boolean).join(' ').toLowerCase();

function normAvatar(r) {
  const id = num(pick(r, 'id', 'avatarId'));
  if (!id) return null;
  const name = String(pick(r, 'name', 'title') || id);
  return {
    id, name,
    element: String(pick(r, 'element', 'el')),
    weapon: String(pick(r, 'weapon', 'weaponType', 'type')),
    rarity: num(pick(r, 'rarity', 'stars', 'quality')) || 5,
    since: String(pick(r, 'since', 'version', 'addedIn')),
    s: searchKey(name, aliasText(r && r.aliases), id),
  };
}

function normWeapon(r) {
  const id = num(pick(r, 'id', 'weaponId'));
  if (!id) return null;
  const name = String(pick(r, 'name', 'title') || id);
  return {
    id, name,
    type: String(pick(r, 'type', 'weaponType', 'weapon')),
    rarity: num(pick(r, 'rarity', 'stars', 'quality')) || 5,
    since: String(pick(r, 'since', 'version', 'addedIn')),
    icon: String(pick(r, 'icon') || ''),
    s: searchKey(name, aliasText(r && r.aliases), id),
  };
}

/// A small sprite tile with the item's initial showing through when the art is missing.
function iconTile(icon, label, cls) {
  // data-artwrap opts into the same capture-phase error handler the splash art uses: a sprite whose
  // file is missing hides the <img> and leaves the tinted initial, instead of a broken-image glyph.
  return `<span class="spr ${cls || ''}" data-artwrap><i>${esc(initial(label))}</i>${
    icon ? `<img src="${esc(iconImg(icon))}" alt="" loading="lazy" decoding="async"/>` : ''}</span>`;
}

function normSet(r) {
  const name = String(pick(r, 'name', 'setName', 'set'));
  const ids = setPieceIds(r);
  if (!name || !ids.length) return null;
  const raw = (r && (r.pieces || r.parts || r.items)) || null;
  return {
    name, ids,
    // One sprite per piece, in the same order as ids, so a set previews as its five real pieces.
    icons: Array.isArray(raw) ? raw.map((p) => String((p && p.icon) || '')) : [],
    rarity: num(pick(r, 'rarity', 'stars', 'quality')) || 5,
    since: String(pick(r, 'since', 'version', 'addedIn')),
    s: searchKey(name, aliasText(r && r.aliases), ids.join(' ')),
  };
}

// Piece ids arrive as an explicit list, as a slot→id object, or only as the set's base id — the five
// pieces of a set are five consecutive ids from that base (23300..23304), so derive them when needed.
function setPieceIds(r) {
  const raw = (r && (r.pieces || r.parts || r.items)) || null;
  let ids = [];
  if (Array.isArray(raw)) ids = raw.map((p) => num(p && typeof p === 'object' ? pick(p, 'id', 'pieceId') : p));
  else if (raw && typeof raw === 'object') ids = SLOT_KEYS.map((k) => num(raw[k]));
  else if (r && SLOT_KEYS.some((k) => r[k])) ids = SLOT_KEYS.map((k) => num(r[k]));
  else { const base = num(pick(r, 'baseId', 'firstId', 'setId', 'id')); if (base) ids = [0, 1, 2, 3, 4].map((i) => base + i); }
  return ids.filter((n) => n > 0).slice(0, 5);
}

const normList = (arr, fn) => (Array.isArray(arr) ? arr.map(fn).filter(Boolean) : []);

// A fast version switch can leave a slow reply in flight; stamp each request so a stale one cannot
// overwrite the catalogue of the version the user is actually looking at.
let gdSeq = 0;
async function loadGameData(force) {
  const version = S.selectedVersionId;
  // No version selected (an empty catalogue, a state file that names none): there is nothing to
  // fetch, and returning while S.gd stays "idle" left the screen on "loading the content
  // catalogue" for ever. Say it is unavailable and open the raw-id inputs, which is the whole
  // capability without a catalogue.
  if (!version) {
    S.gd = { version: null, status: "empty", error: "", avatars: [], weapons: [], sets: [] };
    S.rawOpen = true;
    render();
    return;
  }
  if (!force && S.gd.version === version && S.gd.status === 'ok') return;
  const seq = ++gdSeq;
  S.gd = { version, status: 'loading', error: '', avatars: [], weapons: [], sets: [] };
  render();
  try {
    const d = await rpc('gamedata.forVersion', { version });
    if (seq !== gdSeq) return;
    const avatars = normList(d && (d.avatars || d.characters), normAvatar)
      .sort((a, b) => (b.rarity - a.rarity) || a.name.localeCompare(b.name, I18N.code));
    // 4★ and 5★ weapons and sets are offered (5★ first); a catalogue without a rarity field
    // defaults to 5, so a missing field lists everything instead of silently emptying the picker.
    const weapons = normList(d && d.weapons, normWeapon).filter((w) => w.rarity >= 4)
      .sort((a, b) => (b.rarity - a.rarity) || a.name.localeCompare(b.name, I18N.code));
    const sets = normList(d && (d.sets || d.artifactSets), normSet).filter((s) => s.rarity >= 4)
      .sort((a, b) => (b.rarity - a.rarity) || a.name.localeCompare(b.name, I18N.code));
    S.gd = { version, status: (avatars.length || weapons.length || sets.length) ? 'ok' : 'empty', error: '', avatars, weapons, sets };
    if (!S.gd.avatars.some((a) => a.id === S.sel.avatarId)) S.sel.avatarId = avatars.length ? avatars[0].id : null;
    if (!S.gd.weapons.some((w) => w.id === S.sel.weaponId)) S.sel.weaponId = null;
    if (!S.gd.sets.some((s) => s.name === S.sel.setName)) S.sel.setName = null;
    if (S.gd.status !== 'ok') S.rawOpen = true;
  } catch (e) {
    if (seq !== gdSeq) return;
    S.gd.status = 'error'; S.gd.error = e.message;
    S.rawOpen = true; // the manual id inputs are the whole capability now — do not make them hunt
  }
  render();
}

// ─────────────────────────── icons ───────────────────────────
const I = {
  diamond: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2 22 12 12 22 2 12z"/></svg>',
  grid: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  download: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 21h16"/></svg>',
  wand: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8 19 13M15 9h.01M17.8 6.2 19 5M3 21l9-9M12.2 6.2 11 5"/></svg>',
  stack: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01M6 17h.01"/></svg>',
  gear: '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.6 1.6 0 0 0-2.75 1.14V21a2 2 0 1 1-4 0v-.09A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.6 1.6 0 0 0 4.6 15a1.6 1.6 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.6 1.6 0 0 0 9 4.6a1.6 1.6 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.6 1.6 0 0 0 2.75 1.14l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.6 1.6 0 0 0 19.4 9a1.6 1.6 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.6 1.6 0 0 0-1.51 1z"/></svg>',
  play: '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4l14 8-14 8z"/></svg>',
  folder: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>',
  check: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4 8-9"/></svg>',
  star: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="m12 2 2.9 6.3 6.9.6-5.2 4.5 1.6 6.7L12 17l-6.2 3.6 1.6-6.7L2.2 8.9l6.9-.6z"/></svg>',
  user: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  userSm: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  atom: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="2.6"/><path d="M12 3a3 3 0 0 0 0 6M12 15a3 3 0 0 0 0 6M4.5 7.5a3 3 0 0 0 5.2 3M14.3 13.5a3 3 0 0 0 5.2 3M4.5 16.5a3 3 0 0 1 5.2-3M14.3 10.5a3 3 0 0 1 5.2-3"/></svg>',
  sword: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 17.5 3 6V3h3l11.5 11.5"/><path d="m13 19 6-6"/><path d="m16 16 4 4"/><path d="m19 21 2-2"/></svg>',
  up: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m17 11-5-5-5 5"/><path d="m17 18-5-5-5 5"/></svg>',
  coin: '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="8"/><path d="M12 8v8M9.5 10h5M9.5 14h5"/></svg>',
  warn: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#c07b2a" stroke-width="1.9"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
};
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Translation helpers for HTML context: T() = escaped plain text; tf() additionally renders the
// {b}{/b}{i}{/i}{br} placeholders of the few emphasised strings (text stays escaped).
const T = (k, v) => esc(t(k, v));
function tf(k, v) { return esc(t(k, v)).replace(/\{b\}/g, '<b>').replace(/\{\/b\}/g, '</b>').replace(/\{i\}/g, '<i>').replace(/\{\/i\}/g, '</i>').replace(/\{br\}/g, '<br>'); }

// Bitmap icons for the categories with in-game art (artifacts + weapons) — the rest stay SVGs.
const CMDICON = {
  artifact: '<img class="cmdic" src="assets/cmd-artifacts.webp" alt="" decoding="async"/>',
  weapon: '<img class="cmdic" src="assets/cmd-swords.png" alt="" decoding="async"/>',
};

// ─────────────────────────── render ───────────────────────────
function render() {
  const app = document.getElementById('app');
  // render() rewrites the whole app; streamed log events call it while the (now taller than the
  // viewport) commands screen is scrolled, so carry the scroll positions across the rewrite or every
  // log line yanks the user back to the top. EVERY pane scrolled at this moment is carried, found by
  // its offset rather than by a list of selectors (.main and .pickbody were the list): the agent-install
  // form is a box of its own with overflow-y:auto, and each of its switches — "Start the agent with
  // Windows", "I already have it", the target cards — re-rendered it back to the top (2026-09-30).
  const panes = scrolledPanes(app);
  // The consoles are the panes that must follow the NEWEST line, not the old offset: play.log and
  // server.log stream a line at a time and each one re-renders, so restoring a stale scrollTop would
  // pin the user to the top while the messages they are waiting on scroll out of sight below.
  const logs = LOGPANES.map((sel) => logScroll(app, sel));
  app.innerHTML = titlebar() + (S.screen === 'login' ? login() : shell()) + (S.toast ? toast() : '');
  panes.forEach((p) => restorePane(app, p));
  logs.forEach((l) => restoreLogScroll(app, l));
  // A chip click or a selection inside the picker re-renders it — put the caret back in the search
  // box so the typed query keeps working. Strictly conditional: nothing else may steal focus.
  if (S.picker) {
    const q = app.querySelector('.pickbar input[data-filter]');
    if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  }
  // The progress-bar nodes are new after the rewrite: give them the current value and, if there is
  // still ground to cover up to the target, restart the loop. Without an overlay, stop the loop.
  syncProgress();
  // The start-screen video/audio are static nodes outside #app: only their visibility / play state
  // follows the screen, so they never restart on a render.
  LOGIN_MEDIA.sync();
}
// Every element scrolled away from its top, as {path, top}. The path is one step per node below #app —
// tag, id, class and the index among the siblings that share them —, which is what the same screen
// draws again after a switch is flipped; a pane the new markup no longer has is simply not found, and a
// node at the same path with less content clamps the offset itself. Reading scrollTop on every node is
// cheap: the layout is clean at this point and nothing is written between the reads.
function scrolledPanes(app) {
  const panes = [];
  for (const el of app.querySelectorAll('*')) if (el.scrollTop > 0) panes.push({ path: pathIn(app, el), top: el.scrollTop });
  return panes;
}
function paneKey(el) { return el.tagName + '#' + el.id + '.' + (el.getAttribute('class') || ''); }
function pathIn(app, el) {
  const path = [];
  for (let n = el; n && n !== app; n = n.parentElement) {
    const key = paneKey(n);
    let index = 0;
    for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (paneKey(s) === key) index++;
    path.unshift({ key, index });
  }
  return path;
}
function restorePane(app, pane) {
  let n = app;
  for (const step of pane.path) {
    let next = null, seen = 0;
    for (const c of n.children) if (paneKey(c) === step.key && seen++ === step.index) { next = c; break; }
    if (!next) return;
    n = next;
  }
  n.scrollTop = pane.top;
}
const LOGPANES = ['#cmdlog', '#srvlog', '#deploylog', '#acctlog', '#acctnewlog'];
function logScroll(app, sel) {
  const el = app.querySelector(sel);
  return { sel, top: el ? el.scrollTop : 0, pinned: !el || el.scrollTop + el.clientHeight >= el.scrollHeight - 4 };
}
function restoreLogScroll(app, s) {
  const el = app.querySelector(s.sel);
  if (el) el.scrollTop = s.pinned ? el.scrollHeight : s.top;
}

function titlebar() {
  const subtitle = S.screen === 'login' ? '' : `<span>·  ${T('common.selectedVersion', { id: S.selectedVersionId || '—' })}</span>`;
  const brand = isSummer()
    ? `<img src="assets/dodoco.webp" alt="" style="width:22px;height:22px;object-fit:contain;display:block">`
    : I.diamond;
  return `<div class="titlebar" data-drag>
    <div class="brand" style="color:var(--gold)">${brand}<b>RELIC</b>${subtitle}</div>
    <div class="winbtns">
      <button data-act="win.min" title="${T('common.minimize')}"><svg width="12" height="12" viewBox="0 0 12 12"><line x1="2" y1="6" x2="10" y2="6" stroke="currentColor" stroke-width="1.3"/></svg></button>
      <button class="close" data-act="win.close" title="${T('common.close')}"><svg width="12" height="12" viewBox="0 0 12 12"><line x1="2.5" y1="2.5" x2="9.5" y2="9.5" stroke="currentColor" stroke-width="1.3"/><line x1="9.5" y1="2.5" x2="2.5" y2="9.5" stroke="currentColor" stroke-width="1.3"/></svg></button>
    </div>
  </div>`;
}

// ── start screen ──
// "Which Way?": Player mode (a server address is all it needs) or Server Admin mode (address + agent
// token, or install the agent on a new server). The background video and the music are STATIC nodes
// in index.html (siblings of #app) — render() rewrites #app on every state change and a media element
// inside it would restart each time; LOGIN_MEDIA only toggles visibility and play state.
const LOGIN_MEDIA = (() => {
  let shownBefore = false;
  function sync() {
    const wrap = document.getElementById('login-media');
    const video = document.getElementById('login-bg');
    const audio = document.getElementById('login-audio');
    if (!wrap) return;
    const show = S.screen === 'login';
    wrap.classList.toggle('hidden', !show);
    // In the tray (X → background) nobody sees the screen: nothing plays — the music most of all.
    // Both resume when the window comes back. "Animation off" keeps the video on its current frame
    // (a still picture of the same scene), so the pill switches it without a visual jump.
    if (show && !S.login.hidden) {
      if (video) {
        if (S.login.animOff) { if (!video.paused) video.pause(); }
        else if (video.paused) video.play().catch(() => {});
      }
      if (audio) {
        audio.muted = !!S.login.muted;
        if (!S.login.muted && audio.paused) audio.play().catch(() => { /* autoplay refused: the pill click starts it */ });
        if (S.login.muted && !audio.paused) audio.pause();
      }
      shownBefore = true;
    } else if (shownBefore) {
      if (video && !video.paused) video.pause();
      if (audio && !audio.paused) audio.pause();
    }
  }
  return { sync };
})();

const ICON_LOGIN = {
  motion: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--gold2)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.5v5l4-2.5z" fill="var(--gold2)" stroke="none"/></svg>',
  still: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#9aa3b5" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><line x1="10" y1="9.5" x2="10" y2="14.5"/><line x1="14" y1="9.5" x2="14" y2="14.5"/></svg>',
  sound: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--gold2)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.8a4.6 4.6 0 0 1 0 6.4"/><path d="M18.4 5.6a9 9 0 0 1 0 12.8"/></svg>',
  mute: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#9aa3b5" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><line x1="16" y1="9.5" x2="21" y2="14.5"/><line x1="21" y1="9.5" x2="16" y2="14.5"/></svg>',
  player: '<svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/></svg>',
  shield: '<svg width="23" height="23" viewBox="0 0 24 24" fill="none" stroke="var(--gold2)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><path d="M12 3 20 6v5.6c0 4.5-3.4 8.4-8 9.4-4.6-1-8-4.9-8-9.4V6z"/><circle cx="12" cy="11" r="1.7"/><path d="M12 12.7V15"/></svg>',
  info: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>',
  lock: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>',
  x: '<svg width="11" height="11" viewBox="0 0 12 12"><line x1="2.5" y1="2.5" x2="9.5" y2="9.5" stroke="currentColor" stroke-width="1.4"/><line x1="9.5" y1="2.5" x2="2.5" y2="9.5" stroke="currentColor" stroke-width="1.4"/></svg>',
};

function loginStatusLine() {
  const r = S.login.test;
  if (S.login.testing) return `<div class="lg-status warn"><span class="dot warn"></span><span>${T('login.testing')}</span></div>`;
  if (!r) return '';
  const okAgent = !!(r.agent && r.agent.reachable);
  const okGame = !!(r.game && r.game.reachable === true);
  const cls = okGame || okAgent ? 'ok' : (r.game && r.game.reachable === false ? 'bad' : 'warn');
  let txt;
  if (okAgent) txt = t('login.test.agentOk', { name: r.agent.name || r.host, versions: (r.agent.versionsUp || []).join(', ') || '—' });
  else if (okGame) txt = t('login.test.gameOk', { host: r.host, port: r.port });
  else if (r.game && r.game.reachable === false) txt = t('login.test.down', { host: r.host, port: r.port });
  else txt = t('login.test.unknown');
  const src = r.source === 'txt' ? ` · ${T('login.portFromTxt', { port: r.port })}` : '';
  return `<div class="lg-status ${cls}"><span class="dot ${cls === 'ok' ? 'live' : cls === 'bad' ? 'down' : 'unknown'}"></span><span>${esc(txt)}${src}</span></div>`;
}

// The bare host of what the user typed ("http://Host:21000/" -> "host"): the start screen compares it
// with the host a stored token was saved for.
function hostOf(addr) {
  let h = String(addr || '').trim().toLowerCase();
  const i = h.indexOf('://'); if (i >= 0) h = h.slice(i + 3);
  h = h.split('/')[0];
  if (h.startsWith('[')) { const c = h.indexOf(']'); return c > 0 ? h.slice(1, c) : h; }
  if ((h.match(/:/g) || []).length === 1) h = h.split(':')[0];
  return h.replace(/\.$/, '');
}
// Does a stored/baked token apply to this address? A token saved for another server is not a token
// for this one (the backend applies the same rule — see ServerConfig.Effective).
function tokenAppliesTo(address) {
  const sc = S.admin.tokenScope;
  if (sc === 'any') return true;
  const h = hostOf(address);
  return !!sc && !!h && hostOf(sc) === h;
}
// Whether the start-screen CTA is usable RIGHT NOW — recomputed on every keystroke (syncLoginCta),
// not only at render: typing does not re-render, and the button used to stay disabled until Test.
function loginCanEnter() {
  if (S.login.testing) return false;
  if (S.login.address.trim().length === 0) return false;
  if (S.login.panel !== 'admin') return true;
  return S.login.token.trim().length > 0 || tokenAppliesTo(S.login.address);
}
// The main CTA only: the "Use the agent on this PC" button (.lg-cta-alt) needs neither address nor token.
// The local-agent buttons of the start screen, patched IN PLACE. Flipping S.localAgent.busy used to
// go through render(), which rewrites all of #app for a label and a disabled flag: the whole start
// screen (logo, panel, pills) was rebuilt under the pointer and read as the launcher "refreshing"
// itself on the click. Same rule as the CTA below and the install overlay: what can be patched is
// patched, never re-rendered.
function syncLocalAgentButtons() {
  const busy = !!S.localAgent.busy;
  const enter = document.querySelector('.lg-cta-alt[data-act="localAgentEnter"]');
  if (enter) {
    enter.disabled = busy;
    enter.textContent = busy ? t('login.localAgent.entering') : t('login.localAgent.enter');
  }
  document.querySelectorAll('[data-act="localAgentUninstall"]').forEach((b) => { b.disabled = busy; });
}

function syncLoginCta() {
  const b = document.querySelector('.lg-cta:not(.lg-cta-alt)');
  if (b) b.disabled = !loginCanEnter();
  const tk = document.querySelector('input[data-model="login.token"]');
  if (tk) tk.placeholder = tokenAppliesTo(S.login.address) ? t('login.tokenKept') : t('login.tokenPlaceholder');
}

function loginPanel() {
  const isAdmin = S.login.panel === 'admin';
  const chips = (S.defaults.servers || []).map((s) =>
    `<button class="lg-chip ${S.login.address === s.host ? 'on' : ''}" data-act="loginChip" data-arg="${esc(s.host)}">${esc(s.label || s.host)}</button>`).join('');
  const canEnter = loginCanEnter();
  // Entry animation through once(): every button in the panel (chips, Test, tabs, a toast) goes
  // through render(), which rewrites #app — a CSS animation on the class would replay the whole
  // box flying in on each click, which read as the panel closing and reopening.
  return `<div class="lg-panel" style="${once('login:panel', 'rl-boxin', .35)}">
    <span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>
    <div class="ph"><h2>${isAdmin ? T('login.panel.admin') : T('login.panel.player')}</h2>
      <button class="x" data-act="loginClose" title="${T('common.back')}">${ICON_LOGIN.x}</button></div>
    <div class="lg-tabs">
      <button class="${!isAdmin ? 'on' : ''}" data-act="loginTab" data-arg="player">${T('login.tab.player')}</button>
      <button class="${isAdmin ? 'on' : ''}" data-act="loginTab" data-arg="admin">${T('login.tab.admin')}</button>
    </div>
    <label class="lg-label">${T('login.serverLabel')}</label>
    ${chips ? `<div class="lg-chips">${chips}</div>` : ''}
    <div class="lg-row">
      <input class="txt mono" data-model="login.address" value="${esc(S.login.address)}" placeholder="${T('login.serverPlaceholder')}" spellcheck="false" autocomplete="off">
      <button class="lg-test" data-act="loginTest" ${S.login.testing ? 'disabled' : ''}>${T('login.test')}</button>
    </div>
    ${loginStatusLine()}
    <div class="lg-hint">${ICON_LOGIN.info}<span>${T('login.serverHint')}</span></div>
    ${isAdmin ? `
    <label class="lg-label">${T('login.tokenLabel')}</label>
    <input class="txt mono" type="password" data-model="login.token" value="${esc(S.login.token)}" placeholder="${esc(tokenAppliesTo(S.login.address) ? t('login.tokenKept') : t('login.tokenPlaceholder'))}" autocomplete="off">
    <div class="lg-hint">${ICON_LOGIN.lock}<span>${T('login.tokenHint')}</span></div>
    <label class="lg-label">${T('login.agentPortLabel')}</label>
    <input class="txt mono" data-model="login.agentPort" value="${esc(S.login.agentPort)}" placeholder="${hostOf(S.login.address) && hostOf(S.login.address) === hostOf(S.serverAddr.host) ? S.serverAddr.agentPort : 18080}" inputmode="numeric" autocomplete="off" spellcheck="false">
    <div class="lg-hint">${ICON_LOGIN.info}<span>${T('login.agentPortHint')}</span></div>
    <button class="lg-link" data-act="deployOpen">${T('login.installAgentLink')}</button>
    <button class="lg-link" data-act="openGuide" style="display:block;margin-top:6px">${T('help.guideLink')}</button>` : ''}
    <button class="lg-cta" data-act="loginEnter" ${canEnter ? '' : 'disabled'}>${isAdmin ? T('login.cta.admin') : T('login.cta.player')}</button>
    ${isAdmin ? loginLocalAgent() : ''}
  </div>`;
}

// Admin tab, Windows, an agent installed on this PC (the init-state fact, no health probe): a second
// way in that needs neither the address nor the token — localagent.enter reads both from the agent's
// own config, starts it when it is not running and validates the token before anything is saved.
// The uninstall link is the counterpart of the card's button on the Server page (same confirm).
function loginLocalAgent() {
  const init = S.localAgent.init;
  if (!S.isWindows || !init || !init.installed) return '';
  const busy = S.localAgent.busy;
  return `<div class="lg-local">
    <button class="lg-cta lg-cta-alt" data-act="localAgentEnter" ${busy ? 'disabled' : ''}>${busy ? T('login.localAgent.entering') : T('login.localAgent.enter')}</button>
    <div class="lg-hint">${ICON_LOGIN.info}<span>${T('login.localAgent.hint', { host: init.host || '127.0.0.1', port: init.port || 18080 })}</span></div>
    <button class="lg-link" data-act="localAgentUninstall" ${busy ? 'disabled' : ''}>${T('login.localAgent.uninstallLink')}</button>
  </div>`;
}

// The agent port typed on the admin panel: null when the field is empty (the backend then resolves
// it — TXT record, the port known for the host, 18080), a number when it is a valid port, false when
// it is neither (the caller refuses instead of silently dialling the default).
function loginAgentPort() {
  const raw = String(S.login.agentPort || '').trim();
  if (!raw) return null;
  const n = /^\d{1,5}$/.test(raw) ? parseInt(raw, 10) : NaN;
  return n >= 1 && n <= 65535 ? n : false;
}

function loginFootPill() {
  const host = (S.serverAddr && S.serverAddr.host) || '';
  if (!host) return `<div class="pill"><span class="dot unknown"></span>${T('login.noServer')}</div>`;
  const live = S.srv.state === 'ok' ? srvLiveId() : null;
  const known = S.srv.state === 'ok' && (!!live || !srvDockerError());
  const dot = known ? (live ? 'live' : 'down') : 'unknown';
  const label = known ? (live ? t('login.pill.online') : t('login.pill.offline')) : t('login.pill.unknown');
  return `<div class="pill"><span class="dot ${dot}"></span>${esc(label)} · ${esc(host)}</div>`;
}

function login() {
  // The logo float is continuous: phase it so the rewrite of #app does not restart it (see phaseOf).
  const logo = isSummer()
    ? `<img class="dodoco" src="assets/dodoco.webp" alt="" style="animation-delay:${phaseOf(5)}">`
    : `<span class="float" style="animation-delay:${phaseOf(5)}">${I.diamond.replace('width="17" height="17"', 'width="58" height="58"')}</span>`;
  const muted = !!S.login.muted, still = !!S.login.animOff;
  return `<div class="login">
    <div class="lg-pills">
      <button class="lg-mute" data-act="loginAnim" title="${T('login.anim')}">${still ? ICON_LOGIN.still : ICON_LOGIN.motion}${still ? T('login.animOff') : T('login.animOn')}</button>
      <button class="lg-mute" data-act="loginMute" title="${T('login.music')}">${muted ? ICON_LOGIN.mute : ICON_LOGIN.sound}${muted ? T('login.muted') : T('login.musicOn')}</button>
    </div>
    <div class="lg-head">${logo}<div><div class="logo">RELIC</div><div class="tagline">${T('login.tagline')}</div></div></div>
    ${S.login.panel ? loginPanel() : `
    <div class="lg-which"><i></i><span>${T('login.whichWay')}</span><i class="r"></i></div>
    <div class="lg-mode player"><button data-act="pickMode" data-arg="player" style="animation-delay:${phaseOf(3.4)}">${ICON_LOGIN.player}<span class="t"><b>${T('login.mode.player')}</b><small>${T('login.mode.playerSub')}</small></span></button></div>
    <div class="lg-mode admin"><button data-act="pickMode" data-arg="admin" style="${once('login:modes', 'rl-boxin', .5)}">${ICON_LOGIN.shield}<span class="t"><b>${T('login.mode.admin')}</b><small>${T('login.mode.adminSub')}</small></span></button></div>
    <div class="lg-foot">${loginFootPill()}</div>`}
    ${S.confirm ? confirmOverlay() : S.notice ? noticeOverlay() : S.agentInstall && S.agentInstall.open ? agentInstallOverlay() : ''}
  </div>`;
}

function shell() {
  // The overlays live on the SHELL, not inside .main: they must cover the sidebar too, so nothing
  // (Play, navigation) is clickable while one is up, on whichever screen the user is. Priority:
  // the confirm dialog wins (it is always an explicit question), then install, then the game-launch
  // splash, then the content picker.
  // The install overlay / the wizard are gone entirely: their "once" animations may run again next time.
  if (!S.installing) onceForget('inst:');
  if (S.screen !== 'install') { onceForget('wiz:'); wizStep = -1; }
  const over = S.confirm ? confirmOverlay()
    : S.notice ? noticeOverlay()
      : S.installing ? installOverlay()
        : S.remove ? removeOverlay()
          : S.agentInstall && S.agentInstall.open ? agentInstallOverlay()
            : S.agentCfg.reloc ? relocOverlay()
            : S.playing ? playOverlay()
              : S.picker ? pickerOverlay() : '';
  return `<div class="shell">${sidebar()}<div class="main">${mainContent()}</div>${over}</div>`;
}

// "Are you sure?" — one generic dialog for the irreversible actions (the progress reset, a purge...)
// and, with `options`, the chooser of the Prepare-the-server dialog: option cards above the body, the
// body and the Yes label following the selected option (bodyFor / yesFor), Yes red only for an option
// flagged dangerous (a plain dialog stays red — every one of them asks about something irreversible).
// The backdrop does NOT close the dialog (a selection drag that slips outside the box would close it
// by accident), and the buttons — the option cards too — ignore the first ~400 ms: the second click
// of a double-click on the button that opened it would otherwise land exactly on "Yes" or "Cancel"
// (or pick an option) before the user can read.
const CONFIRM_ARM_MS = 400;
function confirmOverlay() {
  const c = S.confirm;
  const opts = Array.isArray(c.options) && c.options.length ? c.options : null;
  const sel = opts ? (opts.find((o) => o.id === c.selected) || opts[0]) : null;
  const body = (sel && c.bodyFor && c.bodyFor[sel.id]) || c.body || '';
  const yes = (sel && c.yesFor && c.yesFor[sel.id]) || c.yes || t('confirm.yesDefault');
  const danger = opts ? !!(sel && sel.danger) : true;
  const cards = opts ? `<div class="copts">${opts.map((o) => `<div class="opt ${sel && sel.id === o.id ? 'sel' : ''}" data-act="confirmPick" data-arg="${esc(o.id)}"><div class="h">${esc(o.label)}</div>${o.sub ? `<p>${esc(o.sub)}</p>` : ''}</div>`).join('')}</div>` : '';
  const cb = c.checkbox;
  return `<div class="overlay"><div class="box${c.wide ? ' wide' : ''}" style="text-align:center">
    <div style="width:60px;height:60px;border-radius:50%;margin:0 auto 14px;background:rgba(208,106,94,.15);display:flex;align-items:center;justify-content:center">${I.warn}</div>
    <div class="serif" style="font-weight:700;font-size:19px">${esc(c.title)}</div>
    ${cards}
    <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;line-height:1.6;margin:10px 0 22px;text-align:left">${esc(body)}</p>
    ${cb ? `<label class="row" style="margin:0 0 18px;cursor:pointer;text-align:left;gap:14px"><div><b>${esc(cb.label)}</b>${cb.sub ? `<small>${esc(cb.sub)}</small>` : ''}</div><button class="toggle ${cb.checked ? 'on' : ''}" data-act="confirmCheckbox"><span class="knob"></span></button></label>` : ''}
    <div class="flex center gap" style="justify-content:center">
      <button class="btn ghost" data-act="confirmNo">${T('common.cancel')}</button>
      <button class="btn${danger ? ' danger' : ''}" data-act="confirmYes">${esc(yes)}</button>
    </div></div></div>`;
}

// Information window — the "OK only" twin of the confirm dialog: same drawing, one button. No arming
// window: there is no dangerous "Yes" for a double-click to steal. Drawn on the start screen too (login):
// a notice raised while it is up — an admin create that finished after "Change mode or server" — would
// otherwise be invisible while the click guard still treats it as modal. `pick`: a one-time secret (a
// server-generated password) shown as a selectable credential with a Copy button — the body text is
// user-select:none and a 16-character random password must not be copied by hand.
function noticeOverlay() {
  const n = S.notice;
  const pick = typeof n.pick === 'string' && n.pick
    ? `<div class="creds" style="margin:0 0 18px;text-align:left"><div class="crow"><div><small>${T('lib.creds.password')}</small><b class="mono pick">${esc(n.pick)}</b></div><button class="crcopy" data-act="noticeCopy">${T('common.copy')}</button></div></div>`
    : '';
  return `<div class="overlay"><div class="box" style="text-align:center">
    <div style="width:60px;height:60px;border-radius:50%;margin:0 auto 14px;background:rgba(208,106,94,.15);display:flex;align-items:center;justify-content:center">${I.warn}</div>
    <div class="serif" style="font-weight:700;font-size:19px">${esc(n.title)}</div>
    <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;line-height:1.6;margin:10px 0 ${pick ? 12 : 22}px;text-align:left">${esc(n.body)}</p>
    ${pick}
    <div class="flex center gap" style="justify-content:center">
      <button class="btn" data-act="noticeOk">${T('common.understood')}</button>
    </div></div></div>`;
}

// "Starting the game" splash — the in-app counterpart of the native window of the --play shortcut.
// Closes itself when the client has actually appeared (play.appeared) or on error/end.
function playOverlay() {
  const p = S.playing;
  return `<div class="overlay"><div class="box" style="text-align:center">
    <div class="flex center gap" style="justify-content:center;margin-bottom:10px">${spinnerHtml()}
      <div class="serif" style="font-weight:700;font-size:19px">${T('play.starting')}</div></div>
    <div style="color:var(--goldD);font-weight:800;font-size:12px;letter-spacing:2px;margin-bottom:14px">${T('play.versionLabel', { id: p.versionId })}</div>
    <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;min-height:34px;margin:0 0 16px;word-break:break-word">${esc(p.msg || t('play.preparing'))}</p>
    ${credsLine(p.versionId)}
    ${eeWanted(p.versionId) ? `<p style="color:var(--goldD);font-size:11.5px;font-weight:700;margin:0 0 10px">${T('play.eeHint')}</p>` : ''}
    <p style="color:var(--muted);font-size:11.5px;font-weight:700;margin:0 0 14px">${T('play.firstStartNote')}</p>
    <button class="btn ghost" style="padding:8px 16px;font-size:12px" data-act="hidePlaySplash">${T('common.hide')}</button>
  </div></div>`;
}

function navBtn(id, label, icon) {
  return `<button class="nav ${S.screen === id ? 'active' : ''}" data-act="go" data-arg="${id}"><span style="display:flex">${icon}</span><span>${label}</span></button>`;
}

// Mode gating of the navigation. Player mode: Library, Install, Server (status + own account) and
// Settings; GM commands only when the server's policy allows players to send them. Admin mode:
// everything. This only hides entries — the backend refuses admin rpcs on its own (RequireAdmin).
const isAdminMode = () => S.mode === 'admin';
const playerCommandsAllowed = () => !!(S.pub.status && S.pub.status.playerCommands);
function sidebar() {
  const brand = isSummer()
    ? `<img src="assets/dodoco.webp" alt="" style="width:32px;height:32px;object-fit:contain;display:block;flex:0 0 auto">`
    : `<span style="color:var(--gold)">${I.diamond.replace(/17/g,'26')}</span>`;
  const showCommands = isAdminMode() || playerCommandsAllowed();
  const modeLabel = isAdminMode() ? t('common.mode.admin') : t('common.mode.player');
  return `<div class="sidebar">
    <div class="head">${brand}<div><b>RELIC</b><small>${T('common.selectedVersion', { id: S.selectedVersionId || '—' })}</small></div></div>
    <div class="divider"></div>
    ${navBtn('library', T('common.nav.library'), I.grid)}
    ${navBtn('install', T('common.nav.install'), I.download)}
    ${showCommands ? navBtn('commands', T('common.nav.commands'), I.wand) : ''}
    ${navBtn('server', T('common.nav.server'), I.stack)}
    ${navBtn('settings', T('common.nav.settings'), I.gear)}
    <div class="spacer"></div>
    <button class="userchip" data-act="changeMode" title="${T('common.changeMode')}" style="width:100%;text-align:left;cursor:pointer">
      <div class="av">${isAdminMode() ? 'A' : T('common.playerInitial')}</div>
      <div style="min-width:0"><b>${esc(modeLabel)}</b><small style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block">${esc(S.serverAddr.host || t('login.noServer'))}</small></div>
    </button>
  </div>`;
}

function mainContent() {
  switch (S.screen) {
    case 'library': return library();
    case 'install': return install();
    case 'commands': return commands();
    case 'server': return server();
    case 'settings': return settings();
    default: return library();
  }
}

// ── in-game login details ──
// The account is not created anywhere: it comes ready-made in the version's save, so the only thing
// that can block the player on the client's login screen is not knowing it. One block, the same words
// everywhere (end of install, library, game start) — otherwise they would learn two forms for the same
// thing. The values are mouse-selectable (the rest of the app is user-select:none) and have a copy
// button, because "Aetherr" with two r's is easy to mistype.
function credsBox(id) {
  const acc = accountOf(id);
  if (!acc) return '';
  return `<div class="creds">
    <div class="crow">
      <div><small>${T('lib.creds.accountName')}</small><b class="mono pick">${esc(acc)}</b></div>
      <button class="crcopy" data-act="copyAccount" data-arg="${esc(acc)}">${T('common.copy')}</button>
    </div>
    <div class="crow">
      <div><small>${T('lib.creds.password')}</small><b>${esc(passwordWord(id))}</b></div>
    </div>
  </div>`;
}

// One-line variant, for the places too narrow for the block (the "Starting the game" splash).
function credsLine(id) {
  const acc = accountOf(id);
  if (!acc) return '';
  return `<div class="credline">${T('lib.creds.lineAccount')} <b class="mono pick">${esc(acc)}</b> · ${T('lib.creds.linePassword')} <b>${esc(passwordWord(id))}</b></div>`;
}

// Library card: shown only for an INSTALLED version, because only then does the player have a use
// for it — and it is the first thing under the hero with the PLAY button, i.e. exactly where they
// look before going in.
function accountSect(id) {
  if (!accountOf(id)) return '';
  return `<div class="sect" style="margin-bottom:18px">
    <div class="flex between center wrap" style="gap:10px">
      <h4 style="margin:0">${T('lib.account.title', { id })}</h4>
      <span class="vchip">${T('lib.account.chip')}</span>
    </div>
    <p class="hint" style="margin:8px 0 12px">${T('lib.account.hint')}</p>
    ${credsBox(id)}
  </div>`;
}

// ── voice languages ──
// The catalogue's voice packs of a version ([{lang, size, sizeText, isDefault}], in the game's own
// language order — the backend builds the list, nothing here hard-codes a name); [] for an older
// catalogue or before the backend answered.
const voicesFor = (id) => { const v = versionById(id); return v && Array.isArray(v.voices) ? v.voices : []; };
// The packs ticked for an install of `id`. The ticks live by language (S.wiz.voices), so a choice
// carries over between the two versions; a version none of whose packs is ticked — the first visit,
// or the ticked language exists only on the other version — seeds the default pack (English, else
// the first one): the list is never empty, because the game needs a voice language to start with
// and the backend refuses an install without one.
function selectedVoicePacks(id) {
  const packs = voicesFor(id);
  let ticked = packs.filter((p) => S.wiz.voices[p.lang] === true);
  if (!ticked.length && packs.length) {
    const d = packs.find((p) => p.isDefault) || packs[0];
    S.wiz.voices[d.lang] = true; ticked = [d];
  }
  return ticked;
}
const selectedVoices = (id) => selectedVoicePacks(id).map((p) => p.lang);
const packBytes = (packs) => packs.reduce((n, p) => n + (Math.max(0, Number(p.size)) || 0), 0);
// What is on disk for an installed version: the Audio_<Lang>_pkg_version markers the backend detects
// at every start / registration / add (installed[].voices) — never the wizard's ticks.
const installedVoices = (id) => { const i = S.installed.find((x) => x.id === id); return i && Array.isArray(i.voices) ? i.voices : []; };

// Library: the voice languages of an installed version — the catalogue's packs against what is on
// disk. A missing one is added from here, one language per job (install.addVoices, the same overlay
// as an install), so the player learns it BEFORE the game asks the server for 3–9 GB at login.
// It sits UNDER the "Your versions" grid (2026-09-30): the versions are what the player came for,
// a voice pack is an occasional add-on — only the account card stays between the hero and the grid.
function voicesSect(id) {
  const packs = voicesFor(id);
  if (!packs.length) return '';
  const have = installedVoices(id);
  const busy = S.installing || S.remove || S.playing ? 'disabled' : '';
  const rows = packs.map((p) => {
    const on = have.includes(p.lang);
    return `<div class="line"><div><b>${esc(p.lang)}</b><small>${esc(p.sizeText || sizeGb(p.size))}</small></div>
      ${on ? `<span style="color:var(--ok);font-weight:800;font-size:12px;white-space:nowrap"><span class="dot" style="background:var(--ok);display:inline-block;margin-right:6px;vertical-align:middle"></span>${T('lib.voices.installed')}</span>`
        : `<button class="btn ghost" style="padding:8px 12px;font-size:12px;white-space:nowrap" data-act="addVoice" data-arg="${esc(id + '|' + p.lang)}" ${busy}>${T('lib.voices.add', { gb: gb(p.size) })}</button>`}</div>`;
  }).join('');
  return `<div class="sect" style="margin:24px 0 0"><h4>${T('lib.voices.title', { id })}</h4>
    <p class="hint">${T('lib.voices.hint')}</p>${rows}</div>`;
}

// The connected server lists this version with present:false — it does not have it. Only on FRESH data:
// never while checking, after an error or without an entry for the version (an older agent, or an
// unreachable one, must not mark a version as missing).
function notHostedHere(id) {
  const e = S.srv.versions && S.srv.versions[id];
  return !!(S.serverConfigured && S.srv.state === 'ok' && e && e.present === false);
}

// ── library ──
function library() {
  const sel = selected();
  if (!sel) return `<div class="pad"><h1 class="title">${T('lib.title')}</h1><p class="sub">${T('lib.noCatalog')}</p></div>`;
  const selInstalled = isInstalled(sel.id);
  const cards = S.versions.map((v) => {
    const inst = isInstalled(v.id);
    const isSel = v.id === S.selectedVersionId;
    const cta = !inst
      ? `<div class="cta install">${I.download} ${T('lib.card.install')}</div>`
      : (isSel ? `<div class="cta sel">${I.check} ${T('lib.card.selected')}</div>` : `<div class="cta pick">${T('lib.card.select')}</div>`);
    // On an installed version, "Installed" alone tells the user nothing useful: the game account is
    // the information they actually need later, so it sits on the card next to the install state.
    const acc = inst && accountOf(v.id)
      ? `<small class="accline">${I.userSm} ${T('lib.ingameAccount')} <b class="mono">${esc(accountOf(v.id))}</b></small>` : '';
    // The server serves the official 2021 hotpatch: the client fetches the fixes once at login, so
    // the player learns here why the first login of this version shows a download.
    const hp = hotpatchServed(v.id)
      ? `<small class="accline" title="${T('lib.hotpatchTitle')}"><span class="dot" style="background:var(--ok)"></span> ${T('lib.hotpatchServed')}</small>` : '';
    // Before a multi-GB download (or a PLAY that can never connect): the server has no stack for it.
    const nh = notHostedHere(v.id)
      ? `<small class="accline" style="color:var(--muted)"><span class="dot" style="background:var(--warn)"></span> ${T('lib.card.notHosted')}</small>` : '';
    return `<div class="vcard ${isSel ? 'sel' : ''}" data-act="selectVersion" data-arg="${v.id}">
      <div class="thumb" style="background-image:url('${heroImg(v.id)}')"><span class="st"><span class="dot" style="background:${inst ? 'var(--ok)' : '#d06a5e'}"></span>${inst ? T('lib.card.installed') : T('lib.card.new')}</span>${inst && eeWanted(v.id) ? `<span class="st ee">${T('lib.card.enhancements')}</span>` : ''}<span class="vid">${esc(v.id)}</span></div>
      <div class="body"><b>${esc(v.title)}</b><small>${esc(v.desc)}</small><small class="sz">${esc(v.size)}</small>${acc}${hp}${nh}${cta}</div>
    </div>`;
  }).join('');

  const playBtn = selInstalled
    ? `<button class="btn big" data-act="play" data-arg="${sel.id}">${I.play} ${T('lib.play')}</button>`
    : `<button class="btn big" data-act="selectVersion" data-arg="${sel.id}">${I.download} ${T('lib.installBig')}</button>`;
  // Per-version removal: a launcher install is uninstalled (files deleted by default), an imported
  // folder is only unregistered unless the user opts in to deleting it (the confirm dialog asks).
  const removeBtn = selInstalled
    ? `<button class="btn dark" data-act="removeVersion" data-arg="${sel.id}" title="${T('lib.remove.title')}">${T('lib.remove.button')}</button>` : '';

  return `<div>
    <div class="hero" style="background-image:url('${isSummer() ? summerHero(sel.id) : heroImg(sel.id)}')"><div class="inner">
      <div class="kicker">${T('lib.hero.kicker')}</div>
      <h2>${T('common.versionN', { id: sel.id })}</h2>
      <div class="flex center gap wrap">
        <span class="badge"><span class="dot" style="background:${selInstalled ? 'var(--ok)' : 'var(--warn)'}"></span>${selInstalled ? T('lib.card.installed') : T('lib.hero.notInstalled')}</span>
        ${srvBadge()}
        ${selInstalled && accountOf(sel.id) ? `<span class="badge">${I.userSm} ${T('lib.ingameAccount')} <b class="mono" style="color:var(--gold2)">${esc(accountOf(sel.id))}</b></span>` : ''}
        ${selInstalled && eeWanted(sel.id) ? `<span class="badge"><span class="dot" style="background:var(--gold)"></span>${T('lib.hero.enhancements')}</span>` : ''}
        ${selInstalled && installedVoices(sel.id).length ? `<span class="badge">${T('lib.hero.voices', { langs: installedVoices(sel.id).join(', ') })}</span>` : ''}
        ${hotpatchServed(sel.id) ? `<span class="badge" title="${T('lib.hotpatchTitle')}"><span class="dot" style="background:var(--ok)"></span>${T('lib.hotpatchServed')}</span>` : ''}
        <span class="badge">${I.stack.replace('width="19" height="19"', 'width="13" height="13"')}${esc(sel.size)}</span>
      </div>
      <div class="flex center gap" style="margin-top:6px">${playBtn}
        <button class="btn dark" data-act="go" data-arg="server">${T('lib.checkServer')}</button>${removeBtn}</div>
    </div></div>
    <div class="pad">
      ${selInstalled ? accountSect(sel.id) : ''}
      <div class="flex between center" style="margin-bottom:16px"><div class="serif" style="font-weight:700;font-size:19px">${T('lib.yourVersions')}</div>
        <span style="color:var(--muted);font-size:12.5px;font-weight:700">${T('lib.installedCount', { n: S.installed.length })}</span></div>
      <div class="grid3">${cards}
        <button class="addcard" data-act="go" data-arg="install"><span style="width:52px;height:52px;border-radius:50%;background:color-mix(in srgb, var(--gold) 16%, transparent);display:flex;align-items:center;justify-content:center;color:var(--goldD)"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></span>${T('lib.installAnother')}</button>
      </div>
      ${selInstalled ? voicesSect(sel.id) : ''}
    </div>
  </div>`;
}

// ── install wizard ──
// The step list is the one place the count lives: the stepper, the Continue/START switch, wizNext's
// clamp and the dev autoinstall all read LAST — a step added here used to leave one of them at a
// hard-coded 4, i.e. an unreachable step or a START button one step early.
const STEPS = ['wizard.steps.version', 'wizard.steps.isolation', 'wizard.steps.source', 'wizard.steps.voices', 'wizard.steps.fiddler', 'wizard.steps.shortcut'];
const LAST = STEPS.length - 1;
// Last step DRAWN. Every keystroke in a wizard field redraws the whole screen, so the panel's entry
// animation cannot be tied to render, only to the step change: on any change (forward or back) the
// key is forgotten so the new step animates in again.
let wizStep = -1;
function install() {
  const step = S.installStep;
  if (wizStep !== step) { wizStep = step; onceForget('wiz:'); }
  const stepper = STEPS.map((key, i) => {
    const cls = i < step ? 'done' : (i === step ? 'active' : 'todo');
    const inner = i < step ? I.check : (i + 1);
    return `<div class="step"><div class="flex center" style="gap:9px;cursor:pointer" data-act="wizGoto" data-arg="${i}"><span class="num ${cls}">${inner}</span><span class="lbl">${T(key)}</span></div>${i < LAST ? '<span class="bar"></span>' : ''}</div>`;
  }).join('');

  return `<div class="pad">
    <h1 class="title">${T('wizard.title')}</h1>
    <p class="sub">${T('wizard.sub')}</p>
    <div class="stepper">${stepper}</div>
    <div class="panel" style="${once('wiz:' + step, 'rl-fadein', .26)}">${wizardStep(step)}</div>
    <div class="wizfoot">
      ${step > 0 ? `<button class="btn ghost" data-act="wizPrev">${T('common.back')}</button>` : '<span></span>'}
      ${step < LAST ? `<button class="btn" data-act="wizNext">${T('common.continue')}</button>`
        : `<button class="btn glow" style="font-family:'Cinzel',serif;letter-spacing:1px" data-act="startInstall">${T('wizard.start')}</button>`}
    </div>
  </div>`;
}

// Mirrors come from the catalogue, not from code: each version may have different alternative hosts
// (1.6 has Internet Archive, 2.8 not yet), and the id is what gets saved in the settings.
const mirrorsFor = (versionId) => (S.versions.find((v) => v.id === versionId) || {}).mirrors || [];
const allMirrors = () => [...new Map(S.versions.flatMap((v) => v.mirrors || []).map((m) => [m.id, m])).values()];
// Label of a source. Unknown → "Official CDN", because that is exactly what the backend does with a
// mirror id the chosen version does not have: it stays on the default order, CDN first.
const sourceLabel = (id) => (id === 'drive' ? 'Google Drive'
  : id === 'local' ? t('wizard.source.local')
  : (allMirrors().find((m) => m.id === id) || {}).label || t('wizard.source.cdn'));

function wizardStep(step) {
  const w = S.wiz;
  if (step === 0) {
    // v.size = the game + the English voices; with more packs to pick from at the Voices step it is
    // a "from" figure, not the total.
    const opts = S.versions.map((v) => `<div class="opt center ${w.versionId === v.id ? 'sel' : ''}" data-act="pickVersion" data-arg="${v.id}">
      <div class="vbig">${esc(v.id)}</div><div style="color:var(--muted);font-size:11.5px;font-weight:700;margin-top:4px">${esc(v.desc)}</div>
      <div style="color:var(--inkLbl);font-size:11.5px;font-weight:800;margin-top:8px">${voicesFor(v.id).length > 1 ? T('wizard.step.version.sizeFrom', { size: v.size }) : esc(v.size)}</div></div>`).join('');
    // Non-blocking: the version may still be wanted for another server.
    const nh = w.versionId && notHostedHere(w.versionId)
      ? `<p class="hint" style="margin:10px 0 0">${T('install.notHostedHint', { id: w.versionId })}</p>` : '';
    return `<h3>${T('wizard.step.version.title')}</h3><p class="hint">${T('wizard.step.version.hint')}</p><div class="grid3">${opts}</div>${nh}`;
  }
  if (step === 1) {
    return `<h3>${T('wizard.step.isolation.title')}</h3><p class="hint">${T('wizard.step.isolation.hint')}</p>
      <div class="opt sel"><div class="h">${I.user} ${T('wizard.step.isolation.optTitle')}</div>
      <p>${T('wizard.step.isolation.optBody')}</p></div>`;
  }
  if (step === 2) {
    const mirrors = mirrorsFor(w.versionId || S.selectedVersionId);
    // A saved source this version does NOT have (e.g. "archive" on 2.8) is not an error: the backend
    // stays on the default order, CDN first, so that is exactly what we show as selected.
    const known = ['cdn', 'drive', 'local', ...mirrors.map((m) => m.id)];
    const eff = known.includes(w.source) ? w.source : 'cdn';
    const card = (id, icon, title, note) => `<div class="opt ${eff === id ? 'sel' : ''}" data-act="setSource" data-arg="${esc(id)}">
        <div class="h">${icon} ${esc(title)}</div><p>${esc(note)}</p></div>`;
    // Google Drive is offered only when the catalogue carries an id for this version (a private
    // catalogue may; the public build ships none — the card stays hidden, the chain skips it).
    const hasDrive = !!(versionById(w.versionId || S.selectedVersionId) || {}).hasDrive;
    const cards = [
      card('cdn', I.download, t('wizard.source.cdn'), t('wizard.source.cdnNote')),
      ...(hasDrive ? [card('drive', I.download, 'Google Drive', t('wizard.source.driveNote'))] : []),
      ...mirrors.map((m) => card(m.id, I.download, m.label, m.note || t('wizard.source.mirrorNote'))),
      card('local', I.folder, t('wizard.source.local'), t('wizard.source.localNote')),
    ];
    return `<h3>${T('wizard.step.source.title')}</h3><p class="hint">${T('wizard.step.source.hint')}</p>
      <div class="${cards.length > 3 ? 'grid2' : 'grid3'}">${cards.join('')}</div>
      ${eff !== w.source ? `<p class="hint" style="margin:10px 0 0">${T('wizard.step.source.missing', { version: w.versionId || S.selectedVersionId || '', source: sourceLabel(w.source) })}</p>` : ''}
      ${w.source === 'local' ? `
      <label class="fld">${T('wizard.step.source.localFolder')}</label>
      <div class="flex gap"><input class="txt mono" data-model="wiz.localPath" value="${esc(w.localPath)}" placeholder="${T('wizard.step.source.localPlaceholder')}"/><button class="btn ghost" data-act="pickLocalFolder">${T('common.browse')}</button></div>
      <p class="hint" style="margin:10px 0 0">${T('wizard.step.source.localHint')}</p>` : ''}`;
  }
  if (step === 3) {
    const id = w.versionId || S.selectedVersionId;
    // A folder already on disk: the packs are detected when it is registered; nothing to pick here.
    if (w.source === 'local') return `<h3>${T('wizard.step.voices.title')}</h3><p class="hint">${T('wizard.step.voices.localHint')}</p>`;
    const packs = voicesFor(id);
    const ticked = selectedVoicePacks(id);
    const rows = packs.map((p) => `<div class="line"><div><b>${esc(p.lang)}</b><small>${esc(p.sizeText || sizeGb(p.size))}</small></div>
      <button class="toggle ${ticked.includes(p) ? 'on' : ''}" data-act="wizVoiceToggle" data-arg="${esc(p.lang)}"><span class="knob"></span></button></div>`).join('');
    const client = Math.max(0, Number((versionById(id) || {}).clientSize)) || 0, voices = packBytes(ticked);
    // No packs = an older catalogue / backend: the hint alone, no "0.0 GB" total to puzzle over.
    return `<h3>${T('wizard.step.voices.title')}</h3><p class="hint">${T('wizard.step.voices.hint')}</p>
      ${rows ? `<div class="sect" style="padding:2px 18px;margin-bottom:0">${rows}</div>
      <p class="hint" style="margin:14px 0 0">${T('wizard.step.voices.total', { total: gb(client + voices), client: gb(client), voices: gb(voices) })}</p>` : ''}`;
  }
  if (step === 4) {
    return `<h3>${T('wizard.step.fiddler.title')}</h3><p class="hint">${T('wizard.step.fiddler.hint')}</p>
      <div class="row" style="margin-bottom:12px"><div><b>${T('common.decryptHttps')}</b><small>${T('wizard.step.fiddler.decryptNote')}</small></div>${toggle('decrypt', w.decrypt)}</div>
      <div class="row" style="margin-bottom:12px"><div><b>${T('wizard.step.fiddler.cert')}</b><small>${T('wizard.step.fiddler.certNote')}</small></div>${toggle('cert', w.cert)}</div>
      <label class="fld">${T('wizard.step.fiddler.serverAddress')}</label>
      <input class="txt mono" data-model="addrEdit" value="${esc(addrShown())}" placeholder="${T('login.serverPlaceholder')}" spellcheck="false" autocomplete="off"/>`;
  }
  // step LAST (5): shortcut + summary
  const name = w.shortcutName || t('wizard.step.finish.defaultShortcut', { version: w.versionId || S.selectedVersionId || '' });
  const isLocal = w.source === 'local';
  // The label must name the host the download will ACTUALLY use: if the chosen version does not have
  // the mirror saved in settings, the backend starts from the CDN — the summary would lie otherwise.
  const srcAvailable = w.source === 'cdn' || w.source === 'drive'
    || mirrorsFor(w.versionId || S.selectedVersionId).some((m) => m.id === w.source);
  const srcLabel = isLocal ? t('wizard.source.local') : sourceLabel(srcAvailable ? w.source : 'cdn');
  // The languages the download will bring, with the whole figure (game + packs): the number the user
  // must have seen before START, not at "Not enough space" after a 30 GB download.
  const packs = isLocal ? [] : selectedVoicePacks(w.versionId || S.selectedVersionId);
  const voicesRow = packs.length
    ? `<div>${T('wizard.step.finish.sumVoices')} <b>${esc(packs.map((p) => p.lang).join(', '))}</b> · ${esc(sizeGb((Math.max(0, Number((versionById(w.versionId || S.selectedVersionId) || {}).clientSize)) || 0) + packBytes(packs)))}</div>` : '';
  const location = isLocal
    ? `<label class="fld" style="margin-top:0">${T('wizard.step.finish.localFolder')}</label>
       <div style="margin-bottom:14px"><input class="txt mono" value="${esc(w.localPath || '—')}" disabled/></div>`
    : `<label class="fld" style="margin-top:0">${T('wizard.step.finish.location')}</label>
       <div class="flex gap" style="margin-bottom:14px"><input class="txt mono" data-model="settings.installRoot" value="${esc(S.settings.installRoot)}"/><button class="btn ghost" data-act="pickFolder">${T('common.browse')}</button></div>`;
  return `<h3>${T('wizard.step.finish.title')}</h3><p class="hint">${isLocal ? T('wizard.step.finish.hintLocal') : T('wizard.step.finish.hint')}</p>
    ${location}
    <div class="row" style="margin-bottom:14px"><div class="flex center gap"><b>${T('wizard.step.finish.desktopShortcut')}</b></div>${toggle('shortcut', w.shortcut)}</div>
    ${w.shortcut ? `<label class="fld">${T('wizard.step.finish.shortcutName')}</label><input class="txt" data-model="wiz.shortcutName" value="${esc(name)}"/>` : ''}
    <div class="summary" style="margin-top:16px"><div class="k">${T('wizard.step.finish.summary')}</div><div class="g">
      <div>${T('wizard.step.finish.sumVersion')} <b>${esc(w.versionId || '—')}</b></div><div>${T('wizard.step.finish.sumSource')} <b>${esc(srcLabel)}</b></div>
      ${voicesRow}
      <div>${T('wizard.step.finish.sumIsolation')} <b>${T('wizard.step.finish.sumIsolationValue')}</b></div><div>${T('wizard.step.finish.sumServer')} <b class="mono">${esc(S.addrEdit != null && S.addrEdit.trim() !== addrText() ? S.addrEdit.trim() : `${S.serverAddr.host}:${S.serverAddr.port}`)}</b></div>
      ${accountOf(w.versionId) ? `<div>${T('wizard.step.finish.sumAccount')} <b class="mono">${esc(accountOf(w.versionId))}</b></div><div>${T('wizard.step.finish.sumPassword')} <b>${esc(passwordWord(w.versionId))}</b></div>` : ''}
    </div></div>`;
}

// ── animation continuity across render() ──
// render() rewrites all of #app, and a CSS animation starts from zero on every NEW element: the ring
// jumped back to 0° on every percent and the waves started over. The clock is shared
// (performance.now), so a negative animation-delay equal to the current phase makes the fresh element
// continue exactly where the one it replaced left off. It goes AFTER any inline `animation:` — the
// shorthand resets the delay, the longhand after it wins.
const animNow = () => performance.now() / 1000;
const phaseOf = (dur, off) => (-((animNow() + (off || 0)) % dur)).toFixed(3) + 's';
// "Once" animations (the box entering, the note appearing) have the same problem in reverse: a stray
// render() (a toast, the server status poll) would restart them endlessly. Remember the moment of
// the first appearance and, once consumed, emit nothing.
const ONCE = new Map();
function once(key, name, dur, ease) {
  if (!ONCE.has(key)) ONCE.set(key, animNow());
  const e = animNow() - ONCE.get(key);
  return e >= dur ? '' : `animation:${name} ${dur}s ${ease || 'ease'} both;animation-delay:${(-e).toFixed(3)}s;`;
}
const onceForget = (prefix) => { ONCE.forEach((v, k) => { if (k.indexOf(prefix) === 0) ONCE.delete(k); }); };
// The dark backdrop fades in once, when the install overlay appears — NOT on every stage change
// (progress → paused → done), otherwise the screen would flicker under the box.
// The box, by contrast, enters on every stage: it is a new sheet with different content.
const ovIn = () => once('inst:ov', 'rl-fade', .22);
const boxIn = (variant) => once('inst:box:' + variant, 'rl-boxin', .34, 'cubic-bezier(.2,.7,.2,1)');

// ── progress, interpolated ──
// The backend reports in jumps (a package downloaded, a file extracted). `shown` tracks `target`
// exponentially, frame by frame, so the bar, the buoy on the wave and the percent figure move
// smoothly and at the same pace however rarely or irregularly the events arrive. The displayed
// value lives HERE, not in the DOM: a structural render() (the extract note appears, the buttons
// change) redraws the bar straight at `shown`, without jumping back.
const PROG = { shown: 0, target: 0, raf: 0, last: 0 };
function resetProgress() { PROG.shown = 0; PROG.target = 0; PROG.last = 0; }
function paintProgress() {
  const w = (PROG.shown * 100).toFixed(2) + '%';
  document.querySelectorAll('[data-fill]').forEach((el) => { el.style.width = w; });
  document.querySelectorAll('[data-buoy]').forEach((el) => { el.style.left = w; });
  const txt = Math.round(PROG.shown * 100) + '%';
  document.querySelectorAll('[data-pct]').forEach((el) => { if (el.textContent !== txt) el.textContent = txt; });
}
function progTick(ts) {
  PROG.raf = 0;
  if (!document.querySelector('[data-fill]')) { PROG.last = 0; return; } // the overlay is gone
  // The step is computed from real time, not from the frame count: a dropped frame (extraction
  // really loads the machine) no longer slows the tracking down.
  const dt = PROG.last ? Math.min(.1, (ts - PROG.last) / 1000) : 1 / 60;
  PROG.last = ts;
  PROG.shown += (PROG.target - PROG.shown) * (1 - Math.pow(.004, dt));
  if (Math.abs(PROG.target - PROG.shown) < 0.0004) PROG.shown = PROG.target;
  paintProgress();
  if (PROG.shown !== PROG.target) PROG.raf = requestAnimationFrame(progTick); else PROG.last = 0;
}
function syncProgress() {
  const t = Math.max(0, Math.min(1, S.installFraction || 0));
  // Interpolation goes FORWARD only; a decrease is applied directly. The fraction the backend reports
  // is per PHASE, not per whole install (client 0→1, then voices 0→1, then extraction 0→1, then the
  // Patch/Fiddler/Shortcut tail) — so at every phase change it starts again from ~0. Interpolated,
  // that drop would slowly empty the bar from the right for ~1.4 s and look exactly like work being
  // undone; jumped, it reads as "new stage", as it did before.
  if (t < PROG.shown) PROG.shown = t;
  PROG.target = t;
  if (!document.querySelector('[data-fill]')) {
    if (PROG.raf) { cancelAnimationFrame(PROG.raf); PROG.raf = 0; }
    PROG.last = 0; return;
  }
  paintProgress();
  if (!PROG.raf && PROG.shown !== PROG.target) { PROG.last = 0; PROG.raf = requestAnimationFrame(progTick); }
}

// The spinner and the progress bar have a themed shape: classic = the circle + the golden bar as
// before, archipelago = the spinning lifebuoy + the wave bar (markup chosen here, styles in CSS).
function spinnerHtml() {
  return isSummer()
    ? `<span class="lifebuoy" style="animation-delay:${phaseOf(2.2)}"></span>`
    : `<span style="animation:rl-spin 1s linear infinite;animation-delay:${phaseOf(1)};color:var(--goldD)"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.2-8.5"/></svg></span>`;
}
// Widths are drawn from PROG.shown (not S.installFraction): the fresh markup continues from where the
// old drawing was, and the rAF loop takes over from there.
function progressHtml() {
  const w = (Math.max(0, Math.min(1, PROG.shown)) * 100).toFixed(2) + '%';
  // The sheen is an ELEMENT, not a ::before/::after: a pseudo-element cannot receive phaseOf()'s
  // inline animation-delay, so it would have been the only thing in the overlay still restarting.
  if (!isSummer()) return `<div class="pbar"><i data-fill style="width:${w}"><b class="sheen" style="animation-delay:${phaseOf(5.4)}"></b></i></div>`;
  return `<div class="wavewrap"><span class="buoy" data-buoy style="left:${w};animation-delay:${phaseOf(1.3)}"></span>
    <div class="wavebar"><div class="fill" data-fill style="width:${w}">
      <i class="back" style="animation-delay:${phaseOf(5.6)},${phaseOf(6.9)}"></i>
      <i class="mid" style="animation-delay:${phaseOf(3.4)},${phaseOf(5.1)}"></i>
      <i class="front" style="animation-delay:${phaseOf(2.1)},${phaseOf(4.3)}"></i>
      <i class="sheen" style="animation-delay:${phaseOf(6.2)}"></i>
    </div></div></div>`;
}

// Pause only while the work is resumable by design: the downloads ('' = the pre-download probe).
// Extract can only be cancelled — the downloaded zips stay, but extraction restarts on retry.
// From Fiddler on the version is registered and the backend no longer honors cancellation.
// A local import has nothing to "resume" — Pause would lead to an overlay about downloads; Cancel only.
function installGuards() {
  const canPause = !S.activeInstallLocal
    && (!S.installPhase || S.installPhase === 'DownloadClient' || S.installPhase === 'DownloadAudio');
  return { canPause, canCancel: canPause || S.installPhase === 'Extract' };
}
// Signature of everything that CANNOT be patched in place in the progress overlay (title, buttons,
// the extract note, the theme-dictated shape). While it does not change, a progress event only sets
// the new text and leaves the DOM — and therefore the animations — alone. Changes → full render().
function installLiveKey() {
  const g = installGuards();
  return [S.activeInstallId, S.activeInstallVoices ? 'v' : '-', isSummer() ? 's' : 'c', S.installPhase === 'Extract' ? 'x' : '-',
    g.canPause ? 'p' : '-', g.canCancel ? 'c' : '-', S.installStopIntent || '-'].join('|');
}
function patchInstallLive() {
  if (!S.installing || S.installError || S.installDone || S.installPaused) return false;
  const box = document.querySelector('[data-live="install"]');
  if (!box || box.getAttribute('data-key') !== installLiveKey()) return false;
  const m = box.querySelector('[data-live="msg"]');
  const txt = S.installMsg || '...';
  if (m && m.textContent !== txt) m.textContent = txt;
  return true; // the percent is moved by the tween in syncProgress()
}

function installOverlay() {
  if (S.installError) {
    return `<div class="overlay" style="${ovIn()}"><div class="box" style="text-align:center;${boxIn('error')}">
      <div style="width:60px;height:60px;border-radius:50%;margin:0 auto 14px;background:rgba(208,106,94,.15);display:flex;align-items:center;justify-content:center">${I.warn}</div>
      <div class="serif" style="font-weight:700;font-size:19px;color:#8a3a2a">${T('install.failed')}</div>
      <p style="color:#7a5040;font-size:12.5px;font-weight:600;margin:8px 0 20px;word-break:break-word">${esc(S.installError)}</p>
      <button class="btn ghost" data-act="finishInstall">${T('common.close')}</button></div></div>`;
  }
  if (S.installDone) {
    // The last install screen is also the only moment someone is certainly looking: the game's login
    // details go here, otherwise the player reaches the client's account screen without knowing what
    // to type and thinks they have to create one. They remain in the Library afterwards.
    const id = S.activeInstallId;
    const check = `<div style="width:66px;height:66px;border-radius:50%;margin:0 auto 16px;background:linear-gradient(180deg,var(--gold2),var(--gold));display:flex;align-items:center;justify-content:center;box-shadow:0 0 30px color-mix(in srgb, var(--gold) 50%, transparent);color:var(--btnInk)">${I.check.replace('15','34')}</div>`;
    // A voice pack added to an installed version: no login details (the player has them already) —
    // the one thing to say is where the language is picked (in the game's settings).
    if (S.activeInstallVoices) {
      return `<div class="overlay" style="${ovIn()}"><div class="box" style="text-align:center;${boxIn('done')}">
        ${check}
        <div class="serif" style="font-weight:700;font-size:20px">${T('install.voices.done.title')}</div>
        <p style="color:var(--inkSoft);font-size:13px;font-weight:600;margin:8px 0 20px">${T('install.voices.done.ready', { langs: S.activeInstallVoices.join(', '), id })}</p>
        <button class="btn" data-act="finishInstall">${T('common.close')}</button></div></div>`;
    }
    const acc = accountOf(id);
    return `<div class="overlay" style="${ovIn()}"><div class="box" style="text-align:center;${boxIn('done')}">
      ${check}
      <div class="serif" style="font-weight:700;font-size:20px">${T('install.done.title')}</div>
      <p style="color:var(--inkSoft);font-size:13px;font-weight:600;margin:8px 0 ${acc ? '18px' : '20px'}">${T('install.done.ready', { id })}${
        acc ? ' ' + T('install.done.oneMore') : ''}</p>
      ${acc ? `<div style="text-align:left">
        <div class="credhead">${T('install.done.credsHead')}</div>
        ${credsBox(id)}
        <p style="color:var(--inkSoft);font-size:12px;font-weight:600;line-height:1.6;margin:12px 0 18px">
          ${tf('install.done.credsNote')}</p>
      </div>` : ''}
      <button class="btn" data-act="finishInstall">${T('install.done.openLibrary')}</button></div></div>`;
  }
  if (S.installPaused) {
    return `<div class="overlay" style="${ovIn()}"><div class="box" style="text-align:center;${boxIn('paused')}">
      <div style="width:60px;height:60px;border-radius:50%;margin:0 auto 14px;background:color-mix(in srgb, var(--gold) 16%, transparent);display:flex;align-items:center;justify-content:center;color:var(--goldD)"><svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1.4"/><rect x="14" y="4" width="4" height="16" rx="1.4"/></svg></div>
      <div class="serif" style="font-weight:700;font-size:19px">${T('install.paused.title')}</div>
      <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;margin:8px 0 18px">${T('install.paused.note')}</p>
      ${progressHtml()}
      <div data-pct style="text-align:right;color:var(--goldD);font-weight:800;font-size:12px;margin:6px 0 20px">${Math.round(PROG.shown * 100)}%</div>
      <div class="flex center gap" style="justify-content:center">
        <button class="btn" data-act="resumeInstall">${T('install.paused.resume')}</button>
        <button class="btn ghost" data-act="abandonInstall">${T('common.giveUp')}</button>
      </div></div></div>`;
  }
  const { canPause, canCancel } = installGuards();
  const stopping = !!S.installStopIntent;
  const controls = canCancel ? `<div class="flex center gap" style="justify-content:flex-end;margin-top:18px">
      ${canPause ? `<button class="btn ghost" data-act="pauseInstall" ${stopping ? 'disabled' : ''}>${S.installStopIntent === 'pause' ? T('install.pausing') : T('install.pause')}</button>` : ''}
      <button class="btn ghost" data-act="cancelInstall" ${stopping ? 'disabled' : ''}>${S.installStopIntent === 'cancel' ? T('install.cancelling') : T('common.cancel')}</button>
    </div>` : '';
  // min-height on the message row: the texts have 1 or 2 lines, and without it the (centred) box
  // changed height at every step and everything below it jumped by a few pixels.
  const title = S.activeInstallVoices
    ? T('install.voices.installing', { id: S.activeInstallId, langs: S.activeInstallVoices.join(', ') })
    : T('install.installing', { id: S.activeInstallId });
  return `<div class="overlay" style="${ovIn()}" data-live="install" data-key="${esc(installLiveKey())}"><div class="box" style="${boxIn('run')}">
    <div class="flex center gap" style="margin-bottom:6px">${spinnerHtml()}<div class="serif" style="font-weight:700;font-size:18px">${title}</div></div>
    <div data-live="msg" style="color:var(--ink2);font-size:13px;font-weight:700;margin:2px 0 16px;min-height:38px;line-height:1.45">${esc(S.installMsg || '...')}</div>
    ${progressHtml()}
    <div data-pct style="text-align:right;color:var(--goldD);font-weight:800;font-size:12.5px;margin-top:6px">${Math.round(PROG.shown * 100)}%</div>
    ${S.installPhase === 'Extract' ? `<div style="margin-top:14px;padding:10px 12px;border-radius:10px;background:color-mix(in srgb, var(--gold) 10%, transparent);color:var(--inkSoft);font-size:11.5px;font-weight:600;line-height:1.5;${once('inst:note', 'rl-fadein', .32)}">
      ${T('install.extractNote')}</div>` : ''}
    ${controls}
  </div></div>`;
}

// ── commands ──
// name/desc are translation keys (translated when the card is drawn).
const CATS = [
  { id: 'exp', name: 'cmd.cat.exp.name', desc: 'cmd.cat.exp.desc', icon: I.star, color: '#c98b1e', bg: 'rgba(232,176,74,.14)', ph: '10000' },
  { id: 'character', name: 'cmd.cat.character.name', desc: 'cmd.cat.character.desc', icon: I.user, color: '#2b8fc4', bg: 'rgba(76,178,230,.14)', ph: '10000002' },
  { id: 'artifact', name: 'cmd.cat.artifact.name', desc: 'cmd.cat.artifact.desc', icon: CMDICON.artifact, color: '#8a5fc4', bg: 'rgba(180,142,224,.16)', ph: '71544 20' },
  { id: 'weapon', name: 'cmd.cat.weapon.name', desc: 'cmd.cat.weapon.desc', icon: CMDICON.weapon, color: '#c9462e', bg: 'rgba(228,102,78,.14)', ph: '13501' },
  { id: 'level', name: 'cmd.cat.level.name', desc: 'cmd.cat.level.desc', icon: I.up, color: '#5f8a24', bg: 'rgba(143,191,80,.16)', ph: '60' },
  { id: 'mora', name: 'cmd.cat.mora.name', desc: 'cmd.cat.mora.desc', icon: I.coin, color: '#9b7d3a', bg: 'rgba(200,160,79,.16)', ph: '100000' },
  { id: 'primogems', name: 'cmd.cat.primogems.name', desc: 'cmd.cat.primogems.desc', icon: I.diamond, color: '#5a7fc4', bg: 'rgba(90,127,196,.14)', ph: '10000' },
  { id: 'genesis', name: 'cmd.cat.genesis.name', desc: 'cmd.cat.genesis.desc', icon: I.atom, color: '#8a5fc4', bg: 'rgba(180,142,224,.14)', ph: '5000' },
];
// The plain-number cards (exp/level/mora/primogems/genesis) stay simple; the other three are the
// raw-id inputs, used only by the manual section and by the fallback when the catalogue is missing.
const NUM_CATS = ['exp', 'level', 'mora', 'primogems', 'genesis'];

// Skins are ITEMS (item add <id> 1 → the item module calls addCostume — see GmCommands). The ids come
// from AvatarCostumeExcelConfigData of the dumps the catalogue is built on; the character mapping is
// by hand, because the 2.8 dumps have that field's name obfuscated. The list is frozen: 1.6 and 2.8
// never receive new skins.
const COSTUMES = [
  { id: 340000, char: 'Barbara', name: 'Summertime Sparkle', since: '1.6' },
  { id: 340001, char: 'Jean', name: 'Sea Breeze Dandelion', since: '1.6' },
  { id: 340002, char: 'Ningguang', name: "Orchid's Evening Gown", since: '2.8' },
  { id: 340003, char: 'Keqing', name: 'Opulent Splendor', since: '2.8' },
  { id: 340004, char: 'Diluc', name: 'Red Dead of Night', since: '2.8' },
  { id: 340005, char: 'Fischl', name: 'Ein Immernachtstraum', since: '2.8' },
  { id: 340006, char: 'Jean', name: "Gunnhildr's Legacy", since: '2.8' },
  { id: 340007, char: 'Amber', name: '100% Outrider', since: '2.8' },
  { id: 340008, char: 'Mona', name: 'Pact of Stars and Moon', since: '2.8' },
  { id: 340009, char: 'Rosaria', name: "To the Church's Free Spirit", since: '2.8' },
];
const ID_CATS = ['character', 'weapon', 'artifact'];

function cmdCards(ids) {
  return ids.map((id) => CATS.find((c) => c.id === id)).filter(Boolean).map((c) => `<div class="cmdcard">
    <div class="top"><span class="ic" style="background:${c.bg};color:${c.color}">${c.icon}</span><div><b>${T(c.name)}</b><br><small>${T(c.desc)}</small></div></div>
    <div class="in"><input data-model="cmd.${c.id}" value="${esc(S.cmd[c.id])}" placeholder="${c.ph}"/><button class="send" data-act="sendCmd" data-arg="${c.id}">${T('common.send')}</button></div>
  </div>`).join('');
}

function commands() {
  const body = S.gd.status === 'ok'
    ? avatarHero() + `<div class="grid2" style="margin-top:16px">${weaponSlot()}${artifactSlot()}</div>`
    : catalogFallback();
  return `<div class="pad">
    ${cmdHeader()}
    ${cmdHelp()}
    ${body}
    ${ingameCard()}
    ${costumeCard()}
    <div class="cmdgrid" style="margin-top:16px">${cmdCards(NUM_CATS)}</div>
    ${rawSection()}
    ${cmdConsole()}
  </div>`;
}

// ── commands: player instructions ──
// Collapsed by default but always in sight: the rules that are otherwise learned only from failed
// attempts (UID + online required, where the items land, what equipped mode means).
function cmdHelp() {
  const head = `<button class="btn ghost" style="padding:9px 14px;font-size:12px" data-act="toggleHelp">
      ${S.helpOpen ? '▾' : '▸'} ${T('cmd.help.toggle')}</button>`;
  if (!S.helpOpen) return `<div style="margin-bottom:16px">${head}</div>`;
  const li = (html) => `<li style="margin:0 0 9px;line-height:1.55">${html}</li>`;
  return `<div style="margin-bottom:16px">${head}
    <div class="sect" style="margin-top:12px;margin-bottom:0"><h4>${T('cmd.help.title')}</h4>
      <ul style="margin:0;padding-left:20px;color:var(--inkSoft);font-size:12.5px;font-weight:600">
        ${li(tf('cmd.help.online'))}
        ${li(tf('cmd.help.uid'))}
        ${li(tf('cmd.help.characters'))}
        ${li(tf('cmd.help.weapons'))}
        ${li(tf('cmd.help.sets'))}
        ${li(tf('cmd.help.ingame'))}
        ${li(tf('cmd.help.c6'))}
        ${li(tf('cmd.help.setCount'))}
      </ul>
    </div></div>`;
}

// ── commands: the character controlled IN GAME (not the one chosen in the catalogue above) ──
// Commands verified in the gameserver binary (2026-08-13): "break <n>" (setBreakLevel) and "level <n>"
// (setLevel, registered right next to it) work on the current character, "stamina infinite on/off"
// is literal in the binary, and invincibility is setWudi — "wudi global avatar on/off" from the GIO
// guide. Each ascension level has its own cap, so break + level are sent together.
// "talent unlock all" (procTalent / forceUnlockAllTalent in the binary, verified 2026-08-14) unlocks
// all constellations — C6. The server calls the constellation "talent"; unrelated to the talents
// (abilities) of the game UI, and a per-chosen-character form does not exist: the command works on
// the controlled character too, like break/level.
const PROMOTE_LEVEL_CAP = [20, 40, 50, 60, 70, 80, 90];
function ingameCard() {
  const promote = clampInt(S.ingame.promote, 0, 6, 6);
  const dis = S.sending ? 'disabled' : '';
  const onoff = (label, act) => `<div>
      <label class="fld" style="margin:0 0 5px">${label}</label>
      <div class="flex" style="gap:8px">
        <button class="btn" style="padding:10px 16px;font-size:12.5px" data-act="${act}" data-arg="on" ${dis}>${T('cmd.ingame.on')}</button>
        <button class="btn ghost" style="padding:10px 16px;font-size:12.5px" data-act="${act}" data-arg="off" ${dis}>${T('cmd.ingame.off')}</button>
      </div></div>`;
  return `<div class="sect" style="margin-top:16px;margin-bottom:0">
    <div class="flex between center wrap" style="gap:10px;margin-bottom:4px">
      <h4 style="margin:0">${T('cmd.ingame.title')}</h4>
      <span class="vchip">${T('cmd.ingame.chip')}</span>
    </div>
    <p class="hint" style="margin:8px 0 14px">${T('cmd.ingame.hint')}</p>
    <div class="flex gap wrap center">
      <div><label class="fld" style="margin:0 0 5px">${T('cmd.ingame.constellations')}</label>
        <button class="btn" style="padding:10px 16px;font-size:12.5px" data-act="sendC6" ${dis}
          title="talent unlock all">${T('cmd.ingame.unlockC6')}</button></div>
      <div><label class="fld" style="margin:0 0 5px">${T('cmd.ingame.ascension', { level: PROMOTE_LEVEL_CAP[promote] })}</label>
        <div class="flex" style="gap:8px"><input class="num" style="width:76px;padding:9px 11px;border-radius:9px;border:1px solid color-mix(in srgb, var(--goldD) 40%, transparent);background:#fff;color:var(--ink);font-size:13px;font-weight:700" data-model="ingame.promote" value="${esc(S.ingame.promote)}" placeholder="6"/>
        <button class="btn" style="padding:10px 16px;font-size:12.5px" data-act="sendBreak" ${dis}>${T('cmd.ingame.applyAscension')}</button></div></div>
      <div style="flex:1"></div>
      ${onoff(T('cmd.ingame.stamina'), 'sendStamina')}
      ${onoff(T('cmd.ingame.hp'), 'sendWudi')}
    </div>
    <div class="note" style="margin-top:12px">${T('cmd.ingame.fallbackPre')} <button class="btn ghost" style="padding:5px 10px;font-size:11px" data-act="sendHeroWit" ${dis}>${T('cmd.ingame.heroWit')}</button> ${T('cmd.ingame.fallbackPost')}</div>
  </div>`;
}

// ── commands: skins (costumes) ──
// One skin per button row; version 1.6 has only two (Barbara + Jean), 2.8 has all 10.
function costumeCard() {
  const v = S.selectedVersionId || '';
  const list = COSTUMES.filter((c) => c.since === '1.6' || v === '2.8');
  if (!list.length) return '';
  const dis = S.sending ? 'disabled' : '';
  const btns = list.map((c) => `<button class="btn ghost" style="padding:9px 13px;font-size:12px"
      data-act="sendCostume" data-arg="${c.id}" ${dis} title="item add ${c.id} 1"><b>${esc(c.char)}</b>&nbsp;· ${esc(c.name)}</button>`).join('');
  return `<div class="sect" style="margin-top:16px;margin-bottom:0">
    <div class="flex between center wrap" style="gap:10px;margin-bottom:4px">
      <h4 style="margin:0">${T('cmd.costume.title')}</h4>
      <span class="vchip">${T('cmd.costume.chip', { version: v, n: list.length })}</span>
    </div>
    <p class="hint" style="margin:8px 0 12px">${T('cmd.costume.hint')}</p>
    <div class="flex gap wrap">${btns}</div>
  </div>`;
}

function cmdHeader() {
  const v = S.gd.version || S.selectedVersionId || '—';
  return `<div class="flex between center wrap" style="gap:14px;margin-bottom:20px">
    <div><h1 class="title">${T('cmd.title')}</h1>
      <div class="flex center gap wrap" style="margin-top:7px">
        <p class="sub" style="margin:0">${T('cmd.sub')}</p>
        <button class="vchip" data-act="go" data-arg="library" title="${T('cmd.changeVersionTitle')}">${T('cmd.onlyContentFrom', { version: v })}</button>
      </div></div>
    <div class="flex center gap"><label class="fld" style="margin:0">${T('cmd.uid')}</label><input class="txt" style="width:130px" data-model="cmdUid" value="${esc(S.cmdUid)}" placeholder="${T('cmd.uidPlaceholder')}"/></div>
  </div>`;
}

function cmdConsole() {
  const logs = S.log.map((l) => `<div style="color:${l.t === 'ok' ? '#7fc083' : l.t === 'cmd' ? '#e6cf93' : l.t === 'bad' ? '#e08a7f' : '#8aa0c8'}">${esc(l.m)}</div>`).join('');
  return `<div class="console"><div class="bar"><span class="lights"><i style="background:#e4664e"></i><i style="background:#e8b04a"></i><i style="background:#67b86a"></i></span><span>${T('cmd.console.label')} · ${esc(S.settings.serverHost)}</span></div><div class="log" id="cmdlog">${logs}</div></div>`;
}

// Everything that is not a loaded catalogue lands here: the screen keeps exactly the capability it
// had before the redesign (three raw-id cards), it never blocks and it never shows an error page.
function catalogFallback() {
  const v = S.gd.version || S.selectedVersionId || '—';
  const note = S.gd.status === 'loading' || S.gd.status === 'idle'
    ? T('cmd.catalog.loading', { version: v })
    : `${T('cmd.catalog.unavailable', { version: v })}${S.gd.error ? ' (' + esc(S.gd.error) + ')' : ''}`;
  return `<div class="note">${note}</div><div class="cmdgrid" style="margin-top:16px">${cmdCards(ID_CATS)}</div>`;
}

// ── commands: character ──
function avatarHero() {
  const a = curAvatar();
  if (!a) {
    return `<div class="gmhero"><div class="bg" style="background-image:url('${heroImg(S.gd.version)}')"></div>
      <div class="txt"><div class="kicker">${T('cmd.avatar.kicker')}</div><h2>${T('cmd.avatar.none')}</h2>
        <p>${T('cmd.avatar.pickHint', { version: S.gd.version, n: S.gd.avatars.length })}</p>
        <div class="flex gap"><button class="btn" data-act="openPicker" data-arg="avatar">${T('cmd.avatar.pick')}</button></div></div></div>`;
  }
  const c = elColor(a.element);
  const w = wtype(a.weapon);
  return `<div class="gmhero" data-artwrap>
    <div class="bg" style="background-image:url('${esc(splashImg(a.id))}')"></div>
    <div class="glow" style="background:radial-gradient(420px 300px at 78% 55%,${c}55,transparent 70%)"></div>
    <span class="ph" style="background:linear-gradient(180deg,${c}66,${c}18)">${esc(initial(a.name))}</span>
    <img class="art" src="${esc(splashImg(a.id))}" alt="" loading="lazy" decoding="async"/>
    <div class="txt">
      <div class="kicker">${T('cmd.avatar.selectedKicker')}</div>
      <h2>${esc(a.name)}</h2>
      <div class="flex center gap wrap">
        ${a.element ? `<span class="badge"><span class="dot" style="background:${c}"></span>${esc(a.element)}</span>` : ''}
        ${w ? `<span class="badge">${T(w.one)}</span>` : ''}
        <span class="badge">${stars(a.rarity)}</span>
        <span class="badge mono">${T('cmd.idBadge', { id: a.id })}</span>
        ${a.since && a.since === S.gd.version ? `<span class="badge" style="color:var(--gold2)">${T('cmd.avatar.newIn', { version: a.since })}</span>` : ''}
      </div>
      <div class="flex gap" style="margin-top:4px">
        <button class="btn" data-act="sendAvatar" ${S.sending ? 'disabled' : ''}>${T('cmd.avatar.add')}</button>
        <button class="btn dark" data-act="openPicker" data-arg="avatar">${T('cmd.avatar.change')}</button>
      </div>
    </div>
  </div>`;
}

// ── commands: weapon ──
function weaponSlot() {
  const w = curWeapon();
  const wt = w && wtype(w.type);
  return `<div class="gmslot">
    <div class="top"><span class="ic" style="background:rgba(228,102,78,.14);color:#c9462e">${CMDICON.weapon}</span>
      <div><b>${T('cmd.weapon.title')}</b><br><small>${T('cmd.inVersionCount', { n: S.gd.weapons.length, version: S.gd.version })}</small></div></div>
    ${w ? `<div class="pickedrow">${iconTile(w.icon, w.name, 'big')}<div>
             <div class="name">${esc(w.name)}</div>
             <div class="idm">${wt ? T(wt.one) + ' · ' : ''}${T('cmd.idBadge', { id: w.id })} · ${stars(w.rarity)}</div></div></div>`
        : `<div class="name empty">${T('cmd.weapon.none')}</div><div class="idm">${T('cmd.weapon.pickOne')}</div>`}
    <div class="params">
      <div><label class="fld">${T('cmd.weapon.level')}</label><input class="num" data-model="sel.weaponLevel" value="${esc(S.sel.weaponLevel)}" placeholder="90"/></div>
      <div><label class="fld">${T('cmd.weapon.ascension')}</label><input class="num" data-model="sel.weaponPromote" value="${esc(S.sel.weaponPromote)}" placeholder="6"/></div>
    </div>
    <div class="fill"></div>
    <div class="foot"><button class="btn ghost" data-act="openPicker" data-arg="weapon">${T('cmd.weapon.pick')}</button>
      <button class="btn" data-act="sendWeapon" ${w && !S.sending ? '' : 'disabled'}>${T('cmd.weapon.send')}</button></div>
  </div>`;
}

// ── commands: artifact set ──
// Real max level of a piece: +20 at 5★, +16 at 4★ — the clamp at send time respects it.
const setMaxLevel = (st) => (st && st.rarity === 4 ? 16 : 20);
function artifactSlot() {
  const st = curSet();
  const pcs = st ? st.ids.map((id, i) => `${slotName(i)} ${id}`).join(' · ') : '';
  const inv = S.sel.artifactMode !== 'equip';
  const mode = (id, on, label) =>
    `<button class="elchip${on ? ' on' : ''}" data-act="setArtifactMode" data-arg="${id}"${on ? ' style="background:var(--goldD);border-color:var(--goldD)"' : ''}>${label}</button>`;
  return `<div class="gmslot">
    <div class="top"><span class="ic" style="background:rgba(180,142,224,.16);color:#8a5fc4">${CMDICON.artifact}</span>
      <div><b>${T('cmd.artifact.title')}</b><br><small>${T('cmd.inVersionCount', { n: S.gd.sets.length, version: S.gd.version })}</small></div></div>
    ${st ? `<div class="name">${esc(st.name)} ${stars(st.rarity)}</div>
            <div class="sprrow">${st.ids.map((id, i) => iconTile(st.icons[i], slotName(i), '')).join('')}</div>
            <div class="idm">${esc(pcs)}</div>`
         : `<div class="name empty">${T('cmd.artifact.none')}</div><div class="idm">${T('cmd.artifact.pickOne')}</div>`}
    <div class="chips" style="justify-content:flex-start;margin-top:10px">
      ${mode('equip', !inv, T('cmd.artifact.modeEquip'))}${mode('inv', inv, T('cmd.artifact.modeInv'))}
    </div>
    ${inv ? '' : `<div class="params">
      <div><label class="fld">${T('cmd.artifact.pieceLevel', { max: setMaxLevel(st) })}</label><input class="num" data-model="sel.artifactLevel" value="${esc(S.sel.artifactLevel)}" placeholder="${setMaxLevel(st)}"/></div>
    </div>`}
    <div class="note">${inv
      ? T('cmd.artifact.noteInv', { n: st ? st.ids.length : 5 })
      : T('cmd.artifact.noteEquip', { n: st ? st.ids.length : 5 })}</div>
    <div class="fill"></div>
    <div class="foot"><button class="btn ghost" data-act="openPicker" data-arg="artifact">${T('cmd.artifact.pick')}</button>
      <button class="btn" data-act="sendSet" ${st && !S.sending ? '' : 'disabled'}>${T('cmd.artifact.send')}${st ? ` · ${T('cmd.artifact.nCommands', { n: st.ids.length })}` : ''}</button></div>
  </div>`;
}

// Kept for power users and as the automatic escape hatch when the catalogue is missing: raw ids and
// a raw GM command are the only way to reach content the catalogue does not list.
function rawSection() {
  const head = `<button class="btn ghost" style="padding:9px 14px;font-size:12px" data-act="toggleRaw">
      ${S.rawOpen ? '▾' : '▸'} ${T('cmd.raw.toggle')}</button>`;
  if (!S.rawOpen) return `<div style="margin:18px 0">${head}</div>`;
  return `<div style="margin:18px 0">${head}
    <div class="sect" style="margin-top:14px"><h4>${T('cmd.raw.title')}</h4>
      <p class="hint">${T('cmd.raw.hint1')}
        ${T('cmd.raw.hint2')} <span class="mono">equip add</span>${T('cmd.raw.hint3')} <span class="mono">${T('cmd.raw.itemAddExample')}</span>.</p>
      <div class="cmdgrid">${cmdCards(ID_CATS)}</div>
      <label class="fld">${T('cmd.raw.label')}</label>
      <div class="flex gap"><input class="txt mono" data-model="rawCmd" value="${esc(S.rawCmd)}" placeholder="${T('cmd.raw.placeholder')}"/>
        <button class="btn" data-act="sendRaw" ${S.sending ? 'disabled' : ''}>${T('common.send')}</button></div>
    </div>
  </div>`;
}

// ── commands: the modal picker ──
function pickerRows() {
  const p = S.picker;
  const rar = (list) => (p.r ? list.filter((x) => x.rarity === p.r) : list);
  if (p.kind === 'avatar') return p.el ? S.gd.avatars.filter((a) => String(a.element).toLowerCase() === p.el) : S.gd.avatars;
  if (p.kind === 'weapon') return rar(p.type ? S.gd.weapons.filter((w) => String(w.type).toLowerCase() === p.type) : S.gd.weapons);
  return rar(S.gd.sets);
}

function pickerOverlay() {
  const p = S.picker;
  const title = { avatar: T('cmd.avatar.pick'), weapon: T('cmd.weapon.pick'), artifact: T('cmd.picker.titleArtifact') }[p.kind];
  const rows = pickerRows();
  const q = p.q.trim().toLowerCase();
  // The query hides rows with a class instead of dropping them, so deleting a character can bring
  // them back without a render — the keystroke filter never rebuilds the list.
  const hidden = (r) => (q && !r.s.includes(q) ? ' hide' : '');
  const shown = rows.filter((r) => !q || r.s.includes(q)).length;
  const list = p.kind === 'avatar' ? avatarGrid(rows, hidden)
    : p.kind === 'weapon' ? weaponList(rows, hidden)
      : artifactList(rows, hidden);
  const empty = rows.length === 0
    ? `<div class="pickempty">${T('cmd.picker.emptyCatalog', { version: S.gd.version })}</div>`
    : `<div class="pickempty${shown ? ' hide' : ''}">${T('cmd.picker.noResultsPre')}<b data-emptyq>${esc(p.q)}</b>${T('cmd.picker.noResultsPost')}</div>`;
  return `<div class="overlay" data-act="closePicker"><div class="pickbox" data-act="noop">
    <div class="pickhead">
      <div class="flex center gap"><div class="serif" style="font-weight:700;font-size:18px">${title}</div>
        <span class="vchip">${T('cmd.picker.onlyFrom', { version: S.gd.version })} · <b data-count>${shown}</b> ${T('cmd.picker.results')}</span></div>
      <button class="pclose" data-act="closePicker" title="${T('common.close')}"><svg width="13" height="13" viewBox="0 0 12 12"><line x1="2.5" y1="2.5" x2="9.5" y2="9.5" stroke="currentColor" stroke-width="1.5"/><line x1="9.5" y1="2.5" x2="2.5" y2="9.5" stroke="currentColor" stroke-width="1.5"/></svg></button>
    </div>
    <div class="pickbar"><input class="txt" data-model="picker.q" data-filter value="${esc(p.q)}" placeholder="${T('cmd.picker.searchPlaceholder')}"/>${pickerChips()}</div>
    <div class="pickbody">${list}${empty}</div>
    <div class="pickfoot"><p class="hint">${T('cmd.picker.searchHint')}</p>
      <button class="btn ghost" style="padding:9px 16px;font-size:12.5px" data-act="closePicker">${T('common.close')}</button></div>
  </div></div>`;
}

function pickerChips() {
  const p = S.picker;
  const chip = (act, val, on, label, color) =>
    `<button class="elchip${on ? ' on' : ''}" data-act="${act}" data-arg="${esc(val)}"${on && color ? ` style="background:${color};border-color:${color}"` : ''}>${esc(label)}</button>`;
  const rarChips = `<div class="chips">${chip('setPickerRar', '', !p.r, t('common.all'), '')}
      ${chip('setPickerRar', '5', p.r === 5, '5★', 'var(--goldD)')}${chip('setPickerRar', '4', p.r === 4, '4★', 'var(--goldD)')}</div>`;
  if (p.kind === 'avatar') {
    return `<div class="chips">${chip('setPickerEl', '', !p.el, t('common.all'), '')}
      ${ELEMENTS.map((e) => chip('setPickerEl', e.toLowerCase(), p.el === e.toLowerCase(), e, elColor(e))).join('')}</div>`;
  }
  if (p.kind === 'weapon') {
    return `<div class="chips">${chip('setPickerType', '', !p.type, t('common.all'), '')}
      ${WTYPES.map((w) => chip('setPickerType', w.key, p.type === w.key, t(w.many), 'var(--goldD)')).join('')}</div>${rarChips}`;
  }
  return rarChips;
}

function avatarGrid(rows, hidden) {
  const tiles = rows.map((a) => {
    const c = elColor(a.element);
    return `<button class="ptile r${a.rarity}${S.sel.avatarId === a.id ? ' sel' : ''}${hidden(a)}" data-artwrap
      data-act="pickAvatar" data-arg="${a.id}" data-s="${esc(a.s)}" title="${esc(a.name)}">
      <span class="ph" style="background:linear-gradient(180deg,${c}66,${c}18)">${esc(initial(a.name))}</span>
      <img src="${esc(splashImg(a.id))}" alt="" loading="lazy" decoding="async"/>
      <span class="el" style="background:${c}"></span>
      ${a.since && a.since === S.gd.version ? `<span class="new">${T('cmd.picker.newBadge')}</span>` : ''}
      <span class="nm">${esc(a.name)}</span>
    </button>`;
  }).join('');
  return `<div class="ptilegrid">${tiles}</div>`;
}

function weaponList(rows, hidden) {
  const groups = WTYPES.map((wt) => [t(wt.many), rows.filter((w) => String(w.type).toLowerCase() === wt.key)]);
  const known = WTYPES.map((wt) => wt.key);
  groups.push([t('cmd.picker.others'), rows.filter((w) => known.indexOf(String(w.type).toLowerCase()) < 0)]);
  return groups.filter((g) => g[1].length).map(([label, ws]) => {
    const items = ws.map((w) => `<button class="wrow${S.sel.weaponId === w.id ? ' sel' : ''}${hidden(w)}"
      data-act="pickWeapon" data-arg="${w.id}" data-s="${esc(w.s)}">
      ${iconTile(w.icon, w.name, '')}<b>${esc(w.name)}</b>
      <span class="meta"><span class="wid">${T('cmd.idBadge', { id: w.id })}</span>${stars(w.rarity)}</span></button>`).join('');
    // A group whose every row is filtered out would leave a dangling heading; filterPicker applies
    // the same rule live, so the two paths agree.
    const dead = ws.every((w) => hidden(w)) ? ' hide' : '';
    return `<div class="wgroup${dead}"><h5>${esc(label)}</h5><div class="wgrid">${items}</div></div>`;
  }).join('');
}

function artifactList(rows, hidden) {
  const cards = rows.map((s) => `<button class="aset${S.sel.setName === s.name ? ' sel' : ''}${hidden(s)}"
    data-act="pickSet" data-arg="${esc(s.name)}" data-s="${esc(s.s)}">
    <div class="sprrow">${s.ids.map((id, i) => iconTile(s.icons[i], slotName(i), '')).join('')}</div>
    <div class="sn">${esc(s.name)} ${stars(s.rarity)}</div>
    <div class="pcs">${s.ids.map((id, i) => esc(`${slotName(i)} ${id}`)).join(' · ')}</div></button>`).join('');
  return `<div class="asetgrid">${cards}</div>`;
}

// Keystroke filtering: toggle .hide on the already rendered rows. A render() per keystroke would
// destroy the caret and rebuild every tile, so this path must never call render().
function filterPicker(q) {
  const box = document.querySelector('.pickbody');
  if (!box) return;
  const needle = String(q).trim().toLowerCase();
  let shown = 0;
  box.querySelectorAll('[data-s]').forEach((el) => {
    const hit = !needle || el.getAttribute('data-s').indexOf(needle) >= 0;
    el.classList.toggle('hide', !hit);
    if (hit) shown++;
  });
  box.querySelectorAll('.wgroup').forEach((g) => g.classList.toggle('hide', !g.querySelector('[data-s]:not(.hide)')));
  const empty = box.querySelector('.pickempty');
  if (empty && empty.querySelector('[data-emptyq]')) {
    empty.classList.toggle('hide', shown > 0);
    empty.querySelector('[data-emptyq]').textContent = q;
  }
  const count = document.querySelector('.pickhead [data-count]');
  if (count) count.textContent = shown;
}

// ── server ──
// The agent reports more than "running / not running": a stack can be MISSING from the box or exist
// without ever having been installed. Starting an uninstalled one used to stop the running server and
// then fail, so those states have to be drawn separately, not folded into OFFLINE.
// Label tables hold translation keys (translated when drawn).
const SRVMETA = {
  live: ['server.state.live', 'var(--ok)'],
  down: ['server.state.down', 'var(--bad)'],
  absent: ['server.state.absent', '#9098aa'],
  notready: ['server.state.notready', 'var(--warn)'],
  working: ['server.state.working', 'var(--warn)'],
  checking: ['server.state.checking', 'var(--warn)'],
  unknown: ['server.state.unknown', '#9098aa'],
};
const SRVNOTE = {
  live: 'server.note.live',
  down: 'server.note.down',
  absent: 'server.note.absent',
  notready: 'server.note.notready',
  working: 'server.note.working',
  checking: 'server.note.checking',
  unknown: 'server.note.unknown',
};
const SRVJOBS = {
  start: 'server.job.start', stop: 'server.job.stop', setup: 'server.job.setup',
  provision: 'server.job.provision', txtfixes: 'server.job.txtfixes', netfix: 'server.job.netfix',
  events: 'server.job.events', revive: 'server.job.revive',
  towerfix: 'server.job.towerfix', accountcopy: 'server.job.accountcopy',
  templates: 'server.job.templates', 'templates.ensure': 'server.job.templates',
  secrets: 'server.job.secrets', 'secrets.set': 'server.job.secrets',
  auth: 'server.job.auth', password: 'server.job.password', signup: 'server.job.signup',
  hotpatch: 'server.job.hotpatch',
  // Agent 3.4: the ready-made stack downloaded from the Internet Archive (POST /server/fetch).
  fetch: 'server.job.fetch',
  // Agent 3.4: the pathfinding server switched on / off (POST /server/pathfinding).
  pathfinding: 'server.job.pathfinding',
  // Agent 3.5: an account created from Server Admin mode (POST /server/account/create).
  accountcreate: 'server.job.accountcreate',
  // 'hotpatch.voice' = our own job, 'hotpatch-voice' = the agent's kind (a job started elsewhere).
  'hotpatch.voice': 'server.job.hotpatchVoice', 'hotpatch-voice': 'server.job.hotpatchVoice',
  // Agent 3.6: a version's stack folder moved / re-pointed / taken off the agent (POST /server/relocate).
  relocate: 'server.job.relocate',
};
// Jobs started by the agent itself (watchdogs), not by a person — "operation started elsewhere
// (another computer)" would be a lie that sends the user to wait for a phantom admin.
const AGENT_SELF_JOBS = { revive: true, towerfix: true };
// The agent's watchdog jobs start BESIDE a voice download (agent 3.3, the job's yielding phase) and
// /status then names the newer job as `busy`: such a busy says nothing about the voice job itself.
const BESIDE_VOICE_JOBS = { revive: true, towerfix: true, netfix: true };
const TXTFIX = { none: 'server.txtfix.none', pending: 'server.txtfix.pending', applied: 'server.txtfix.applied' };
// Agent 3.4: the provisioning choice a stack was prepared with (state line of the Advanced section).
const PROGRESS_MODE = { none: 'server.adv.progressMode.none', default: 'server.adv.progressMode.default', keep: 'server.adv.progressMode.keep', fixes: 'server.adv.progressMode.fixes' };
const jobTitle = (kind) => t(SRVJOBS[kind] || 'server.job.generic');
// The title of a job OBJECT: a Prepare / progress job says which choice it carries — "keep" and
// "fixes" have their own words, because "GAA progress re-apply" would announce the very import the
// admin declined in the dialog.
const jobTitleOf = (j) => {
  if (j && (j.kind === 'setup' || j.kind === 'provision')) {
    if (j.progress === 'keep') return t('server.job.provisionKeep');
    if (j.progress === 'fixes') return t('server.job.provisionFixes');
  }
  return jobTitle(j ? j.kind : '');
};
const jobRunning = () => !!(S.srvJob && S.srvJob.running);
// The followed job carries the server it was started on (`target`, runServerJob / srvJobFor): after a
// switch to another server it still runs THERE — and a voice packs job does so for an hour. jobHere =
// it is this server's: the only case in which a card may draw it as its own (WORKING, the voice
// running note, Stop — a Stop goes to the CONFIGURED host, so from another server's card it would hit
// the wrong box). The button gating stays on jobRunning(): one followed job per launcher, the backend
// refuses a second one anyway; jobElsewhere says on screen why this server's buttons are dead.
const jobHere = () => jobRunning() && S.srvJob.target === srvTarget();
const jobElsewhere = () => jobRunning() && S.srvJob.target !== srvTarget();
// Busy = our job OR one started elsewhere (another computer, another admin): the agent accepts a
// single operation at a time, so the buttons must be dead in both cases, not only in ours.
const srvBusy = () => jobRunning() || !!(S.srv.info && S.srv.info.busy);

// An older agent does not send present/bootstrapped. Their absence means "it is there and installed"
// — otherwise the screen would declare absent some stacks that work perfectly.
function srvFacts(id) {
  const e = S.srv.versions[id];
  if (!e) return null;
  return {
    up: !!e.up, present: e.present !== false, bootstrapped: e.bootstrapped !== false,
    // Agent 3.2 sends null when its state file is unreadable: unknown, not "not applied".
    provisioned: e.provisioned === null ? null : !!e.provisioned, provisionedAt: e.provisionedAt || '',
    txtFixes: e.txtFixes || 'none', account: e.account || '', dir: e.dir || '',
    // Agent 2.5+: the compose services NOT running although the stack is up (the gameserver exits
    // with code 0 when it dies, so "up" alone says nothing). Absent on old agents = we assert nothing.
    down: Array.isArray(e.servicesDown) ? e.servicesDown : [],
    // Up with dead game services. The public snapshot sets `degraded` without naming them (empty down),
    // so readers test this flag and show the list only when it is not empty.
    degraded: !!e.degraded || (Array.isArray(e.servicesDown) && e.servicesDown.length > 0),
    // Agent 3.x: progress templates present on the box + whether passwords are verified in game.
    templates: Array.isArray(e.templates) ? e.templates : (e.templates && typeof e.templates === 'object' ? Object.keys(e.templates).map((id) => ({ id, label: id })) : []),
    passwordVerify: !!e.passwordVerify,
    // Agent 3.1+: {available, enabled, pending, complete} of the official 2021 hotpatch mirror.
    hotpatch: e.hotpatch && typeof e.hotpatch === 'object' ? e.hotpatch : null,
    // Agent 3.4: 'vendor' = the stack still carries the archive's published MUIP sign key (anyone with
    // the archive can sign GM commands) | 'custom' | null (absent stack / older agent).
    muipKey: e.muipKey === 'vendor' || e.muipKey === 'custom' ? e.muipKey : null,
    // Agent 3.4: the choice the stack was prepared with ('default' = the shipped save imported, 'keep'
    // = the admin's own progress, 'fixes' = configuration files only; null = never / older agent) and
    // whether the shipped save — the default account — is in THIS database (null = the agent's state
    // file is unreadable, undefined = older agent). The account shown on the card follows it.
    progress: e.progress === 'default' || e.progress === 'keep' || e.progress === 'fixes' ? e.progress : null,
    defaultAccount: e.defaultAccount === undefined ? undefined : e.defaultAccount,
    // Agent 3.4: the pathfinding server in the stack's compose file — true/false, or null = unknown
    // (older agent, an absent stack, or the service not in the file): the toggle asserts nothing then.
    pathfinding: typeof e.pathfinding === 'boolean' ? e.pathfinding : null,
    // Agent 3.4: {available, size, host, topDir, part, archive, tool, fetchedAt} of the ready-made
    // stack download (POST /server/fetch); null on an older agent — the card keeps today's MISSING.
    // configured:false = the version has no folder on this agent, nothing to download into.
    fetch: e.fetch && typeof e.fetch === 'object' ? e.fetch : null,
    configured: e.configured !== false,
  };
}

// The agent answered, but `docker compose ls` failed/timed out on the box: every version then carries
// up:false without it being true. The library badge already refused to call that STOPPED; the cards
// and the start-screen pill must not call it OFFLINE either (fixed 2026-08-21).
const srvDockerError = () => !!(S.srv.info && S.srv.info.error);
function srvState(id) {
  // The followed job turns its version's card to WORKING when it runs on THIS server (jobHere) and
  // works on the stack. A voice packs job only fills the mirror folder — for an hour — while the game
  // server stays up: the card keeps LIVE and its DEGRADED line (the agent goes on reporting
  // servicesDown during that job for this very reason); the buttons are dead through srvBusy() anyway.
  // An account creation (agent 3.5) is the same case: it only writes the SDK and the player database
  // of a RUNNING stack — it needs the stack up and stops nothing, so the card stays LIVE.
  if (jobHere() && S.srvJob.version === id && S.srvJob.kind !== 'hotpatch.voice' && S.srvJob.kind !== 'accountcreate') return 'working';
  const f = srvFacts(id);
  if (!f) return S.srv.state === 'checking' ? 'checking' : 'unknown';
  if (!f.present) return 'absent';
  if (!f.bootstrapped) return 'notready';
  if (f.up) return 'live';
  return srvDockerError() ? 'unknown' : 'down';
}

// The version whose stack is running on the box RIGHT NOW, or null. Only one can run at a time
// (identical ports), so the first one found is the complete answer.
function srvLiveId() {
  for (const id of Object.keys(S.srv.versions || {}))
    if (S.srv.versions[id] && S.srv.versions[id].up) return id;
  return null;
}

// Server status on the main page: GLOBAL (what runs on the box), not the selected version's — the
// player wants to know whether there is something to connect to and on which version. The data comes
// from the same S.srv as the "Server status" screen, refreshed by the polling in boot(). Without a
// configured server nothing is asserted; a contact error = "unverified", not an invented STOPPED (the
// agent may be down with the game server perfectly functional).
function srvBadge() {
  if (!S.serverConfigured) return '';
  const go = `data-act="go" data-arg="server" style="cursor:pointer"`;
  if (S.srv.state === 'ok') {
    const live = srvLiveId();
    // up:true is an affirmative fact and stays LIVE even next to an error reported by the agent...
    if (live) {
      // ...but "running" with dead game services is not LIVE: the agent restarts them on its own
      // (the service watchdog, under ~2 minutes) — until then the player sees DEGRADED, not a LIVE
      // that ends in a white screen in game.
      const lf = srvFacts(live);
      if (lf && lf.degraded)
        return `<span class="badge" ${go} title="${lf.down.length ? T('server.badge.degradedTitle', { services: lf.down.join(', ') }) : T('server.badge.degradedTitleNoList')}"><span class="dot" style="background:var(--warn)"></span>${T('server.badge.label')} <b style="color:var(--warn)">&nbsp;${T('server.badge.degraded', { version: live })}</b></span>`;
      return `<span class="badge" ${go} title="${T('server.badge.liveTitle', { version: live })}"><span class="dot live"></span>${T('server.badge.label')} <b style="color:var(--ok)">&nbsp;${T('server.badge.live', { version: live })}</b></span>`;
    }
    // ...but a "nothing up" with a top-level error is not STOPPED: docker compose ls failed on the box
    // and up:false is fabricated for every version — the server may be humming along. Same rule as the
    // PLAY pre-check and as in --play: without data, nothing is asserted.
    if (!(S.srv.info && S.srv.info.error))
      return `<span class="badge" ${go} title="${T('server.badge.stoppedTitle')}"><span class="dot" style="background:var(--bad)"></span>${T('server.badge.label')} <b style="color:var(--bad)">&nbsp;${T('server.badge.stopped')}</b></span>`;
  }
  if (S.srv.state === 'error' || S.srv.state === 'ok') {
    const why = S.srv.error || (S.srv.info && S.srv.info.error) || t('server.badge.unreachable');
    return `<span class="badge" ${go} title="${esc(String(why))}"><span class="dot" style="background:#9098aa"></span>${T('server.badge.label')} ${T('server.badge.unverified')}</span>`;
  }
  return `<span class="badge"><span class="dot" style="background:var(--warn)"></span>${T('server.badge.label')} ${T('server.badge.checking')}</span>`;
}

function serverCard(v) {
  const st = srvState(v.id);
  const f = srvFacts(v.id);
  const meta = SRVMETA[st];
  const dis = srvBusy() ? 'disabled' : '';
  // "Check" is only a read — the agent accepts it while someone else is working too, so it stays the
  // only way out of a "busy" started elsewhere. Dead only under a job followed on THIS server: one
  // followed on another server (jobElsewhere) says nothing about this one's status.
  const chkDis = jobHere() ? 'disabled' : '';
  const fld = 'color:var(--muted);font-weight:800;font-size:10.5px';
  // Dead game services on a "live" stack: the agent's watchdog restarts them on its own within a few
  // minutes — the card says so, so the user does not press Stop over it for nothing. The fallback
  // button is "Restart services", because "Start" does not exist on the live card.
  const isDegraded = st === 'live' && f && f.degraded;
  const degraded = isDegraded
    ? `<p style="color:var(--warn);font-size:12.5px;font-weight:700;line-height:1.5;margin:8px 0 0">${f.down.length ? T('server.card.degraded', { services: f.down.join(', ') }) : T('server.card.degradedNoList')}</p>`
    : '';
  // MISSING with a download known for it (agent 3.4: the ready-made package on the Internet Archive)
  // and a folder configured to put it in: the note says so and the button fetches it — "Resume" with
  // the bytes a stopped or cut run kept in the .part. Without a 7z tool on the box the agent would
  // refuse the job (409): the button stays dead and the note names what to install.
  const canFetch = st === 'absent' && !!(f && f.fetch && f.fetch.available && f.configured);
  const fetchSize = canFetch ? sizeGb(f.fetch.size) : '';
  const fetchPart = canFetch ? Math.max(0, Number(f.fetch.part)) || 0 : 0;
  const note = st === 'unknown' && srvDockerError() ? T('server.note.dockerError', { error: String(S.srv.info.error) })
    : canFetch ? T('server.note.absentFetch', { size: fetchSize, dir: f.dir }) : T(SRVNOTE[st]);
  const noTool = canFetch && !f.fetch.tool
    ? `<p style="color:var(--warn);font-size:12.5px;font-weight:700;line-height:1.5;margin:8px 0 0">${T('server.note.noExtractor')}</p>` : '';
  // No pre-made login on this server (prepared with "keep my progress" / "fixes only"): said in place
  // of the empty account slot, so nobody hunts for the catalogue's name on the login screen.
  const noDefault = f && !f.account && (f.progress === 'keep' || f.progress === 'fixes') && (st === 'live' || st === 'down')
    ? `<p style="color:var(--inkSoft);font-size:12px;font-weight:600;line-height:1.5;margin:-8px 0 14px">${T('server.card.noDefaultAccount')}</p>` : '';
  // Absent or unverified: no start button. Not installed: the install, not the start.
  // On the degraded card, "Restart services" is still server.start: a start over the already running
  // stack does not stop it (down_all keeps it), it only restarts what is dead and verifies stability —
  // the supervised variant of the repair the watchdog does on its own anyway.
  const cta = st === 'absent'
    ? (canFetch ? `<button class="btn" style="flex:1;padding:11px" data-act="fetchServer" data-arg="${v.id}" ${dis || (!f.fetch.tool ? 'disabled' : '')}>${fetchPart > 0 ? T('server.card.fetchResume', { have: sizeGb(fetchPart), size: fetchSize }) : T('server.card.fetch', { size: fetchSize })}</button>` : '')
    : st === 'unknown' || st === 'working' || st === 'checking' ? ''
    : st === 'notready' ? `<button class="btn" style="flex:1;padding:11px" data-act="setupServer" data-arg="${v.id}" ${dis}>${T('server.card.setup')}</button>`
      : st === 'live' ? `${isDegraded ? `<button class="btn" style="flex:1;padding:11px" data-act="startServer" data-arg="${v.id}" ${dis}>${T('server.card.restartServices')}</button>` : ''}<button class="btn ghost" style="flex:1;padding:11px" data-act="stopServer" data-arg="${v.id}" ${dis}>${T('server.card.stop')}</button>`
        : `<button class="btn" style="flex:1;padding:11px" data-act="startServer" data-arg="${v.id}" ${dis}>${T('server.card.start')}</button>`;
  return `<div class="cmdcard" style="padding:20px">
    <div class="flex between center"><div class="serif" style="font-weight:700;font-size:18px">${T('server.card.title', { id: v.id })}</div>
      <span class="badge" style="background:rgba(0,0,0,.06);color:${meta[1]}"><span class="dot" style="background:${meta[1]}"></span>${T(meta[0])}</span></div>
    <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;line-height:1.5;margin:11px 0 0">${note}</p>${degraded}${noTool}
    <div class="flex gap wrap" style="margin:14px 0 18px">
      <div><small style="${fld}">${T('server.card.versionLabel')}</small><div style="font-weight:800">${T('common.versionN', { id: v.id })}</div></div>
      <div><small style="${fld}">${T('server.card.serverLabel')}</small><div style="font-weight:800">${esc(S.settings.serverHost)}</div></div>
      ${f && f.account ? `<div><small style="${fld}">${T('server.card.accountLabel')}</small><div style="font-weight:800" class="mono">${esc(f.account)}</div></div>` : ''}
      ${f && f.present && f.pathfinding !== null ? `<div><small style="${fld}">${T('server.card.pathfindingLabel')}</small><div style="font-weight:800">${T(f.pathfinding ? 'server.pathfinding.on' : 'server.pathfinding.off')}</div></div>` : ''}
    </div>${noDefault}
    <div class="flex gap"><button class="btn ghost" style="flex:1;padding:11px" data-act="checkServers" ${chkDis}>${T('server.card.check')}</button>${cta}</div>
    ${st === 'live' || st === 'down'
      ? `<button class="btn ghost danger" style="width:100%;margin-top:10px;padding:10px" data-act="provisionServer" data-arg="${v.id}" ${dis}>${T('server.card.resetProgress')}</button>`
      : ''}
  </div>`;
}

// The agent prefixes its lines with the time, so "ERROR" is not necessarily at the start.
const srvLogLine = (l) => `<div style="color:${l.indexOf('ERROR') >= 0 ? '#e08a7f' : '#8aa0c8'}">${esc(l)}</div>`;

// Log of the operation in progress — the same console as on the commands screen, so the user does
// not learn two forms for the same thing.
function serverConsole() {
  const j = S.srvJob;
  if (!j) return '';
  const lines = j.lines.map(srvLogLine).join('');
  const tail = j.running ? '' : (j.error ? ' · ' + T('server.console.failed') : ' · ' + T('server.console.done'));
  // A running stack download of THIS server (jobHere — the cancel goes to the configured host): the
  // agent's cancel is a plain call that gets through while the job runs, like the voice job's Stop;
  // the .part stays and the card's button resumes it. One click, dead until the job's state changes.
  // A stack folder copied to another drive (agent 3.6, relocate) stops the same way: the copy is
  // deleted, the old folder and the configuration stay as they were. crossDrive comes from the folder
  // dialog's check, or from the agent's own "Copying N files" line (server.log) — a rename on the same
  // drive takes a moment and has nothing to stop.
  const stopAct = j.kind === 'fetch' ? 'fetchCancel' : j.kind === 'relocate' && j.crossDrive ? 'relocCancel' : '';
  const cancel = j.running && stopAct && jobHere()
    ? `<div class="flex gap" style="justify-content:flex-end;margin-top:10px"><button class="btn ghost" data-act="${stopAct}" data-arg="${esc(j.version)}" ${j.cancelSent ? 'disabled' : ''}>${stopAct === 'fetchCancel' ? T('server.card.fetchCancel') : T('agentCfg.reloc.stop')}</button></div>` : '';
  return `<div class="console"><div class="bar"><span class="lights"><i style="background:#e4664e"></i><i style="background:#e8b04a"></i><i style="background:#67b86a"></i></span>
      <span>${esc(jobTitleOf(j).toLowerCase())} · ${T('server.console.version', { version: j.version })}${tail}</span></div>
    <div class="log" id="srvlog">${lines || `<div style="color:#8aa0c8">${T('server.console.connecting')}</div>`}</div></div>
    ${j.running ? cancel : `<div class="flex gap" style="justify-content:flex-end;margin-top:10px"><button class="btn ghost" data-act="closeSrvLog">${T('server.console.closeLog')}</button></div>`}`;
}

// Administration actions — a normal player never touches them, so they hide under the same treatment
// as "Advanced settings" in Settings.
function serverAdvanced() {
  const head = `<button class="btn ghost" style="padding:9px 14px;font-size:12px" data-act="toggleSrvAdv">
      ${S.srvAdvOpen ? '▾' : '▸'} ${T('common.advancedAdminToggle')}</button>`;
  if (!S.srvAdvOpen) return `<div style="margin-top:16px">${head}</div>`;
  const busy = srvBusy();
  const rows = S.versions.map((v) => {
    const f = srvFacts(v.id);
    const dis = busy || !f || !f.present || !f.bootstrapped ? 'disabled' : '';
    const applied = !!f && f.txtFixes === 'applied';
    const state = !f ? T('server.adv.stateUnknown')
      : !f.present ? T('server.adv.stateAbsent')
        : !f.bootstrapped ? T('server.adv.stateNotReady')
          : f.provisioned === null ? `${T('server.adv.progress')} ${T('server.adv.unknown')}`
          : `${T('server.adv.progress')} ${f.provisioned ? T('server.adv.applied') + (f.provisionedAt ? ` (${esc(f.provisionedAt)})` : '') : T('server.adv.notApplied')} · ${T('server.adv.txtFixes')} ${esc(TXTFIX[f.txtFixes] ? t(TXTFIX[f.txtFixes]) : f.txtFixes)}`;
    // Agent 3.4: which choice the stack was prepared with (an older agent, or a never-prepared stack,
    // names none — the `none` word is empty and nothing is appended).
    const modeWord = f && f.present && f.bootstrapped ? t(PROGRESS_MODE[f.progress || 'none']) : '';
    const mode = modeWord ? ` · ${esc(modeWord)}` : '';
    // Only the warning is drawn: a private key (or an agent that does not report it) says nothing.
    const muipWarn = f && f.muipKey === 'vendor' ? ` · <span style="color:var(--warn)">${T('server.adv.muipVendor')}</span>` : '';
    // The account copy needs the stack RUNNING (it works through the live MySQL, without a stop).
    const cdis = busy || !f || !f.up ? 'disabled' : '';
    const c = S.acctCopy && S.acctCopy.version === v.id ? S.acctCopy : null;
    const copyForm = !c ? '' : `
      <div class="sect" style="margin-top:10px;padding:14px">
        <b>${T('server.acct.title', { version: v.id })}</b>
        <p class="hint" style="margin:8px 0 10px">${T('server.acct.hint')}</p>
        <div class="flex gap wrap">
          <div><label class="fld">${T('server.acct.from')}</label><input class="txt" style="width:170px" data-model="acctCopy.from" value="${esc(c.from)}" placeholder="${T('server.acct.fromPlaceholder')}"/></div>
          <div><label class="fld">${T('server.acct.to')}</label><input class="txt" style="width:190px" data-model="acctCopy.to" value="${esc(c.to)}" placeholder="${T('server.acct.toPlaceholder')}"/></div>
        </div>
        <div class="flex gap" style="margin-top:10px">
          <button class="btn" style="padding:8px 14px;font-size:12px" data-act="acctCopyGo">${T('server.acct.go')}</button>
          <button class="btn ghost" style="padding:8px 14px;font-size:12px" data-act="acctCopyClose">${T('common.giveUp')}</button>
        </div>
      </div>`;
    return `<div class="line"><div><b>${T('common.versionN', { id: v.id })}</b> <small style="color:var(--muted);font-weight:700">${state}${mode}${muipWarn}</small></div>
      <div class="flex gap wrap" style="justify-content:flex-end">
        <button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="provisionServer" data-arg="${v.id}" ${dis}>${T('server.adv.reprovision')}</button>
        <button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="eventsServer" data-arg="${v.id}" ${dis}>${T('server.adv.events')}</button>
        <button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="txtFixes" data-arg="${v.id}:${applied ? 'revert' : 'apply'}" ${dis}>${applied ? T('server.adv.txtRevert') : T('server.adv.txtApply')}</button>
        <button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="netfixServer" data-arg="${v.id}" ${dis}>${T('server.adv.netfix')}</button>
        <button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="acctCopyOpen" data-arg="${v.id}" ${cdis} title="${T('server.adv.copyTitle')}">${T('server.adv.copy')}</button>
      </div></div>${copyForm}`;
  }).join('');
  return `<div style="margin-top:16px">${head}
    <div class="sect" style="margin-top:14px"><h4>${T('server.adv.title')}</h4>
      <p class="hint">${T('server.adv.hint')}</p>
      ${rows}
    </div></div>`;
}

// ── player: "Your account on this server" (self-service signup, when the admin allows it) ──
// One shape for every reset: boot and another server (srvRetarget). version = the chooser's pick (null
// = acctVersionCur decides — until the player touches the form, which pins the version DRAWN);
// tplBy = {version: template id}, the progress choice of each version (a draw of one version never
// rewrites another's); another = {version: true} while "Create another account" replaces a
// remembered login; shown = the signature of the form last DRAWN (accountCreate compares it).
function accountBlank() { return { version: null, another: {}, shown: '', name: '', password: '', tplBy: {}, running: false, lines: [], error: null, result: null }; }
// The progress choices the server offers a player on this version ([{id, label}]) — or null when it
// offers none: signup off, the version not in the policy, or (agent 3.5) every template the policy
// lists still unimported on that stack, which the snapshot then leaves out. An agent that sends no
// list offers the new account only.
function acctOffer(id) {
  const su = S.pub.status && S.pub.status.signup;
  const sv = su && su.enabled && su.versions && su.versions[id];
  if (!sv) return null;
  if (!Array.isArray(sv.templates)) return [{ id: 'fresh', label: t('account.tpl.fresh') }];
  const list = sv.templates.filter((tp) => tp && typeof tp.id === 'string' && tp.id);
  return list.length ? list : null;
}
// Running right now, as the public snapshot says (a stopped version would refuse the signup anyway).
const acctUp = (id) => !!(S.pub.status && S.pub.status.versions && S.pub.status.versions[id] && S.pub.status.versions[id].up === true);
// The versions the card can speak about: the ones the server offers an account on, and the ones this
// PC already remembers a login for.
const acctVersions = () => S.versions.map((v) => v.id).filter((id) => acctOffer(id) || accountIsMine(id) || acctMineAll(id).length > 0);
// The version the card shows. It used to be the Library's selection, which with nothing installed is
// the catalogue's first version: a player who never installed 2.8 could never create a 2.8 account,
// however the admin had opened it. Now: the player's own pick; else the selection when an account can
// be created on it right now; else the first version where one can, preferring one that offers a
// prepared save (what the admin opened signups for); else the selection / the first one listed.
function acctVersionCur() {
  const list = acctVersions();
  const ready = (id) => !!acctOffer(id) && acctUp(id);
  const sel = S.selectedVersionId;
  if (S.account.version && list.includes(S.account.version)) return S.account.version;
  if (sel && list.includes(sel) && ready(sel)) return sel;
  const saved = list.find((id) => ready(id) && acctOffer(id).some((tp) => tp.id !== 'fresh'));
  if (saved) return saved;
  const first = list.find(ready);
  if (first) return first;
  if (sel && list.includes(sel)) return sel;
  return list[0] || null;
}
// What the form offers for a version, as one string: the version, the templates and whether a password
// is asked. accountCard records the one it DREW; accountCreate recomputes it from the current snapshot —
// a policy change that landed without a redraw (the user was typing) must not send a stale choice.
function acctSig(id) {
  const offer = acctOffer(id);
  const pub = S.pub.status;
  const pv = !!(pub && pub.versions && pub.versions[id] && pub.versions[id].passwordVerify);
  return id + '|' + (offer ? offer.map((tp) => tp.id).join(',') : '-') + '|' + pv + '|' + acctLimit(id);
}
// Every account this launcher created on a version of the current server (init state accountLists; an
// older build remembered only the login, S.accounts), oldest first.
function acctMineAll(id) {
  const lists = S.accountLists && S.accountLists[id];
  if (Array.isArray(lists)) return lists.filter((a) => a && typeof a.name === 'string' && a.name);
  return S.accounts[id] && S.accounts[id].name ? [S.accounts[id]] : [];
}
// The ones that still exist there: an account of an earlier generation was wiped by a re-provision
// with the default save. gen = the server's generation now (unknown = nothing is judged gone).
const acctGone = (a, gen) => !!(a.generation && gen && a.generation !== gen);
// How many accounts one launcher may create on this version (agent 3.7 maxPerPlayer), null when the
// server does not say (an older agent: no such limit).
function acctLimit(id) {
  const su = S.pub.status && S.pub.status.signup;
  const sv = su && su.versions && su.versions[id];
  return sv && Number.isInteger(sv.maxPerPlayer) && sv.maxPerPlayer >= 0 ? sv.maxPerPlayer : null;
}
// The server's generation of a version as the public snapshot says (null = unknown).
const acctGen = (id) => { const pub = S.pub.status; return (pub && pub.versions && pub.versions[id] && pub.versions[id].generation) || null; };
// This launcher's accounts on a version that still exist on the server.
const acctLive = (id) => { const gen = acctGen(id); return acctMineAll(id).filter((a) => !acctGone(a, gen)); };
function accountCard(versionId) {
  const pub = S.pub.status;
  if (!pub) return '';
  const offer = acctOffer(versionId);
  const up = acctUp(versionId);
  const pv = !!(pub.versions && pub.versions[versionId] && pub.versions[versionId].passwordVerify);
  const gen = acctGen(versionId);
  const all = acctMineAll(versionId);
  const live = all.filter((a) => !acctGone(a, gen));
  const gone = all.filter((a) => acctGone(a, gen));
  // The login the Library shows for this version (one of the list: the last created unless picked).
  const shownName = S.accounts[versionId] && S.accounts[versionId].name ? S.accounts[versionId].name.toLowerCase() : '';
  const limit = acctLimit(versionId);
  const full = limit !== null && live.length >= limit;
  const ac = S.account;
  const list = acctVersions();
  // The version chooser: only when there is a choice. Dead while a create runs — its log and its
  // result belong to the version it was started on.
  const chips = list.length > 1
    ? `<div class="flex gap wrap" style="margin-bottom:12px">${list.map((id) => `<button class="lg-chip ${versionId === id ? 'on' : ''}" data-act="accountVersion" data-arg="${esc(id)}" ${ac.running ? 'disabled' : ''}>${T('common.versionN', { id })}</button>`).join('')}</div>`
    : '';
  const title = `<h4>${T(live.length ? 'account.listTitle' : 'account.titleFor', { version: versionId })}</h4>`;
  // "Create a new account" opens the form under the list — only while the server offers one on this
  // version and this launcher is still under its limit there.
  const another = live.length > 0 && !!ac.another[versionId] && !!offer && !full;
  const formOpen = !!offer && !full && (!live.length || another);
  const staleNote = gone.length && !live.length ? `<div class="note" style="margin-bottom:10px">${T('account.stale', { name: gone[gone.length - 1].name })}</div>` : '';
  let listHtml = '';
  if (live.length) {
    const rows = live.map((a) => {
      const isShown = a.name.toLowerCase() === shownName;
      const use = isShown ? '' : `<button class="crcopy" data-act="accountUse" data-arg="${esc(versionId + ':' + a.name)}" title="${T('account.useTitle')}">${T('account.use')}</button>`;
      return `<div class="crow"><div><small>${T(isShown ? 'account.shownInLibrary' : 'account.accountLabel')}</small><b class="mono pick">${esc(a.name)}</b></div>
        <div class="flex gap">${use}<button class="crcopy" data-act="copyAccount" data-arg="${esc(a.name)}">${T('common.copy')}</button></div></div>`;
    }).join('');
    listHtml = `<div class="creds">${rows}<div class="crow"><div><small>${T('lib.creds.password')}</small><b class="pw">${esc(passwordWord(versionId))}</b></div></div></div>
      <p class="hint" style="margin:10px 0 0">${T('account.loginHint', { version: versionId })}</p>`;
  }
  if (!formOpen) {
    if (!live.length && !offer) return list.length > 1 || staleNote ? `<div class="sect">${title}${chips}${staleNote}</div>` : '';
    // Offered, but this launcher may create no (more) account here: its limit is used up, or it is 0.
    const limitNote = offer && full ? `<div class="note" style="margin-top:${live.length ? 12 : 0}px">${T(live.length ? 'account.limitReached' : 'account.limitClosed', { version: versionId, limit })}</div>` : '';
    const used = live.length && limit !== null ? `<span class="hint" style="margin:0">${T('account.used', { count: live.length, limit })}</span>` : '';
    const btn = live.length && offer && !full ? `<button class="btn" style="padding:9px 16px;font-size:12.5px" data-act="accountAnother" data-arg="${esc(versionId)}">${T('account.another')}</button>` : '';
    const foot = used || btn ? `<div class="flex between center" style="margin-top:12px">${used || '<span></span>'}${btn}</div>` : '';
    return `<div class="sect">${title}${chips}${staleNote}${listHtml}${foot}${limitNote}</div>`;
  }
  const templates = offer;
  // Normalised for THIS version only: a draw of another one (the card follows the poll until the
  // player touches it) used to reset the progress picked here to the new account.
  if (!templates.some((tp) => tp.id === ac.tplBy[versionId])) ac.tplBy[versionId] = templates[0].id;
  const tpl = ac.tplBy[versionId];
  ac.shown = acctSig(versionId);
  // Offered but not running: the form stays (the player can prepare it), Create waits for the server.
  const dis = ac.running || !up || !!S.acctPending ? 'disabled' : '';
  const limits = limit !== null ? T('account.limitsPlayer', { limit }) : T('account.limits');
  return `<div class="sect">${title}${chips}${staleNote}${listHtml}
    ${another ? `<h4 style="margin-top:18px">${T('account.newTitle')}</h4>` : ''}
    <p class="hint">${T(another ? 'account.anotherHint' : 'account.intro', { version: versionId })}</p>
    ${!up ? `<div class="note" style="margin-bottom:10px">${T('account.notRunning', { version: versionId })}</div>` : ''}
    <label class="fld">${T('account.name')}</label><input class="txt mono" data-model="account.name" value="${esc(ac.name)}" placeholder="${T('account.namePh')}" maxlength="20" autocomplete="off" spellcheck="false">
    ${pv ? `<label class="fld">${T('account.password')}</label><input class="txt mono" type="password" data-model="account.password" value="${esc(ac.password)}" placeholder="${T('account.passwordPh')}" autocomplete="new-password">` : ''}
    <label class="fld">${T('account.template')}</label>
    <div class="grid2">${templates.map((tp) => `<div class="opt ${tpl === tp.id ? 'sel' : ''}" data-act="accountTemplate" data-arg="${esc(versionId + ':' + tp.id)}"><div class="h">${esc(tp.label || tp.id)}</div><p>${T(I18N.has('account.tpl.' + tp.id + '.sub') ? 'account.tpl.' + tp.id + '.sub' : 'account.tpl.other.sub')}</p></div>`).join('')}</div>
    ${ac.running || ac.lines.length ? `<div class="console" style="margin-top:12px"><div class="log" id="acctlog">${ac.lines.map((l) => `<div>${esc(l)}</div>`).join('')}</div></div>` : ''}
    ${ac.error ? `<div class="note" style="margin-top:10px">${esc(ac.error)}</div>` : ''}
    <div class="flex between center" style="margin-top:14px"><span class="hint" style="margin:0">${limits}</span>
      <div class="flex gap">${another ? `<button class="btn ghost" data-act="accountAnotherCancel" data-arg="${esc(versionId)}" ${ac.running ? 'disabled' : ''}>${T('common.cancel')}</button>` : ''}
        <button class="btn" data-act="accountCreate" data-arg="${esc(versionId)}" ${dis}>${ac.running ? T('account.creating') : T('account.create')}</button></div></div>
  </div>`;
}

// Player-mode server page: read-only status from the public snapshot (or the SDK probe) + the
// account card. No admin buttons — the agent would refuse them anyway.
function playerServer() {
  const pub = S.pub.status;
  const cards = S.versions.map((v) => {
    const e = pub && pub.versions && pub.versions[v.id];
    // Only fresh data asserts anything: while checking, after an error, or before a new server was read,
    // the previous snapshot must never be drawn under this server's name. statusUnknown (docker did not
    // answer on the box, agent 3.2) makes every up:false fabricated: unverified, not OFFLINE.
    const st = S.srv.state === 'checking' ? 'checking' : S.srv.state !== 'ok' || !pub ? 'unknown'
      : !e || e.present === false ? 'absent' : e.up ? (e.healthy === false ? 'working' : 'live') : pub.statusUnknown ? 'unknown' : 'down';
    const meta = SRVMETA[st];
    const acc = e && e.defaultAccount ? `<div><small style="color:var(--muted);font-weight:800;font-size:10.5px">${T('server.card.accountLabel')}</small><div style="font-weight:800" class="mono">${esc(e.defaultAccount)}</div></div>` : '';
    return `<div class="cmdcard" style="padding:20px">
      <div class="flex between center"><div class="serif" style="font-weight:700;font-size:18px">${T('server.card.title', { id: v.id })}</div>
        <span class="badge" style="background:rgba(0,0,0,.06);color:${meta[1]}"><span class="dot" style="background:${meta[1]}"></span>${T(meta[0])}</span></div>
      <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;line-height:1.5;margin:11px 0 0">${st === 'working' ? T('server.player.degraded') : T(SRVNOTE[st])}</p>
      <div class="flex gap wrap" style="margin:14px 0 18px">
        <div><small style="color:var(--muted);font-weight:800;font-size:10.5px">${T('server.card.serverLabel')}</small><div style="font-weight:800">${esc(S.serverAddr.host)}:${S.serverAddr.port}</div></div>${acc}
      </div>
      <div class="flex gap"><button class="btn ghost" style="flex:1;padding:11px" data-act="checkServers" ${S.srv.state === 'checking' ? 'disabled' : ''}>${T('server.card.check')}</button></div>
    </div>`;
  }).join('');
  // The account card picks its own version (acctVersionCur) — not the Library's selection.
  const acctId = acctVersionCur();
  const errNote = S.srv.state === 'error' ? `<div class="note" style="margin-top:16px">${T('server.player.unreachable', { error: S.srv.error })}</div>` : '';
  const src = S.srv.info && S.srv.info.public ? T('server.player.viaAgent', { name: (pub && pub.name) || S.serverAddr.host }) : T('server.player.viaProbe');
  return `<div class="pad"><h1 class="title">${T('server.title')}</h1><p class="sub">${T('server.player.sub')}</p>
    <div class="grid2">${cards}</div>${errNote}
    <div class="agentbar" style="margin-top:16px"><span class="dot ${S.srv.state === 'ok' ? 'live' : 'unknown'}"></span><span>${esc(S.serverAddr.host)}:${S.serverAddr.port}</span><small>· ${src}</small></div>
    ${acctId ? accountCard(acctId) : ''}
  </div>`;
}

// ── admin: "Create a player account" (agent 3.5: POST /server/account/create) ──
// An in-game login for the admin or a friend, with any progress imported on the stack — the Player
// accounts policy and its quotas do not apply (it is an admin job, behind the token). An older agent
// has no such route: the card then falls back to the player route while that policy opens the version
// (policy + limits apply, and it says so), else it only says what to upgrade.
// One shape for every reset: boot and another server (srvRetarget). tpl[v] = the last GET
// /server/templates of v ({list, loading} — list null when it failed); tplSeq[v] = its request seq;
// created = this session's results, newest first (a generated password lives only here, in memory);
// pending = {remember, name} of the admin-route create in flight (runServerJob keeps no extras).
// version = the chip picked (null = acctNewCur decides — until the admin touches the form, which pins
// shownVersion, the version the card last DREW); tplBy = the progress choice per version, as on the
// player card; logVersion = the version the player-route console / error belong to.
function acctNewBlank() { return { version: null, shownVersion: '', name: '', password: '', tplBy: {}, remember: false, tpl: {}, tplSeq: {}, created: [], running: false, lines: [], error: null, pending: null, logVersion: '' }; }
const acctNewCur = () => S.acctNew.version || srvLiveId() || S.selectedVersionId || (S.versions[0] && S.versions[0].id) || '';
// The agent's own rules (NAME_RE / PASSWORD_RE in gio_agent.py): said here before a job is started.
const ACCOUNT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{2,19}$/;
const ACCOUNT_PASSWORD_RE = /^[\x21-\x7e]{8,64}$/;
// How the card creates the account: 'admin' (agent 3.5+), 'public' (an older agent whose signup policy
// opens this version — read from the admin /status, which carries the whole policy), 'none', or ''
// while there is no status to tell.
function acctNewRoute(v) {
  const info = S.srv.info;
  if (!info) return '';
  if (agentAtLeast('3.5')) return 'admin';
  const su = info.policy && info.policy.signup;
  return su && su.enabled && su.versions && su.versions[v] ? 'public' : 'none';
}
// The progress templates of a stack, [{id, label, ready}]: the agent's GET /server/templates when it
// answered (loadAcctTemplates), else the admin /status records ({id: {createdAt, error}} — no labels
// there, and a template never imported has no record at all). ready = imported without an error, the
// agent's own template_ready test.
function acctNewStock(v) {
  const r = S.acctNew.tpl[v];
  if (r && Array.isArray(r.list)) return r.list.map((x) => ({ id: x.id, label: x.label, ready: x.imported }));
  const e = S.srv.versions && S.srv.versions[v];
  const recs = e && e.templates && typeof e.templates === 'object' && !Array.isArray(e.templates) ? e.templates : {};
  return Object.keys(recs).map((id) => ({ id, label: id, ready: !!(recs[id] && recs[id].createdAt && !recs[id].error) }));
}
// What the form offers ({list, notReady}): the admin route = the new account + every imported
// template; the player route = what the policy allows of those (its "fresh" included only when listed —
// the agent refuses anything the policy does not name). notReady = templates known but not imported.
function acctNewChoices(v, route) {
  const fresh = { id: 'fresh', label: t('account.tpl.fresh') };
  const stock = acctNewStock(v).filter((x) => x.id !== 'fresh');
  const ready = stock.filter((x) => x.ready);
  if (route === 'admin') return { list: [fresh].concat(ready), notReady: stock.filter((x) => !x.ready) };
  const vp = ((((S.srv.info || {}).policy || {}).signup || {}).versions || {})[v];
  const allowed = vp && Array.isArray(vp.templates) ? vp.templates : ['fresh'];
  return {
    list: (allowed.includes('fresh') ? [fresh] : []).concat(ready.filter((x) => allowed.includes(x.id))),
    notReady: allowed.filter((id) => id !== 'fresh' && !ready.some((x) => x.id === id))
      .map((id) => ({ id, label: (stock.find((x) => x.id === id) || {}).label || id })),
  };
}
// A template's label for a result row (the agent's result names the id only).
function acctNewTplLabel(v, id) {
  if (!id || id === 'fresh') return t('account.tpl.fresh');
  const x = acctNewStock(v).find((s) => s.id === id);
  return (x && x.label) || id;
}
// A finished create, into the session list (newest first, ten at most). key = a stable handle for the
// row's copy button: the password never goes into an attribute.
let acctNewKey = 0;
function acctNewPush(e) {
  S.acctNew.created.unshift(Object.assign({ key: ++acctNewKey }, e));
  if (S.acctNew.created.length > 10) S.acctNew.created.length = 10;
}
function acctNewCard() {
  const a = S.acctNew;
  const v = acctNewCur();
  const chips = S.versions.map((x) => `<button class="lg-chip ${v === x.id ? 'on' : ''}" data-act="acctNewVersion" data-arg="${esc(x.id)}">${T('common.versionN', { id: x.id })}</button>`).join('');
  const route = acctNewRoute(v);
  const f = srvFacts(v);
  const agent = (S.srv.info && S.srv.info.agent) || '?';
  const choices = route === 'admin' || route === 'public' ? acctNewChoices(v, route) : null;
  let body;
  // No status yet: nothing can be said (a failed read has the page's own error note, and without a
  // configured server there is nothing being checked).
  if (!route) body = S.serverConfigured && S.srv.state !== 'error' ? `<p class="hint">${T('common.checking')}</p>` : '';
  else if (route === 'none' || (route === 'public' && !choices.list.length)) body = `<div class="note">${T('server.acctNew.tooOld', { agent })}</div>`;
  else if (!f || !f.present) body = `<div class="note">${T('server.acctNew.notPresent', { version: v })}</div>`;
  else {
    const admin = route === 'admin';
    const verify = passwordVerified(v);
    // Keep the choice valid — for THIS version only (see accountCard): the new account on the admin
    // route (always offered first); on the player route the first the policy allows (it may not list
    // the new account).
    if (!choices.list.some((tp) => tp.id === a.tplBy[v])) a.tplBy[v] = choices.list[0].id;
    const tpl = a.tplBy[v];
    a.shownVersion = v;
    // The password field: the admin route lets the agent generate one when verification is on (the
    // field is optional) — except for the admin's OWN login ("Show it as my login"): the launcher then
    // calls it "the password you chose", so it must be one the admin typed. The player route needs it
    // typed; without verification nobody asks for one.
    const pwField = verify
      ? `<label class="fld">${T('account.password')}</label><input class="txt mono" type="password" data-model="acctNew.password" value="${esc(a.password)}" placeholder="${T('account.passwordPh')}" autocomplete="new-password">
        ${admin ? `<p class="hint" style="margin:8px 0 0">${T(a.remember ? 'server.acctNew.passwordMine' : 'server.acctNew.passwordHint')}</p>` : ''}`
      : `<p class="hint" style="margin:12px 0 0">${T('server.acctNew.passwordAny')}</p>`;
    const tplCards = choices.list.map((tp) => {
      const sub = 'account.tpl.' + tp.id + '.sub';
      return `<div class="opt ${tpl === tp.id ? 'sel' : ''}" data-act="acctNewTemplate" data-arg="${esc(v + ':' + tp.id)}"><div class="h">${esc(tp.label || tp.id)}</div>${I18N.has(sub) ? `<p>${T(sub)}</p>` : ''}</div>`;
    }).join('');
    // "Import them now" beside the note: POST /server/templates/ensure is an admin call on every agent
    // that has templates (3.0+), so it serves the player route too. A job — dead while another runs or
    // while the stack is stopped (the agent refuses to import into a stopped stack).
    const notReady = choices.notReady.length
      ? `<div class="flex center gap" style="margin-top:10px"><div class="note" style="flex:1">${T('server.acctNew.tplNotReady', { list: choices.notReady.map((x) => x.label || x.id).join(', ') })}</div>
        <button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="acctNewImport" data-arg="${esc(v)}" ${srvBusy() || !f.up ? 'disabled' : ''}>${T('server.acctNew.importTemplates')}</button></div>` : '';
    // The admin route runs as a server job (one at a time, its log in the page's console); the player
    // route as the launcher's own account.create, streamed into this card. "Creating...", its console
    // and its error only on the chip of the version being created — another chip's button is merely
    // dead meanwhile.
    const running = admin ? jobHere() && S.srvJob.kind === 'accountcreate' && S.srvJob.version === v
      : a.running && !(S.acctPending && S.acctPending.version !== v);
    const dis = (admin ? srvBusy() : a.running || !!S.acctPending) || !f.up ? 'disabled' : '';
    body = `${admin ? '' : `<div class="note" style="margin-bottom:10px">${T('server.acctNew.viaPublic', { agent })}</div>`}
      ${!f.up ? `<div class="note" style="margin-bottom:10px">${T('server.acctNew.notLive', { version: v })}</div>` : ''}
      <label class="fld">${T('account.name')}</label><input class="txt mono" data-model="acctNew.name" value="${esc(a.name)}" placeholder="${T('account.namePh')}" maxlength="20" autocomplete="off" spellcheck="false">
      ${pwField}
      <label class="fld">${T('account.template')}</label>
      <div class="grid2">${tplCards}</div>${notReady}
      <div class="line" style="margin-top:12px"><div><b>${T('server.acctNew.remember')}</b><small>${T('server.acctNew.rememberSub')}</small></div><button class="toggle ${a.remember ? 'on' : ''}" data-act="acctNewRemember"><span class="knob"></span></button></div>
      ${!admin && a.logVersion === v && (a.running || a.lines.length) ? `<div class="console" style="margin-top:12px"><div class="log" id="acctnewlog">${a.lines.map((l) => `<div>${esc(l)}</div>`).join('')}</div></div>` : ''}
      ${!admin && a.logVersion === v && a.error ? `<div class="note" style="margin-top:10px">${esc(a.error)}</div>` : ''}
      <div class="flex" style="justify-content:flex-end;margin-top:14px"><button class="btn" data-act="acctNewCreate" data-arg="${esc(v)}" ${dis}>${running ? T('account.creating') : T('account.create')}</button></div>`;
  }
  // This session's results — never a lock on the form: the next account can be typed right away.
  const rows = a.created.map((c) => {
    const uid = c.uid != null && c.uid !== '' ? t('server.acctNew.uid', { uid: c.uid }) : t('server.acctNew.uidLater');
    const pw = c.password
      ? `<div class="crow"><div><small>${c.generated ? T('server.acctNew.pwGenerated') : T('lib.creds.password')}</small><b class="mono pick">${esc(c.password)}</b></div><button class="crcopy" data-act="acctNewCopyPw" data-arg="${c.key}">${T('common.copy')}</button></div>`
      : `<div class="crow"><div><small>${T('lib.creds.password')}</small><b class="pw">${T('lib.creds.any')}</b></div></div>`;
    return `<div class="creds" style="margin-top:8px">
      <div class="crow"><div><small>${T('server.acctNew.rowLabel', { version: c.version, template: c.templateLabel, uid })}</small><b class="mono pick">${esc(c.name)}</b></div><button class="crcopy" data-act="copyAccount" data-arg="${esc(c.name)}">${T('common.copy')}</button></div>
      ${pw}</div>`;
  }).join('');
  const results = rows ? `<label class="fld">${T('server.acctNew.created')}</label>${rows}<p class="hint" style="margin:10px 0 0">${T('server.acctNew.loginHint')}</p>` : '';
  return `<div class="sect"><h4>${T('server.acctNew.title')}</h4><p class="hint">${T('server.acctNew.hint')}</p>
    <div class="flex gap wrap" style="margin-bottom:12px">${chips}</div>
    ${body}${results}
  </div>`;
}

// ── admin: player-account policy ──
// A version's template chips, [{id, label, ready}]: the new account, then the stock the admin card
// reads (acctNewStock — the labels and the imports of GET /server/templates, else the /status records,
// failed imports included), then any id the policy already names, so a chip it names can always be
// switched off. stockKnown = one of those two reads is there: without it readiness is not known and
// nothing may be called "not imported".
function policyTplChips(v, allowed) {
  const out = [{ id: 'fresh', label: t('account.tpl.fresh'), ready: true }];
  const add = (id, label, ready) => { if (id && !out.some((x) => x.id === id)) out.push({ id, label: label || id, ready: !!ready }); };
  acctNewStock(v).forEach((x) => add(x.id, x.label, x.ready));
  allowed.forEach((id) => add(id, id, false));
  return out;
}
function acctNewStockKnown(v) {
  const r = S.acctNew.tpl[v];
  if (r && Array.isArray(r.list)) return true;
  const e = S.srv.versions && S.srv.versions[v];
  return !!(e && e.templates && typeof e.templates === 'object' && !Array.isArray(e.templates));
}
function policyCard() {
  const pol = S.policy.data;
  if (!pol) return `<div class="sect"><h4>${T('policy.title')}</h4><p class="hint">${S.policy.loading ? T('common.checking') : T('policy.unavailable')}</p>
    <button class="btn ghost" style="padding:8px 14px;font-size:12px" data-act="policyLoad">${T('common.retry')}</button></div>`;
  const row = (label, sub, on, act, arg) => `<div class="line"><div><b>${label}</b><small>${sub}</small></div><button class="toggle ${on ? 'on' : ''}" data-act="${act}" ${arg ? `data-arg="${esc(arg)}"` : ''}><span class="knob"></span></button></div>`;
  let html = `<div class="sect"><h4>${T('policy.title')}</h4><p class="hint">${T('policy.hint')}</p>
    ${row(T('policy.signup'), T('policy.signupSub'), !!(pol.signup && pol.signup.enabled), 'policyToggleSignup')}`;
  S.versions.forEach((v) => {
    const pv = (pol.signup && pol.signup.versions && pol.signup.versions[v.id]) || { templates: ['fresh'], maxPerDay: 50 };
    const allowed = Array.isArray(pv.templates) ? pv.templates : [];
    const avail = policyTplChips(v.id, allowed);
    // Agent 3.5 offers players only the templates imported on the stack: an allowed one that is not
    // (never imported, or its import failed) is silently absent from their card. Said here, as a note —
    // the chip stays live: allowing it now is fine, it is offered once imported.
    const unready = agentAtLeast('3.5') && acctNewStockKnown(v.id) ? avail.filter((x) => x.id !== 'fresh' && !x.ready && allowed.includes(x.id)) : [];
    html += `<div class="line"><div><b>${T('policy.versionTemplates', { version: v.id })}</b><small>${T('policy.versionTemplatesSub')}</small>
      <div class="flex gap wrap" style="margin-top:8px">${avail.map((tp) => `<button class="lg-chip ${allowed.includes(tp.id) ? 'on' : ''}" data-act="policyToggleTemplate" data-arg="${esc(v.id + ':' + tp.id)}">${esc(tp.label)}</button>`).join('')}</div></div>
      <div style="text-align:right"><small style="display:block;color:var(--muted);font-size:10.5px;font-weight:800">${T('policy.maxPerDay')}</small><input class="txt" style="width:90px;padding:8px 10px" data-policy-max data-arg="${esc(v.id)}" value="${esc(String(pv.maxPerDay == null ? 50 : pv.maxPerDay))}">
        ${Number.isInteger(pv.maxPerPlayer) ? `<small style="display:block;color:var(--muted);font-size:10.5px;font-weight:800;margin-top:8px" title="${T('policy.maxPerPlayerSub')}">${T('policy.maxPerPlayer')}</small><input class="txt" style="width:90px;padding:8px 10px" data-policy-maxplayer data-arg="${esc(v.id)}" value="${esc(String(pv.maxPerPlayer))}" title="${T('policy.maxPerPlayerSub')}">` : ''}</div></div>
      ${unready.length ? `<div class="note" style="margin:0 0 10px">${T('policy.tplNotReady', { list: unready.map((x) => x.label).join(', ') })}</div>` : ''}`;
  });
  html += row(T('policy.playerCommands'), T('policy.playerCommandsSub'), !!pol.playerCommands, 'policyTogglePlayerCommands');
  html += `<div class="flex between center" style="margin-top:14px"><span class="hint" style="margin:0">${T('policy.note')}</span><button class="btn" data-act="policySave">${T('policy.save')}</button></div></div>`;
  return html;
}

// ── admin: server secrets (per version) ──
// The MUIP line of GET /server/secrets, in words: never the key (the agent answers a fingerprint
// only), and — agent 3.4 — whether it is still the archive's published one, which the agent replaces
// with a random key at the next Prepare server / Start on its own.
function muipKeyText(m) {
  if (!m || !m.set) return t('secrets.muipUnset');
  return t(m.vendorDefault ? 'secrets.muipVendor' : 'secrets.muipSet', { fp: m.fingerprint || '' });
}
function secretsCard() {
  const s = S.secrets;
  const versions = S.versions.map((v) => v.id);
  const cur = s.version || versions[0] || '';
  const tabs = versions.map((id) => `<button class="lg-chip ${cur === id ? 'on' : ''}" data-act="secretsVersion" data-arg="${esc(id)}">${T('common.versionN', { id })}</button>`).join('');
  const d = s.data && s.version === cur ? s.data : null;
  const field = (key, label, sub) => `<div><label class="fld">${label}</label><div class="flex gap"><input class="txt mono" type="password" data-model="secrets.form.${key}" value="${esc(s.form[key] || '')}" placeholder="${esc(d && d[key] ? t('secrets.keep') : '')}" autocomplete="new-password"><button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="secretsGen" data-arg="${key}">${T('deploy.net.generate')}</button></div><small class="hint">${sub}</small></div>`;
  return `<div class="sect"><h4>${T('secrets.title')}</h4><p class="hint">${T('secrets.hint')}</p>
    <div class="flex gap wrap" style="margin-bottom:12px">${tabs}</div>
    ${!d ? `<button class="btn ghost" style="padding:8px 14px;font-size:12px" data-act="secretsLoad" data-arg="${esc(cur)}">${s.loading ? T('common.checking') : T('secrets.load')}</button>` : `
    <div class="grid2">
      ${field('mysqlRoot', T('secrets.mysqlRoot'), T('secrets.mysqlRootSub'))}
      ${field('internal', T('secrets.internal'), T('secrets.internalSub'))}
      ${field('flask', T('secrets.flask'), T('secrets.flaskSub'))}
      ${field('muip', T('secrets.muip'), esc(muipKeyText(d.muip)))}
    </div>
    <div class="note" style="margin-top:12px">${d.bootstrapped ? T('secrets.restartWarning') : T('secrets.freshNote')}</div>
    <div class="flex between center" style="margin-top:12px"><button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="secretsShow" data-arg="${esc(cur)}">${T('secrets.show')}</button><button class="btn" data-act="secretsApply" data-arg="${esc(cur)}" ${srvBusy() ? 'disabled' : ''}>${T('secrets.apply')}</button></div>`}
  </div>`;
}

// ── admin: pathfinding server (per version, agent 3.4) ──
// pathfindingserver computes the routes monsters and NPCs walk and needs about 4 GB of RAM of its own:
// on a small box the kernel kills it and restarts it in a loop, and the whole server slows down. The
// agent excludes it through the compose profile `donotstart` — the vendor's own way, used on oaserver
// — on BOTH compose files, and stops / starts the container on a running stack. The row shows the
// file's truth; null (older agent, an absent stack, a foreign compose) asserts nothing and is dead.
function pathfindingCard() {
  const busy = srvBusy();
  const rows = S.versions.map((v) => {
    const f = srvFacts(v.id);
    const on = !!(f && f.pathfinding);
    const sub = !f || f.pathfinding === null ? T('server.pathfinding.unknown') : on ? T('server.pathfinding.onSub') : T('server.pathfinding.offSub');
    const dis = busy || !f || !f.present || f.pathfinding === null ? 'disabled' : '';
    return `<div class="line"><div><b>${T('common.versionN', { id: v.id })}</b><small>${sub}</small></div>
      <button class="toggle ${on ? 'on' : ''}" data-act="pathfindingToggle" data-arg="${esc(v.id)}" ${dis}><span class="knob"></span></button></div>`;
  }).join('');
  return `<div class="sect"><h4>${T('server.pathfinding.title')}</h4><p class="hint">${T('server.pathfinding.hint')}</p>${rows}</div>`;
}

// ── admin: official 2021 hotpatch mirror (per version, agent 3.1) ──
// The agent mirrors the official client hotfix outputs and makes the dispatch advertise them, so every
// client hotpatches itself at login exactly like the official one did in 2021. Read-only here except
// the two confirmed jobs; the numbers come from GET /server/hotpatch (never inferred).
// Decimal MB (/1e6), the same convention as the agent's job log and the docs (127.1 MB for 1.6).
const mb = (n) => (Math.max(0, Number(n) || 0) / 1e6).toFixed(1);
// Decimal GB (/1e9), one digit: the voice packs are 3–9 GB per language (the agent's log says the same).
// An amount under 0.05 GB is not printed as "0.0" (what a stopped run leaves on 1.6 is a few tens of MB).
const gb = (n) => { const v = Math.max(0, Number(n) || 0) / 1e9; return v > 0 && v < 0.05 ? Math.max(v, 0.01).toFixed(2) : v.toFixed(1); };
// "1.9 GB" — decimal GB with the unit from the dictionary: one form for every size the server pages show.
const sizeGb = (n) => t('common.sizeGb', { gb: gb(n) });
// The ready-made server packages on the Internet Archive (agent/payloads/<v>/stack.json carries the
// same figures): the install form quotes them before any agent exists to report the real ones.
const STACK_SIZES = { '1.6': 1870180624, '2.8': 2453575625 };
// One shape for every reset: boot, another server (srvRetarget), an agent install (deploy.done).
function hotpatchBlank() { return { version: null, data: null, loading: false, voiceSel: null, voiceKey: '', busy: null, voiceStop: null }; }
// The tab the card shows — and the only version a finished job may re-read (never a tab switch).
const hotpatchCur = () => S.hotpatch.version || S.selectedVersionId || (S.versions[0] && S.versions[0].id) || '';
// The loaded reply, but only as THIS version's: loadHotpatch names the tab before the reply is in, and
// the agent's payload says which version it describes (one that does not say is taken at its word).
const hotpatchDataOf = (id) => {
  const h = S.hotpatch, d = h.data;
  return d && h.version === id && (d.version == null || String(d.version) === String(id)) ? d : null;
};

// ── voice packs of the hotpatch mirror (agent 3.3) ──
// Once real revisions are advertised the client checks every pack of the voice language it USES (on a
// fresh profile: the Windows display language) and downloads a missing one from the mirror. Those are
// packs of the BASE build (3–9 GB per language), which the agent fetches only for the languages the
// admin selected — a pack that is already cached is served whatever the selection. An agent older
// than 3.3 sends no `voice` and nothing is drawn.
const hotpatchVoiceOf = (d) => (d && d.available && d.voice && d.voice.base && Array.isArray(d.voice.languages) && d.voice.languages.length ? d.voice : null);
// The 2.8 game's downloader gives every GET 30 s for the first byte and stops after about three
// minutes of tries (1.6 opens with a HEAD that waits 100 s, and its packs are 68 MB at most): a pack
// the server still has to fetch from the CDN fails the first logins there — on 2.8 mirror FIRST.
const voiceShortWait = (id) => String(id) === '2.8';
// A voice job of this version runs ON THIS SERVER: the one this launcher follows (jobHere — never one
// it started on another server before the address was changed), or the one /status names (started
// elsewhere, or ours after the link was lost — the agent's kind is 'hotpatch-voice').
const voiceJobRunning = (id) => {
  const j = S.srvJob, b = S.hotpatch.busy;
  if (jobHere() && j.kind === 'hotpatch.voice' && String(j.version) === String(id)) return true;
  return !!(b && b.kind === 'hotpatch-voice' && b.version === String(id) && S.srv.info && S.srv.info.busy);
};
// Either hotpatch job of this version running ON THIS SERVER: the Enable / "Fetch missing files" run
// ('hotpatch') and the voice packs run ('hotpatch.voice'; the agent's own kind is 'hotpatch-voice').
// Both spend nearly all their time downloading files, which is exactly what this card counts —
// refreshHotpatchQuiet follows them.
const hotpatchJobRunning = (id) => {
  const j = S.srvJob, b = S.hotpatch.busy;
  if (jobHere() && (j.kind === 'hotpatch' || j.kind === 'hotpatch.voice') && String(j.version) === String(id)) return true;
  return !!(b && b.version === String(id) && S.srv.info && S.srv.info.busy);
};
// The Enable / Disable run of THIS version, as the State line has to show it: an Enable flips the
// agent's stored state only once every file is fetched (minutes), so until then a card that just
// read the agent would keep saying "Disabled" over a run that is enabling it — as if the button had
// done nothing. Ours carries the intent it was started with (runServerJob); one started elsewhere
// (/status busy) does not say which way it goes, so it is only named as a run.
const hotpatchApplyJob = (id) => {
  const j = S.srvJob, b = S.hotpatch.busy;
  if (jobHere() && j.kind === 'hotpatch' && String(j.version) === String(id))
    return j.enabled === true ? 'enabling' : j.enabled === false ? 'disabling' : 'working';
  return b && b.kind === 'hotpatch' && b.version === String(id) && S.srv.info && S.srv.info.busy ? 'working' : '';
};
const HOTPATCH_APPLY = { enabling: 'hotpatch.stateEnabling', disabling: 'hotpatch.stateDisabling', working: 'hotpatch.stateWorking' };
// The followed voice job belongs to ANOTHER server: this card draws neither its running note nor a
// Stop (the cancel call goes to the configured host = this one) — it says where that job lives.
const voiceJobElsewhere = () => jobElsewhere() && S.srvJob.kind === 'hotpatch.voice';
// The server's rows against the admin's ticks — one reading for the section and its confirm dialogs.
// Names and dates in here are the agent's text: escaped where they are drawn.
function voicePlan(d) {
  const v = hotpatchVoiceOf(d);
  if (!v) return null;
  const sel = S.hotpatch.voiceSel;
  const langs = v.languages.filter((l) => l && typeof l.name === 'string' && l.name).map((l) => {
    const files = Number(l.files) || 0, cached = Number(l.cached) || 0, selected = !!l.selected;
    // diskBytes (agent 3.3): EVERYTHING in the language's folder, a stopped run's .part files included
    // — what the job's purge deletes and what the agent credits for it. cachedBytes counts complete
    // packs only and stands in for an agent that does not send diskBytes.
    const cachedBytes = Number(l.cachedBytes) || 0, disk = Number(l.diskBytes);
    return {
      name: l.name, files, cached, bytes: Number(l.bytes) || 0, cachedBytes,
      diskBytes: l.diskBytes != null && Number.isFinite(disk) ? Math.max(disk, cachedBytes) : cachedBytes,
      selected, ticked: sel ? sel[l.name] === true : selected, full: files > 0 && cached >= files,
      requested: Number(l.requested) || 0, requestedAt: typeof l.requestedAt === 'string' ? l.requestedAt : '',
      // Agent 3.4: where the last mirror run took this language's packs from ('bundle' = the
      // archive.org bundle, 'cdn', 'bundle+cdn'); '' on older agents.
      source: typeof l.source === 'string' ? l.source : '',
    };
  });
  if (!langs.length) return null;
  const ticked = langs.filter((l) => l.ticked);
  // What the job's `purge` deletes: every unticked language with anything on disk — complete packs or
  // only the .part a stopped run left (invisible in the rows, and this job is the only way to delete it).
  const loose = langs.filter((l) => !l.ticked && l.diskBytes > 0);
  const download = ticked.reduce((n, l) => n + Math.max(0, l.bytes - l.cachedBytes), 0);
  const looseBytes = loose.reduce((n, l) => n + l.diskBytes, 0);
  // Free space against the agent's rule (a run must leave `reserve` free, else 409). The agent credits
  // the purge only when it was ASKED for, so there are two verdicts: `hard` = does not fit even with
  // the purge, `needsPurge` = fits only with the purge box ticked. A heads-up only — the agent's own
  // count is the authority (it credits half-fetched files).
  const free = typeof v.free === 'number' ? v.free : null, reserve = Math.max(0, Number(v.reserve) || 0);
  const hard = free != null && download > 0 && free + looseBytes < download + reserve;
  const needsPurge = free != null && download > 0 && !hard && free < download + reserve;
  // Selected, not complete on disk: the server fetches such a pack when a player's game asks for it —
  // a fallback, not a plan (voiceShortWait) — and not at all with on-demand fetching switched off
  // (d.upstream === false: the mirror answers a plain 404 until the mirror job has run).
  const incomplete = langs.filter((l) => l.selected && !l.full);
  return {
    v, langs, ticked, loose, download, looseBytes, free, reserve, hard, needsPurge,
    changed: langs.some((l) => l.ticked !== l.selected),
    diskBytes: langs.reduce((n, l) => n + l.diskBytes, 0), // what Disable + purge deletes with the branch
    // Neither selected nor complete on disk — the mirror refuses it: with the fixes enabled a player
    // of that voice language fails the download at every login.
    unserved: langs.filter((l) => !l.selected && !l.full),
    unfetched: d.upstream === false ? incomplete : [],
    onDemand: d.upstream === false ? [] : incomplete,
  };
}
// The ticks follow the server's selection when the server / version changes and when the selection
// itself changed there (a finished job, another admin); a background refresh of the same version with
// the same selection keeps the admin's unsaved ticks. voiceKey = '' makes the next load re-take them.
function syncVoiceSel(id, d, target) {
  const h = S.hotpatch, v = hotpatchVoiceOf(d);
  if (!v) { h.voiceSel = null; h.voiceKey = ''; return; }
  const rows = v.languages.filter((l) => l && typeof l.name === 'string' && l.name);
  const key = [target == null ? srvTarget() : target, id, JSON.stringify(rows.filter((l) => l.selected).map((l) => l.name))].join('|');
  if (h.voiceSel && h.voiceKey === key) return;
  h.voiceKey = key; h.voiceSel = {};
  rows.forEach((l) => { h.voiceSel[l.name] = !!l.selected; });
}
function hotpatchVoiceSection(cur, d) {
  const p = voicePlan(d);
  if (!p) return '';
  const v = p.v, busy = srvBusy();
  const rows = p.langs.map((l) => {
    const st = l.full ? ['hotpatch.voice.stateMirrored', 'var(--ok)'] : l.cached > 0 ? ['hotpatch.voice.statePartial', 'var(--warn)']
      : l.selected ? (d.upstream === false ? ['hotpatch.voice.stateSelectedOff', 'var(--bad)'] : ['hotpatch.voice.stateSelected', 'var(--warn)'])
      : ['hotpatch.voice.stateNone', 'var(--muted)'];
    // Requests the mirror REFUSED since the agent started: a player needs this language.
    const asked = l.requested > 0 && !l.selected
      ? `<small style="color:var(--bad);font-weight:800">${T(l.requestedAt ? 'hotpatch.voice.requested' : 'hotpatch.voice.requestedNoTime', { n: l.requested, at: l.requestedAt })}</small>` : '';
    return `<div class="line"><div><b>${esc(l.name)}</b><small>${T('hotpatch.voice.rowSub', { gb: gb(l.bytes), cached: l.cached, total: l.files })}${l.source === 'bundle' ? ` · ${T('hotpatch.voice.fromBundle')}` : ''}</small>${asked}</div>
      <div class="flex gap center"><span style="font-weight:800;font-size:12px;text-align:right;color:${st[1]}">${T(st[0])}</span><button class="toggle ${l.ticked ? 'on' : ''}" data-act="hotpatchVoiceToggle" data-arg="${esc(l.name)}" ${busy ? 'disabled' : ''}><span class="knob"></span></button></div></div>`;
  }).join('');
  const note = (html) => `<div class="note" style="margin-top:10px">${html}</div>`;
  const names = (list) => list.map((l) => l.name).join(', ');
  const freeLine = p.free == null ? '' : `<p class="hint" style="margin:10px 0 0">${T(p.reserve > 0 ? 'hotpatch.voice.free' : 'hotpatch.voice.freeOnly', { free: gb(p.free), reserve: gb(p.reserve) })}</p>`;
  // The agent refuses (409) a run that would eat into its reserve — see voicePlan for the two verdicts.
  const space = { need: gb(p.download), free: gb(p.free), reserve: gb(p.reserve), langs: names(p.loose), gb: gb(p.looseBytes) };
  const noSpace = p.hard ? note(T('hotpatch.voice.noSpace', space)) : p.needsPurge ? note(T('hotpatch.voice.needsPurge', space)) : '';
  const notIndexed = !v.indexed ? `<p class="hint" style="margin:10px 0 0">${T('hotpatch.voice.notIndexed')}</p>` : '';
  const unserved = d.enabled && p.unserved.length ? note(T('hotpatch.voice.unserved', { langs: names(p.unserved) })) : '';
  // Selected but not complete on the box, while clients are being sent here: refused outright with
  // on-demand fetching off, left to the on-demand fallback otherwise (which the 2.8 game does not wait for).
  const unfetched = d.enabled && p.unfetched.length ? note(T('hotpatch.voice.unfetched', { langs: names(p.unfetched) })) : '';
  const onDemand = d.enabled && p.onDemand.length
    ? note(T('hotpatch.voice.onDemand', { langs: names(p.onDemand) }) + (voiceShortWait(cur) ? ' ' + T('hotpatch.voice.shortWait') : '')) : '';
  // The voice job this launcher follows runs on another server: why every button here is dead, and
  // where that job can be stopped (voiceJobElsewhere) — instead of a running note + Stop of this card.
  const elsewhere = voiceJobElsewhere()
    ? note(T('hotpatch.voice.followedElsewhere', { host: S.srvJob.host || '—', version: S.srvJob.version })) : '';
  // Nothing to do = the ticks are the server's selection, every selected language is complete and no
  // unticked language has anything left on disk (this job is also the only way to delete that).
  const idle = !p.changed && !p.loose.length && p.langs.every((l) => !l.selected || l.full);
  // While this version's voice job runs, the (dead) Mirror button gives way to Stop: one click, then
  // dead too until the job's state changes (S.hotpatch.voiceStop).
  const running = voiceJobRunning(cur), stopSent = S.hotpatch.voiceStop === cur;
  const todo = running ? T('hotpatch.voice.running')
    : [p.changed ? T('hotpatch.voice.changed') : '', p.download > 0 ? T('hotpatch.voice.todo', { gb: gb(p.download) }) : ''].filter(Boolean).join(' · ');
  const action = running
    ? `<button class="btn ghost" data-act="hotpatchVoiceStop" data-arg="${esc(cur)}" ${stopSent ? 'disabled' : ''}>${T(stopSent ? 'hotpatch.voice.stopping' : 'hotpatch.voice.stop')}</button>`
    : `<button class="btn" data-act="hotpatchVoice" data-arg="${esc(cur)}" ${busy || idle ? 'disabled' : ''}>${T('hotpatch.voice.mirror')}</button>`;
  return `<div style="margin-top:18px;padding-top:16px;border-top:1px solid color-mix(in srgb, var(--goldD) 18%, transparent)">
    <h4>${T('hotpatch.voice.title')}</h4><p class="hint">${T('hotpatch.voice.hint')}</p>
    <div>${rows}</div>${freeLine}${notIndexed}${noSpace}${unserved}${unfetched}${onDemand}${elsewhere}
    <div class="flex between center" style="margin-top:12px"><span class="hint" style="margin:0">${todo}</span>${action}</div></div>`;
}
// Where the last Enable run took the fix files from (agent 3.4, GET /server/hotpatch `source`): only
// the words the agent defines are drawn — an unknown one (a newer agent) shows nothing rather than a
// missing-key label.
// The two mirrors the server can fill the hotpatch from, as the admin chooses between them in this
// card (hotpatchMirrorSection). Internet Archive is the default: one pinned download instead of
// thousands of CDN requests. The value is the agent's policy field `hotpatchSource`, not a setting
// of this launcher.
const HOTPATCH_MIRRORS = {
  archive: { label: 'hotpatch.mirror.archive', sub: 'hotpatch.mirror.archiveDesc' },
  cdn: { label: 'hotpatch.mirror.cdn', sub: 'hotpatch.mirror.cdnDesc' },
};

// Where the SERVER fills its mirror from -- the fixes and the voice packs alike. It is the agent's
// policy, saved on the box the moment it is clicked (not with the launcher's own "Save settings"),
// which is why it lives in this card and not in Settings: it belongs to the server the card shows.
// An agent older than 3.4 has no such field, and then neither option is marked and a click reports
// whatever that agent answers.
function hotpatchMirrorSection() {
  if (!isAdminMode() || !S.serverConfigured) return '';
  const pol = S.policy.data;
  const body = !pol
    ? `<p class="hint" style="margin:0 0 10px">${S.policy.loading ? T('common.checking') : T('policy.unavailable')}</p>
      <button class="btn ghost" style="padding:8px 14px;font-size:12px" data-act="policyLoad" ${S.policy.loading ? 'disabled' : ''}>${T('common.retry')}</button>`
    : `<div class="grid2">${Object.keys(HOTPATCH_MIRRORS).map((id) => `<div class="opt ${pol.hotpatchSource === id ? 'sel' : ''}" data-act="setHotpatchSource" data-arg="${id}">
        <div class="h">${T(HOTPATCH_MIRRORS[id].label)}</div><p>${T(HOTPATCH_MIRRORS[id].sub)}</p></div>`).join('')}</div>
      <p class="hint" style="margin:10px 0 0">${T('hotpatch.mirror.hint')}</p>`;
  return `<div style="margin-top:18px;padding-top:16px;border-top:1px solid color-mix(in srgb, var(--goldD) 18%, transparent)">
    <h4>${T('hotpatch.mirror.title')}</h4>${body}</div>`;
}

const HOTPATCH_FILL = { bundle: 'hotpatch.source.bundle', cdn: 'hotpatch.source.cdn', 'bundle+cdn': 'hotpatch.source.bundle+cdn', cached: 'hotpatch.source.cached' };
function hotpatchCard() {
  const h = S.hotpatch;
  const versions = S.versions.map((v) => v.id);
  const cur = hotpatchCur();
  const tabs = versions.map((id) => `<button class="lg-chip ${cur === id ? 'on' : ''}" data-act="hotpatchVersion" data-arg="${esc(id)}">${T('common.versionN', { id })}</button>`).join('');
  const d = hotpatchDataOf(cur);
  const dis = srvBusy() ? 'disabled' : '';
  const fld = 'color:var(--muted);font-weight:800;font-size:10.5px';
  let body;
  if (!d) {
    body = `<p class="hint">${!S.serverConfigured ? T('server.agent.notConfigured') : h.loading ? T('common.checking') : T('hotpatch.noData')}</p>
      <button class="btn ghost" style="padding:8px 14px;font-size:12px" data-act="hotpatchLoad" data-arg="${esc(cur)}" ${h.loading ? 'disabled' : ''}>${T('hotpatch.load')}</button>`;
  } else if (!d.available) {
    body = `<p class="hint" style="margin:0">${T('hotpatch.unavailable')}</p>`;
  } else {
    const files = d.files || {};
    const total = Number(files.total) || 0, cached = Number(files.cached) || 0;
    // `problem` = the agent cannot derive a mirror URL players could reach (loopback listener, no
    // advertised IP, a URL over 128 chars): an enabled mirror in that state is NOT advertised and
    // stays pending until the address is fixed — a distinct label, not "pending restart".
    const blocked = d.enabled && typeof d.problem === 'string' && d.problem.trim() !== '';
    // A running Enable / Disable of this version owns the line: what the agent has stored is the
    // state from BEFORE that run until it has fetched (or reverted) everything — hotpatchApplyJob.
    const applying = hotpatchApplyJob(cur);
    // "Fetch missing files" is that same 'hotpatch' job on a mirror that is ALREADY enabled: it
    // switches nothing on, so it must not read as if the mirror were off for its duration.
    const applyKey = applying === 'enabling' && d.enabled ? 'hotpatch.stateRefetching' : HOTPATCH_APPLY[applying];
    const state = applying ? T(applyKey)
      : d.enabled ? (blocked ? T('hotpatch.stateBlocked') : d.pending ? T('hotpatch.statePending') : T('hotpatch.stateEnabled')) : T('hotpatch.stateDisabled');
    const stateColor = applying ? 'var(--warn)'
      : d.enabled ? (blocked ? 'var(--bad)' : d.pending ? 'var(--warn)' : 'var(--ok)') : 'var(--muted)';
    const rev = (k) => (d[k] && d[k].revision) != null ? String(d[k].revision) : '—';
    const problem = blocked ? `<div class="note" style="margin-top:12px">${T('hotpatch.problem', { problem: d.problem })}</div>` : '';
    // A miss is served from the official CDN on demand unless the admin switched that off
    // (d.upstream === false): only then would a client fail on a file missing from the server.
    const partial = d.enabled && !d.complete ? `<div class="note" style="margin-top:12px">${T(d.upstream === false ? 'hotpatch.incompleteNoUpstream' : 'hotpatch.incomplete', { cached, total })}</div>` : '';
    // Agent 3.4 fills the mirror from the archive.org bundles first and falls back to the official
    // CDN file by file; without a 7z/tar tool on the box the bundles cannot be unpacked — said here,
    // so the admin knows why an Enable is slower and what to install.
    const source = HOTPATCH_FILL[d.source] ? `<div><small style="${fld}">${T('hotpatch.sourceLabel')}</small><div style="font-weight:800">${T(HOTPATCH_FILL[d.source])}</div></div>` : '';
    // ... and only while bundles are actually in play: with the CDN chosen as the mirror source no
    // archive is ever unpacked, so asking for a 7z tool would be noise.
    const noExtractor = d.bundles && typeof d.bundles === 'object' && d.bundles.enabled && !d.bundles.extractor && d.mirrorSource !== 'cdn'
      ? `<div class="note" style="margin-top:12px">${T('hotpatch.noExtractor')}</div>` : '';
    body = `<div class="flex gap wrap" style="margin:4px 0 14px">
      <div><small style="${fld}">${T('hotpatch.stateLabel')}</small><div style="font-weight:800;color:${stateColor}">${state}</div></div>
      <div><small style="${fld}">${T('hotpatch.cachedLabel')}</small><div style="font-weight:800">${T('hotpatch.cached', { cached, total, mb: mb(files.cachedBytes), totalMb: mb(files.bytes) })}</div></div>
      <div><small style="${fld}">${T('hotpatch.revisionsLabel')}</small><div style="font-weight:800" class="mono">${T('hotpatch.revisions', { res: rev('res'), data: rev('data'), silence: rev('silence') })}</div></div>
      ${source}
      ${d.url ? `<div style="min-width:0"><small style="${fld}">${T('hotpatch.urlLabel')}</small><div style="font-weight:800;word-break:break-all" class="mono">${esc(d.url)}</div></div>` : ''}
      ${d.appliedAt ? `<div><small style="${fld}">${T('hotpatch.appliedLabel')}</small><div style="font-weight:800" class="mono">${esc(d.appliedAt)}</div></div>` : ''}
    </div>${problem}${partial}${noExtractor}
    <div class="flex between center" style="margin-top:12px"><button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="hotpatchLoad" data-arg="${esc(cur)}" ${h.loading ? 'disabled' : ''}>${T('hotpatch.refresh')}</button>
      <div class="flex gap">${d.enabled && !d.complete
        ? `<button class="btn" data-act="hotpatchRefetch" data-arg="${esc(cur)}" ${dis}>${T('hotpatch.refetch')}</button>` : ''}
      ${d.enabled
        ? `<button class="btn ghost danger" data-act="hotpatchDisable" data-arg="${esc(cur)}" ${dis}>${T('hotpatch.disable')}</button>`
        : `<button class="btn" data-act="hotpatchEnable" data-arg="${esc(cur)}" ${dis}>${T('hotpatch.enable')}</button>`}</div></div>${hotpatchMirrorSection()}${hotpatchVoiceSection(cur, d)}`;
  }
  return `<div class="sect"><h4>${T('hotpatch.title')}</h4><p class="hint">${T('hotpatch.hint')}</p>
    <div class="flex gap wrap" style="margin-bottom:12px">${tabs}</div>
    ${body}
  </div>`;
}

// ── admin: the agent's own settings (agent 3.6) ──
// GET /agent/config = the KEY=VALUE configuration the agent runs with, as a list of typed settings
// (value = what the process uses, stored = the file's line or null, default), plus the stack folder of
// each version. POST /agent/config {set} writes the file on the box — the agent keeps its comments and
// every other line — and applies the "live" keys at once; the "restart" keys wait for POST
// /agent/restart (agent.restart, which the backend follows until the agent answers again). Values
// travel as strings: a switch "1"/"0", a size in MiB, '' = the line is removed and the default applies
// again (the placeholder shows it). A key an environment variable fixes is drawn disabled and never
// sent. An agent older than 3.6 answers 404, which the backend returns as {tooOld: true}.
// One shape for every reset: boot, another server (srvRetarget), an agent install (deploy.done).
// form = every field as drawn ({KEY: string}; data-model keys hold no dots), open = the settings groups
// unfolded, restartRequired = the restart keys the last Save wrote (only for an agent that does not
// report restartPending), reloc = the folder dialog of one version (agentCfgReloc) or null.
function agentCfgBlank() { return { data: null, loading: false, error: '', form: {}, saving: false, restarting: false, open: false, restartRequired: [], reloc: null }; }
const AGENT_CFG_GROUPS = ['general', 'network', 'storage', 'downloads'];
// Label tables (translation keys, translated when drawn): a folder's state on the settings card, the
// folder dialog's three ways (card title / description / what a running server goes through / the Yes
// label) and the agent's word on the target folder (server.relocate.check `target`).
const AGENT_DIR_STATES = { running: 'agentCfg.dir.state.running', present: 'agentCfg.dir.state.present', missing: 'agentCfg.dir.state.missing', none: 'agentCfg.dir.state.none' };
const RELOC_MODES = {
  move: { label: 'agentCfg.reloc.move', sub: 'agentCfg.reloc.moveSub', running: 'agentCfg.reloc.running.move', yes: 'agentCfg.reloc.yes.move' },
  repoint: { label: 'agentCfg.reloc.repoint', sub: 'agentCfg.reloc.repointSub', running: 'agentCfg.reloc.running.repoint', yes: 'agentCfg.reloc.yes.repoint' },
  remove: { label: 'agentCfg.reloc.remove', sub: 'agentCfg.reloc.removeSub', running: 'agentCfg.reloc.running.remove', yes: 'agentCfg.reloc.yes.remove' },
};
const RELOC_TARGETS = { absent: 'agentCfg.reloc.target.absent', empty: 'agentCfg.reloc.target.empty', stack: 'agentCfg.reloc.target.stack', other: 'agentCfg.reloc.target.other' };
// Only keys setModel can walk to (a dotted key would split the path): a newer agent's odd key is left out.
const agentCfgSettings = () => {
  const d = S.agentCfg.data;
  return d && Array.isArray(d.settings) ? d.settings.filter((s) => s && typeof s.key === 'string' && /^[A-Za-z0-9_]+$/.test(s.key)) : [];
};
const agentCfgGroup = (s) => (typeof s.group === 'string' && s.group ? s.group : 'general');
// The agent's own truthy and falsy words (it normalises what it writes to 1/0, a hand-edited file may
// say "yes" — or "", or "enabled"). It reads a switch two ways, by its default: a default-ON switch is
// on unless the line says 0/false/no/off (_flag_on), a default-OFF one only for 1/true/yes/on (_flag_off).
const agentCfgTrue = (v) => ['1', 'true', 'yes', 'on'].includes(String(v == null ? '' : v).trim().toLowerCase());
const agentCfgFalse = (v) => ['0', 'false', 'no', 'off'].includes(String(v == null ? '' : v).trim().toLowerCase());
// A field as it is drawn before any edit: what the FILE says ('' = no line, the default applies); a
// switch has no "no line" position, so its effective 1/0 — the file's line read the agent's way, else
// the default. A key an environment variable fixes shows what the process uses.
function agentCfgBase(s) {
  if (s.env) return String(s.value == null ? '' : s.value);
  if (s.type === 'bool') {
    const raw = s.stored != null ? s.stored : s.default;
    return (String(s.default) === '1' ? !agentCfgFalse(raw) : agentCfgTrue(raw)) ? '1' : '0';
  }
  return s.stored == null ? '' : String(s.stored);
}
const agentCfgField = (s) => (hasOwn(S.agentCfg.form, s.key) ? String(S.agentCfg.form[s.key]) : agentCfgBase(s));
// What Save sends: the fields that differ from the file (trimmed — the agent strips them anyway).
const agentCfgChanged = () => agentCfgSettings().filter((s) => !s.env && agentCfgField(s).trim() !== agentCfgBase(s).trim());
// A reply replaces the data and re-seeds the fields from it. keepEdits (a background re-read: a folder
// job settled, the Server page opened again): what the admin changed and has not saved stays typed.
function agentCfgApply(d, keepEdits) {
  const c = S.agentCfg, edits = {};
  if (keepEdits) agentCfgChanged().forEach((s) => { edits[s.key] = agentCfgField(s); });
  c.data = d && typeof d === 'object' ? d : null;
  c.form = {};
  agentCfgSettings().forEach((s) => { c.form[s.key] = hasOwn(edits, s.key) && !s.env ? edits[s.key] : agentCfgBase(s); });
}
const agentCfgLabel = (key) => { const k = `agentCfg.key.${key}`; return I18N.has(k) ? t(k) : key; };
const agentCfgChoice = (key, ch) => (I18N.has(`agentCfg.enum.${key}.${ch}`) ? t(`agentCfg.enum.${key}.${ch}`) : ch);
// The restart keys the running agent does not use yet. The agent's restartPending (stored or default ≠
// what the process runs with) is the truth and survives a reload — also the reply of a Save, whose
// restartRequired would still name a key saved back to the value the process already uses; that list
// only stands in for an agent that does not report restartPending.
function agentCfgPending() {
  const c = S.agentCfg, d = c.data || {};
  const list = Array.isArray(d.restartPending) ? d.restartPending : Array.isArray(c.restartRequired) ? c.restartRequired : [];
  return list.filter((k, i) => typeof k === 'string' && k && list.indexOf(k) === i);
}
// One version's folder on the agent: the folder and "configured" from the config read (the agent's
// VERSIONS), present / running from the live status poll when there is one — a download or a Prepare
// changes them long after the config was read.
function agentCfgVersion(id) {
  const d = S.agentCfg.data;
  const cv = d && d.versions && typeof d.versions === 'object' && d.versions[id] && typeof d.versions[id] === 'object' ? d.versions[id] : null;
  const f = srvFacts(id), live = S.srv.state === 'ok' && !!f;
  const dir = cv && typeof cv.dir === 'string' ? cv.dir : (f && f.dir) || '';
  const configured = cv ? !!cv.configured && !!dir : !!(f && f.configured && dir);
  const present = configured && (live ? f.present : !!(cv && cv.present));
  return { dir, configured, present, up: present && (live ? f.up : !!(cv && cv.up)) };
}
function agentCfgRow(s, locked) {
  const key = s.key, v = agentCfgField(s), dis = locked || s.env ? 'disabled' : '';
  const subKey = `agentCfg.key.${key}.sub`;
  const sub = I18N.has(subKey) ? `<small>${T(subKey)}</small>` : '';
  const badge = s.apply === 'restart' ? `<span class="acfg-badge">${T('agentCfg.restartBadge')}</span>` : '';
  const env = s.env ? `<small style="color:var(--warn);font-weight:800">${T('agentCfg.envOverride')}</small>` : '';
  const running = String(s.value == null ? '' : s.value);
  const waiting = s.apply === 'restart' && !s.env && agentCfgPending().includes(key)
    ? `<small style="color:var(--warn);font-weight:800">${T('agentCfg.pendingValue', { value: running || t('agentCfg.valueDefault') })}</small>` : '';
  // The default greyed in an empty field; without one (a derived default: the host name, a folder
  // beside the state file) what the process resolved it to, as long as the file does not set it.
  const ph = String(s.default == null ? '' : s.default) || (s.stored == null ? running : '');
  const model = `data-model="agentCfg.form.${key}"`;
  let ctl;
  if (s.type === 'bool') {
    ctl = `<button class="toggle ${v === '1' ? 'on' : ''}" data-act="agentCfgToggle" data-arg="${esc(key)}" ${dis}><span class="knob"></span></button>`;
  } else if (s.type === 'enum') {
    const choices = (Array.isArray(s.choices) ? s.choices : []).map(String);
    if (v && !choices.includes(v)) choices.push(v); // a hand-edited value outside the list: shown as it is
    const dflt = String(s.default == null ? '' : s.default);
    ctl = `<select class="txt acfg-in" ${model} ${dis}><option value=""${v === '' ? ' selected' : ''}>${T('agentCfg.enumDefault', { choice: dflt ? agentCfgChoice(key, dflt) : '—' })}</option>${
      choices.map((ch) => `<option value="${esc(ch)}"${ch === v ? ' selected' : ''}>${esc(agentCfgChoice(key, ch))}</option>`).join('')}</select>`;
  } else if (s.type === 'mib' || s.type === 'seconds') {
    // A text field, not type=number: '' has to mean "remove the line", and a number input reports a
    // half-typed "12a" as '' too. Save refuses anything but digits; the agent checks the range.
    ctl = `<input class="txt mono acfg-in acfg-num" ${model} value="${esc(v)}" placeholder="${esc(ph)}" inputmode="numeric" autocomplete="off" spellcheck="false" ${dis}><span class="acfg-unit">${s.type === 'mib' ? T('agentCfg.unit.mib') : T('agentCfg.unit.seconds')}</span>`;
  } else {
    ctl = `<input class="txt mono acfg-in" ${model} value="${esc(v)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false" ${dis}>`;
  }
  return `<div class="line"><div style="min-width:0"><b>${esc(agentCfgLabel(key))}</b>${badge}${sub}${env}${waiting}</div><div class="acfg-ctl">${ctl}</div></div>`;
}
function agentCfgCard() {
  const c = S.agentCfg, d = c.data;
  const head = `<h4>${T('agentCfg.title')}</h4><p class="hint">${T('agentCfg.hint')}</p>`;
  if (!S.serverConfigured) return `<div class="sect acfg">${head}<p class="hint" style="margin:0">${T('server.agent.notConfigured')}</p></div>`;
  if (!d) {
    return `<div class="sect acfg">${head}${c.loading ? `<p class="hint" style="margin:0">${T('common.checking')}</p>` : `
      ${c.error ? `<div class="note" style="margin-bottom:10px">${T('agentCfg.loadFailed', { error: c.error })}</div>` : ''}
      <button class="btn ghost" style="padding:8px 14px;font-size:12px" data-act="agentCfgLoad">${T('common.retry')}</button>`}</div>`;
  }
  if (d.tooOld) return `<div class="sect acfg">${head}<div class="note">${T('agentCfg.tooOld')}</div></div>`;
  // A restart in flight holds the edits too: the agent is going away under them.
  const busy = srvBusy() || c.restarting, locked = !d.writable || c.saving || c.restarting;
  const settings = agentCfgSettings(), changed = agentCfgChanged(), pending = agentCfgPending();
  const fld = 'color:var(--muted);font-weight:800;font-size:10.5px';
  // Read-only facts: which agent, where it listens, which file it edits, how it checks HTTPS.
  const started = Number(d.started) > 0 ? new Date(Number(d.started) * 1000).toLocaleString() : '';
  const tls = d.tls && typeof d.tls === 'object' ? d.tls : null;
  // The extra roots: the file only when one is set; one the agent could not load (caFileError: its
  // reason) is said in the warning colour — the downloads then run on the system store alone.
  const caFile = tls && typeof tls.caFile === 'string' ? tls.caFile.trim() : '';
  const caErr = tls && typeof tls.caFileError === 'string' ? tls.caFileError.trim() : '';
  const caText = caErr ? `<span style="color:var(--warn)">${T('agentCfg.tls.caFileBad', { file: caFile || String((agentCfgSettings().find((s) => s.key === 'GIO_CA_FILE') || {}).value || '').trim() || '?', why: caErr })}</span>`
    : caFile ? T('agentCfg.tls.caFile', { file: caFile }) : '';
  const tlsText = tls ? [tls.strict ? T('agentCfg.tls.strict') : T('agentCfg.tls.verified'), tls.pinnedFallback ? T('agentCfg.tls.fallbackOn') : T('agentCfg.tls.fallbackOff'),
    caText].filter(Boolean).join(' · ') : '';
  const hosts = tls && Array.isArray(tls.fallbackHosts) ? tls.fallbackHosts.filter((h) => typeof h === 'string' && h) : [];
  const file = d.file ? (d.writable ? `<span class="mono">${esc(d.file)}</span>` : T('agentCfg.fileReadOnly', { file: d.file })) : T('agentCfg.fileNone');
  const facts = `<div class="flex gap wrap" style="row-gap:10px;margin-bottom:4px">
      <div><small style="${fld}">${T('agentCfg.agent')}</small><div style="font-weight:800">${esc(d.agent || '?')}${d.platform ? ` · ${esc(d.platform)}` : ''}${started ? ` · ${T('agentCfg.since', { time: started })}` : ''}</div></div>
      ${d.listen ? `<div><small style="${fld}">${T('agentCfg.listen')}</small><div style="font-weight:800" class="mono">${esc(d.listen)}</div></div>` : ''}
      <div style="min-width:0"><small style="${fld}">${T('agentCfg.file')}</small><div style="font-weight:800;word-break:break-all">${file}</div></div>
      ${tlsText ? `<div><small style="${fld}">${T('agentCfg.tls')}</small><div style="font-weight:800">${tlsText}</div></div>` : ''}
    </div>
    ${hosts.length ? `<div class="note" style="margin-top:10px">${T('agentCfg.tlsFallbackHosts', { hosts: hosts.join(', ') })}</div>` : ''}
    ${!d.writable && d.file ? `<div class="note" style="margin-top:10px">${T('agentCfg.readOnly')}${d.writableReason ? `<br><small class="mono">${esc(d.writableReason)}</small>` : ''}</div>`
      : !d.file ? `<div class="note" style="margin-top:10px">${T('agentCfg.noFile')}</div>` : ''}`;
  // Saved but not in use yet: the restart, from here when the agent can do it itself (a restart
  // refuses while any job runs — the agent's rule, mirrored by the dead button).
  const restart = !pending.length ? '' : `<div class="note" style="margin-top:12px;display:flex;align-items:center;justify-content:space-between;gap:12px">
      <span>${T('agentCfg.restartNote', { keys: pending.map(agentCfgLabel).join(', ') })}${d.restart ? '' : ` ${T('agentCfg.restartManual')}`}</span>
      ${d.restart ? `<button class="btn" style="padding:8px 14px;font-size:12px;flex:none" data-act="agentRestart" ${busy || c.restarting ? 'disabled' : ''}>${c.restarting ? T('agentCfg.restarting') : T('agentCfg.restart')}</button>` : ''}</div>`;
  // The server folders: the catalogue's versions first, then any other the agent knows.
  const ids = S.versions.map((v) => v.id);
  Object.keys(d.versions && typeof d.versions === 'object' ? d.versions : {}).forEach((id) => { if (!ids.includes(id)) ids.push(id); });
  const dirRows = ids.map((id) => {
    const vi = agentCfgVersion(id);
    const st = !vi.configured ? 'none' : vi.up ? 'running' : vi.present ? 'present' : 'missing';
    const color = st === 'running' ? 'var(--ok)' : st === 'missing' ? 'var(--warn)' : 'var(--muted)';
    return `<div class="line"><div style="min-width:0"><b>${T('common.versionN', { id })}</b>
        <small class="${vi.dir ? 'mono' : ''}" style="word-break:break-all">${vi.dir ? esc(vi.dir) : T('agentCfg.dir.none')}</small>
        <small style="color:${color};font-weight:800">${T(AGENT_DIR_STATES[st])}</small></div>
      <button class="btn ghost" style="padding:8px 12px;font-size:12px;flex:none" data-act="agentCfgReloc" data-arg="${esc(id)}" ${busy || !d.writable ? 'disabled' : ''}>${T('agentCfg.dir.change')}</button></div>`;
  }).join('');
  const dirs = `<h5 class="acfg-group">${T('agentCfg.dirs.title')}</h5><p class="hint" style="margin:0 0 2px">${T('agentCfg.dirs.hint')}</p>${dirRows}`;
  // The settings, folded by default (the Server page is long): one section per group, in the agent's order.
  const groups = AGENT_CFG_GROUPS.concat(settings.map(agentCfgGroup).filter((g, i, a) => !AGENT_CFG_GROUPS.includes(g) && a.indexOf(g) === i));
  const body = !c.open ? '' : groups.map((g) => {
    const rows = settings.filter((s) => agentCfgGroup(s) === g);
    if (!rows.length) return '';
    const gk = `agentCfg.group.${g}`;
    return `<h5 class="acfg-group">${I18N.has(gk) ? T(gk) : esc(g)}</h5>${rows.map((s) => agentCfgRow(s, locked)).join('')}`;
  }).join('');
  const fold = settings.length ? `<div style="margin-top:16px"><button class="btn ghost" style="padding:9px 14px;font-size:12px" data-act="agentCfgOpen">${c.open ? '▾' : '▸'} ${c.open ? T('agentCfg.hideSettings') : T('agentCfg.showSettings', { n: settings.length })}</button></div>` : '';
  // Unsaved edits keep the bar on screen after the groups are folded again.
  const saveBar = !(c.open || changed.length) ? '' : `<div class="flex between center" style="margin-top:14px;gap:12px">
      <span class="hint" style="margin:0" id="acfg-unsaved">${changed.length ? T('agentCfg.unsaved', { n: changed.length }) : ''}</span>
      <button class="btn" data-act="agentCfgSave" ${locked || !changed.length ? 'disabled' : ''}>${c.saving ? T('agentCfg.saving') : T('agentCfg.save')}</button></div>`;
  return `<div class="sect acfg">${head}${facts}${restart}${dirs}${fold}${body}${saveBar}</div>`;
}
// Typing does not re-render (the caret would go): the Save button and its count follow in place.
function syncAgentCfgSave() {
  const c = S.agentCfg, n = agentCfgChanged().length;
  const b = document.querySelector('[data-act="agentCfgSave"]');
  if (b) b.disabled = !n || c.saving || c.restarting || !(c.data && c.data.writable);
  const u = document.getElementById('acfg-unsaved');
  if (u) u.textContent = n ? t('agentCfg.unsaved', { n }) : '';
}

// The folder dialog of one version (agentCfgReloc; POST /server/relocate). Three ways, as option cards:
// move the stack (it must be there — renamed on the same drive, copied and verified on another), point
// the agent at another folder (nothing is moved: a server already there, or an empty folder to download
// into), or take the version off the agent (a re-point to no folder, only for a configured version).
// The agent checks exactly what is on screen (server.relocate.check, a dry run — debounced while
// typing) and Start stays dead until that check said ok; the job re-validates everything under its lock.
// r = {version, from, dir, mode: 'move'|'repoint'|'remove', check: {ui (the mode it ran for), dir, mode, res, error} | null,
// checking, soon (a debounced check is due), seq, at (the arming window of the confirm dialog)}.
function relocModes(r) {
  const vi = agentCfgVersion(r.version);
  const out = [{ id: 'move', dis: !vi.present }, { id: 'repoint', dis: false }];
  if (vi.configured) out.push({ id: 'remove', dis: false });
  return out;
}
const relocModeOk = (r) => relocModes(r).some((o) => o.id === r.mode && !o.dis);
// What the agent is asked: "remove" is a re-point to no folder.
const relocQuery = (r) => (r.mode === 'remove' ? { version: r.version, dir: '', mode: 'repoint' } : { version: r.version, dir: String(r.dir || '').trim(), mode: r.mode });
// A check stands for exactly what is on screen: the same query AND the same option card (ui) — "remove"
// and a re-point with a blank field ask the agent the same thing, and a remove verdict must never
// unlock "Use this folder" (which would take the version off the agent). Outside "remove" a folder
// has to be typed.
const relocCheckFits = (r, q, ch) => !!ch && ch.ui === r.mode && ch.dir === q.dir && ch.mode === q.mode;
function relocStartOk(r) {
  const q = relocQuery(r), ch = r.check;
  return relocModeOk(r) && (r.mode === 'remove' || !!q.dir) && !srvBusy() && !r.checking && !r.soon && relocCheckFits(r, q, ch)
    && !!ch.res && ch.res.ok === true && !ch.res.problem;
}
function relocCheckHtml(r) {
  const q = relocQuery(r), ch = r.check, remove = r.mode === 'remove';
  const line = (html, color) => `<p style="margin:6px 0 0;font-size:12.5px;font-weight:700;line-height:1.5;color:${color || 'var(--inkSoft)'}">${html}</p>`;
  if (!remove && !q.dir) return line(T('agentCfg.reloc.enterDir'), 'var(--muted)');
  if (r.checking || r.soon) return line(T('agentCfg.reloc.checking'), 'var(--muted)');
  if (!relocCheckFits(r, q, ch)) return line(T('agentCfg.reloc.needCheck'), 'var(--muted)');
  if (ch.error) return line(T('agentCfg.reloc.checkFailed', { error: ch.error }), 'var(--bad)');
  const res = ch.res || {}, out = [];
  // A refusal is the whole answer: the agent's own sentence (not enough space, a folder that holds
  // something else...), which would otherwise repeat the target line below it word for word.
  if (res.problem) return line(esc(String(res.problem)), 'var(--bad)');
  // An absent folder: a move creates it; a re-point creates nothing (a later download does).
  if (!remove && hasOwn(RELOC_TARGETS, res.target)) {
    out.push(line(T(res.target === 'absent' && q.mode === 'repoint' ? 'agentCfg.reloc.target.absentRepoint' : RELOC_TARGETS[res.target])));
  }
  if (q.mode === 'move' && res.sameVolume === true) out.push(line(T('agentCfg.reloc.sameDrive')));
  else if (q.mode === 'move' && res.sameVolume === false) {
    out.push(line(T('agentCfg.reloc.otherDrive', { size: res.stackBytes == null ? '?' : sizeGb(res.stackBytes), free: res.freeBytes == null ? '?' : sizeGb(res.freeBytes) })));
  }
  if (res.running) out.push(line(T(RELOC_MODES[r.mode].running), 'var(--warn)'));
  if (res.ok === true) out.push(line(T('agentCfg.reloc.ok'), 'var(--ok)'));
  return out.join('');
}
function relocOverlay() {
  const r = S.agentCfg.reloc, d = S.agentCfg.data || {};
  const remove = r.mode === 'remove';
  // Only "move" can be unavailable (no server files to move): its card says why instead of its description.
  const cards = relocModes(r).map((o) => `<div class="opt${r.mode === o.id ? ' sel' : ''}${o.dis ? ' dis' : ''}" data-act="relocMode" data-arg="${o.id}">
      <div class="h">${T(RELOC_MODES[o.id].label)}</div><p>${o.dis ? T('agentCfg.reloc.moveOff') : T(RELOC_MODES[o.id].sub)}</p></div>`).join('');
  const ph = d.platform === 'windows' ? `C:\\relic_servers\\${r.version}_live` : `/home/${r.version}_live`;
  // Browse opens THIS PC's folder picker: offered only when the agent runs on this PC.
  const dirField = remove ? '' : `<label class="fld">${T('agentCfg.reloc.newDir')}</label>
    <div class="flex gap"><input class="txt mono" data-model="agentCfg.reloc.dir" value="${esc(r.dir)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">
      ${d.local ? `<button class="btn ghost" style="padding:8px 12px;font-size:12px;flex:none" data-act="relocPick">${T('common.browse')}</button>` : ''}
      <button class="btn ghost" style="padding:8px 12px;font-size:12px;flex:none" data-act="relocCheck" ${r.checking ? 'disabled' : ''}>${T('agentCfg.reloc.check')}</button></div>`;
  return `<div class="overlay"><div class="box wide">
    <div style="width:60px;height:60px;border-radius:50%;margin:0 auto 14px;background:color-mix(in srgb, var(--gold) 16%, transparent);color:var(--goldD);display:flex;align-items:center;justify-content:center">${I.folder.replace('width="18" height="18"', 'width="26" height="26"')}</div>
    <div class="serif" style="font-weight:700;font-size:19px;text-align:center">${T('agentCfg.reloc.title', { version: r.version })}</div>
    <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;line-height:1.6;margin:10px 0 0;text-align:center">${T('agentCfg.reloc.current')}: <b class="${r.from ? 'mono pick' : ''}" style="color:var(--ink);word-break:break-all">${r.from ? esc(r.from) : T('agentCfg.dir.none')}</b></p>
    <div class="copts">${cards}</div>
    ${dirField}
    <div id="reloc-check" style="min-height:24px;margin:6px 0 20px">${relocCheckHtml(r)}</div>
    <div class="flex center gap" style="justify-content:center">
      <button class="btn ghost" data-act="relocClose">${T('common.cancel')}</button>
      <button class="btn" data-act="relocStart" ${relocStartOk(r) ? '' : 'disabled'}>${T(RELOC_MODES[r.mode].yes)}</button>
    </div></div></div>`;
}
// The check area and Start follow in place: the check lands while the admin may still be typing.
function syncReloc() {
  const r = S.agentCfg.reloc, box = document.getElementById('reloc-check');
  if (!r || !box) { if (!typingNow()) render(); return; }
  box.innerHTML = relocCheckHtml(r);
  const b = document.querySelector('[data-act="relocStart"]'); if (b) b.disabled = !relocStartOk(r);
  const k = document.querySelector('[data-act="relocCheck"]'); if (k) k.disabled = !!r.checking;
}
let relocTimer = 0;
function relocCheckSoon() {
  const r = S.agentCfg.reloc;
  if (!r) return;
  clearTimeout(relocTimer);
  r.soon = true; syncReloc();
  relocTimer = setTimeout(() => { if (S.agentCfg.reloc === r) relocCheck(); }, 700);
}
// The last check asked wins (seq); one for another dialog, or for a folder typed over since, is dropped.
async function relocCheck() {
  clearTimeout(relocTimer);
  const r = S.agentCfg.reloc;
  if (!r) return;
  r.soon = false;
  const q = relocQuery(r), ui = r.mode, seq = ++r.seq;
  if (!relocModeOk(r) || (r.mode !== 'remove' && !q.dir)) { r.checking = false; syncReloc(); return; }
  r.checking = true; syncReloc();
  let res = null, error = '';
  try { res = await rpc('server.relocate.check', q); } catch (e) { error = e.message || String(e); }
  if (S.agentCfg.reloc !== r || r.seq !== seq) return;
  const good = !!res && typeof res === 'object';
  r.checking = false;
  r.check = { ui, dir: q.dir, mode: q.mode, res: good ? res : null, error: error || (good ? '' : t('common.unknownError')) };
  syncReloc();
}

// ── admin (Windows): the agent running on this PC ──
function localAgentCard() {
  if (!S.isWindows) return '';
  const la = S.localAgent.status;
  if (!la) return `<div class="sect"><h4>${T('localAgent.title')}</h4><p class="hint">${T('common.checking')}</p></div>`;
  const dot = la.healthy ? 'live' : la.running ? 'warn' : 'down';
  // An enter / uninstall in flight (S.localAgent.busy) holds every button of the card: the uninstall
  // may sit on a UAC prompt for a minute, and a Start pressed meanwhile would race the kill.
  const busy = S.localAgent.busy ? 'disabled' : '';
  return `<div class="sect"><h4>${T('localAgent.title')}</h4>
    <div class="line"><div><b><span class="dot ${dot}" style="display:inline-block;margin-right:6px;vertical-align:middle"></span>${la.installed ? (la.healthy ? T('localAgent.healthy', { port: la.port }) : la.running ? T('localAgent.starting') : T('localAgent.stopped')) : T('localAgent.notInstalled')}</b>
      <small>${esc(la.root)} · ${la.python ? T('localAgent.python', { v: la.python.version }) : T('localAgent.noPython')}</small></div>
      <div class="flex gap">${la.installed
        ? `${la.running ? `<button class="btn ghost" data-act="localAgentStop" ${busy}>${T('localAgent.stop')}</button>` : `<button class="btn" data-act="localAgentStart" ${busy}>${T('localAgent.start')}</button>`}<button class="btn ghost" data-act="localAgentUninstall" ${busy}>${T('localAgent.uninstall')}</button>`
        : `<button class="btn" data-act="deployOpenWindows">${T('localAgent.install')}</button>`}</div></div>
    ${la.installed ? `
    <div class="line"><div><b>${T('localAgent.autostart')}</b><small>${T('localAgent.autostartSub')}</small></div><button class="toggle ${la.autostart ? 'on' : ''}" data-act="localAgentAutostart"><span class="knob"></span></button></div>
    <div class="line"><div><b>${T('localAgent.firewall')}</b><small>${T('localAgent.firewallSub', { port: la.port })}</small></div><button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="localAgentFirewall">${T('localAgent.firewallBtn')}</button></div>
    <div class="line"><div><b>${T('localAgent.log')}</b><small>${esc(la.logPath)}</small></div><button class="btn ghost" style="padding:8px 12px;font-size:12px" data-act="localAgentOpenLog">${T('localAgent.openLog')}</button></div>` : ''}
    ${la.fiddlerRunning ? `<div class="note" style="margin-top:10px">${T('deploy.fiddlerWarning')}</div>` : ''}
  </div>`;
}

// "Guide (PDF)" — the vendor's GIO guide book, next to everything that asks the admin to set up a
// server: the install form, the admin start-screen panel, About.
const ICON_BOOK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:6px;vertical-align:-2px"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>';
const guideBtn = () => `<button class="btn ghost" style="padding:7px 12px;font-size:11.5px;white-space:nowrap" data-act="openGuide" title="${T('help.guideTitle')}">${ICON_BOOK}${T('help.guide')}</button>`;

// ── install-agent overlay (reachable from the start screen and from the admin Server page) ──
function agentInstallOverlay() {
  const a = S.agentInstall;
  const f = a.form;
  const win = a.target === 'windows';
  const field = (key, label, ph, type, mono) =>
    `<label class="fld">${T(label)}</label><input class="txt ${mono === false ? '' : 'mono'}" type="${type || 'text'}" data-model="agentInstall.form.${key}" value="${esc(f[key] || '')}" placeholder="${esc(ph || '')}" autocomplete="off" spellcheck="false">`;
  // One server stack: the "I already have it" toggle (on = the folder holds the extracted vendor
  // package, the install checks it; off = the folder is where the agent DOWNLOADS the ready-made
  // package from the Internet Archive once it runs — agent 3.4, deploy.done `fetch`) above its
  // folder field, whose label follows the toggle. The size is this build's catalogue figure: the agent
  // that knows the real one is not installed yet.
  const stackCell = (ver, dirKey, haveKey, dirLabel, ph) => {
    const have = f[haveKey] !== false;
    return `<div>
      <div class="row" style="margin:6px 0 2px"><div><b>${T('deploy.stacks.have', { version: ver })}</b><small>${T('deploy.stacks.haveSub', { size: sizeGb(STACK_SIZES[ver]) })}</small></div><button class="toggle ${have ? 'on' : ''}" data-act="deployToggle" data-arg="${haveKey}"><span class="knob"></span></button></div>
      <label class="fld">${have ? T(dirLabel) : T('deploy.stacks.installInto', { version: ver })}</label><input class="txt mono" data-model="agentInstall.form.${dirKey}" value="${esc(f[dirKey] || '')}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">
      ${win ? `<button class="btn ghost" style="margin-top:6px;padding:7px 12px;font-size:12px" data-act="deployPickDir" data-arg="${dirKey}">${T('common.browse')}</button>` : ''}
    </div>`;
  };
  if (a.running || a.done || a.error) {
    return `<div class="overlay"><div class="box" style="width:640px">
      <h3 class="serif" style="margin:0 0 10px">${T(win ? 'deploy.title.windows' : 'deploy.title.linux')}</h3>
      <div class="console"><div class="bar"><span class="lights"><i style="background:#e4664e"></i><i style="background:#e8b04a"></i><i style="background:#67b86a"></i></span><span>${T('deploy.console.label')} · ${esc(win ? 'localhost' : (f.sshHost || '—'))}</span></div><div class="log" id="deploylog" style="max-height:260px">${(a.lines || []).map((l) => `<div>${esc(l)}</div>`).join('')}</div></div>
      ${a.error ? `<div class="note" style="margin-top:12px">${esc(a.error)}</div>` : ''}
      ${!a.error && a.done && a.result && a.result.warning ? `<div class="note" style="margin-top:12px">${esc(a.result.warning)}</div>` : ''}
      ${(a.done || a.error) && a.result && a.result.token ? `<div class="summary" style="margin-top:12px"><div class="k">${T(a.error ? 'deploy.done.titleFailed' : 'deploy.done.title')}</div><div class="g"><span>${T('deploy.done.token')}</span><b class="pick mono" style="word-break:break-all">${esc(a.result.token)}</b></div><p class="hint" style="margin:8px 0 0">${T(a.error ? 'deploy.done.hintFailed' : 'deploy.done.hint')}</p></div>` : ''}
      ${!a.error && a.done && a.result && Array.isArray(a.result.fetch) && a.result.fetch.length ? `<div class="note" style="margin-top:12px">${T('deploy.done.fetchStarting')}</div>` : ''}
      <div class="flex between center" style="margin-top:16px"><span class="hint" style="margin:0">${a.running ? T('deploy.running') : ''}</span>
        <div class="flex gap">${guideBtn()}${a.error ? `<button class="btn" data-act="deployBack">${T('deploy.backToForm')}</button>` : ''}<button class="btn ghost" data-act="deployClose" ${a.running ? 'disabled' : ''}>${T('common.close')}</button></div></div></div></div>`;
  }
  return `<div class="overlay"><div class="box" style="width:700px;max-height:640px;overflow-y:auto">
    <div class="flex between center" style="gap:12px"><h3 class="serif" style="margin:0 0 4px">${T('deploy.title')}</h3>${guideBtn()}</div><p class="hint">${T('deploy.intro')}</p>
    <div class="grid2" style="margin-bottom:12px">
      <div class="opt ${!win ? 'sel' : ''}" data-act="deployTarget" data-arg="linux"><div class="h">${I.stack} ${T('deploy.target.linux')}</div><p>${T('deploy.target.linuxSub')}</p></div>
      <div class="opt ${win ? 'sel' : ''}" data-act="deployTarget" data-arg="windows"><div class="h">${I.gear} ${T('deploy.target.windows')}</div><p>${T('deploy.target.windowsSub')}</p></div>
    </div>
    <div class="note">${T('deploy.filesWarning')}</div>
    ${win ? `<div class="note" style="margin-top:8px">${T('deploy.fiddlerWarning')}</div>` : ''}
    ${!win ? `<h4 class="serif" style="margin:16px 0 4px">${T('deploy.ssh.title')}</h4>
    <div class="grid2">
      <div>${field('sshHost', 'deploy.ssh.host', 'game.example.com')}</div><div>${field('sshPort', 'deploy.ssh.port', '22')}</div>
      <div>${field('sshUser', 'deploy.ssh.user', 'root')}</div><div>${field('sshPassword', 'deploy.ssh.password', '', 'password')}</div>
      <div>${field('sshKeyPath', 'deploy.ssh.key', 'C:\\path\\key.ppk')}<button class="btn ghost" style="margin-top:6px;padding:7px 12px;font-size:12px" data-act="deployPickKey">${T('common.browse')}</button></div>
      <div>${field('sshKeyPassphrase', 'deploy.ssh.passphrase', '', 'password')}</div>
      <div>${field('sudoPassword', 'deploy.ssh.sudo', '', 'password')}<small class="hint">${T('deploy.ssh.sudoHint')}</small></div>
    </div>` : ''}
    <h4 class="serif" style="margin:16px 0 4px">${T('deploy.stacks.title')}</h4>
    <div class="grid2">
      ${stackCell('1.6', 'dir16', 'have16', 'deploy.stacks.dir16', DEPLOY_DIRS.have16[win ? 1 : 2])}
      ${stackCell('2.8', 'dir28', 'have28', 'deploy.stacks.dir28', DEPLOY_DIRS.have28[win ? 1 : 2])}
    </div>
    <h4 class="serif" style="margin:16px 0 4px">${T('deploy.net.title')}</h4>
    <div class="grid2">
      <div>${field('bindIp', 'deploy.net.bindIp', t('deploy.net.auto'))}<small class="hint">${T('deploy.net.bindHint')}</small></div>
      <div>${field('advertisedIp', 'deploy.net.advertisedIp', '')}<small class="hint">${T('deploy.net.advHint')}</small></div>
      <div>${field('advertisedHost', 'deploy.net.advertisedHost', 'game.example.com')}<small class="hint">${T('deploy.net.advHostHint')}</small></div>
      <div>${field('listen', 'deploy.net.listen', '0.0.0.0:18080')}</div>
      <div>${field('serverName', 'deploy.net.name', t('deploy.net.namePh'), 'text', false)}</div>
      <div>${field('token', 'deploy.net.token', '')}<button class="btn ghost" style="margin-top:6px;padding:7px 12px;font-size:12px" data-act="deployGenToken">${T('deploy.net.generate')}</button></div>
      <div>${field('muipHost', 'deploy.net.muip', t('deploy.net.auto'))}<small class="hint">${T('deploy.net.muipHint')}</small></div>
      <div>${field('muipKey', 'deploy.net.muipKey', t('deploy.net.random'))}<small class="hint">${T('deploy.net.muipKeyHint')}</small></div>
    </div>
    ${win ? `<div class="row" style="margin-top:12px"><div><b>${T('deploy.win.autostart')}</b><small>${T('deploy.win.autostartSub')}</small></div><button class="toggle ${f.autostart ? 'on' : ''}" data-act="deployToggle" data-arg="autostart"><span class="knob"></span></button></div>` : ''}
    ${!win ? `<div class="row" style="margin-top:12px"><div><b>${T('deploy.ssh.upgradeForce')}</b><small>${T('deploy.ssh.upgradeForceSub')}</small></div><button class="toggle ${f.upgradeForce ? 'on' : ''}" data-act="deployToggle" data-arg="upgradeForce"><span class="knob"></span></button></div>` : ''}
    <div class="flex between center" style="margin-top:18px">
      <button class="btn ghost" data-act="deployClose">${T('common.cancel')}</button>
      <button class="btn glow" data-act="deployStart">${T(win ? 'deploy.start.windows' : 'deploy.start.linux')}</button>
    </div></div></div>`;
}

// ── per-version removal overlay ──
function removeOverlay() {
  const r = S.remove;
  const title = T('lib.remove.removing', { id: r.versionId });
  if (r.error || r.done) {
    return `<div class="overlay"><div class="box" style="text-align:center">
      <div class="serif" style="font-weight:700;font-size:19px">${r.error ? T('lib.remove.failed') : T('lib.remove.done', { id: r.versionId })}</div>
      <p style="color:var(--inkSoft);font-size:12.5px;font-weight:600;margin:10px 0 18px;word-break:break-word">${r.error ? esc(r.error) : (r.leftovers && r.leftovers.length ? T('lib.remove.leftovers', { n: r.leftovers.length }) : T('lib.remove.doneNote'))}</p>
      <button class="btn" data-act="removeClose">${T('common.close')}</button></div></div>`;
  }
  return `<div class="overlay"><div class="box">
    <div class="flex center gap" style="margin-bottom:6px">${spinnerHtml()}<div class="serif" style="font-weight:700;font-size:18px">${title}</div></div>
    <div style="color:var(--inkSoft);font-size:12.5px;font-weight:700;margin:2px 0 18px;min-height:36px">${esc(r.msg || '...')}</div>
    <div class="pbar"><i style="width:${Math.round((r.fraction || 0) * 100)}%"></i></div>
  </div></div>`;
}

function adminServer() {
  const cards = S.versions.map(serverCard).join('');
  const banner = `<div class="banner">${I.warn}<div><b>${T('server.banner.title')}</b><p>${T('server.banner.body')}</p></div></div>`;
  const info = S.srv.info || {};
  // The configured server (Settings.ServerHost, the one source of truth) with its agent port — the
  // Advanced form that used to show the port is gone, so it is named here.
  const agentAt = `${S.serverAddr.host.includes(':') ? `[${S.serverAddr.host}]` : S.serverAddr.host}:${S.serverAddr.agentPort}`;
  const agent = S.serverConfigured
    ? `<div class="agentbar"><span class="dot live"></span><span>${T('server.agent.configured')}</span><small>· ${esc(agentAt)} · ${S.admin.mode === 'ssh' ? T('server.agent.ssh') : T('server.agent.direct')}${info.agent ? ` · ${T('server.agent.agentLabel')} ${esc(info.agent)}` : ''}${info.advertisedHost
    // agent 3.2 follows a DDNS name: show which address it currently resolves to (the lookup error as a tooltip)
    ? ` · <span title="${esc(info.advertisedError || '')}">${esc(info.advertisedHost)} → ${esc(info.advertisedIp || t('server.agent.unresolved'))}</span>`
    : (info.advertisedIp ? ` · ${esc(info.advertisedIp)}` : '')}</small>
        <button class="btn dark" style="margin-left:auto;padding:7px 12px;font-size:11.5px" data-act="deployOpen">${T('server.agent.installNew')}</button></div>`
    : `<div class="agentbar"><span class="dot" style="background:var(--warn)"></span><span>${T('server.agent.notConfigured')}</span><small>· ${T('server.agent.seeSettings')}</small>
        <button class="btn" style="margin-left:auto;padding:7px 12px;font-size:11.5px" data-act="deployOpen">${T('server.agent.installNew')}</button></div>`;
  // An operation started elsewhere (another computer, another admin): the agent refuses the second
  // one with 409 anyway, so it is more honest to show why the buttons are dead.
  const busyNote = info.busy && !jobHere()
    ? (AGENT_SELF_JOBS[info.busy.kind]
      ? `<div class="note" style="margin-top:16px">${T('server.busy.self', { job: jobTitle(info.busy.kind).toLowerCase(), version: info.busy.version })}</div>`
      : `<div class="note" style="margin-top:16px">${T('server.busy.other', { job: jobTitle(info.busy.kind).toLowerCase(), version: info.busy.version })}</div>`)
    : '';
  // The job this launcher follows runs on ANOTHER server (the address was changed while it ran): it
  // keeps every button of this one dead, and the console below is that server's log — say so.
  const elsewhereNote = jobElsewhere()
    ? `<div class="note" style="margin-top:16px">${T('server.busy.elsewhere', { job: jobTitleOf(S.srvJob).toLowerCase(), version: S.srvJob.version, host: S.srvJob.host || '—' })}</div>`
    : '';
  const errNote = S.srv.state === 'error'
    ? `<div class="note" style="margin-top:16px">${T('server.err.status', { error: S.srv.error })}</div>`
    : (info.error ? `<div class="note" style="margin-top:16px">${T('server.err.docker', { error: info.error })}</div>` : '');
  // Agent 3.2: the agent's own state file. Unreadable = the agent refuses every change (a stop still
  // works) until the file is repaired by hand or reset from here; restored from its backup / reset =
  // automatic provisioning stays off for any version without a provisioning record. An older agent
  // sends no `state` and the page renders exactly as before.
  const ast = info.state && typeof info.state === 'object' ? info.state : null;
  const stateNote = !ast || S.srv.state !== 'ok' ? ''
    : ast.ok === false
      ? `<div class="banner">${I.warn}<div><b>${T('server.agentState.unreadableTitle')}</b>
          <p>${T('server.agentState.unreadableBody', { error: String(ast.error || ''), path: String(ast.path || 'state.json') })}</p>
          <button class="btn ghost danger" style="margin-top:10px;padding:8px 14px;font-size:12px" data-act="stateResetAsk" ${srvBusy() ? 'disabled' : ''}>${T('server.agentState.resetButton')}</button></div></div>`
      : ast.restoredFromBackup
        ? `<div class="note" style="margin-top:16px">${T('server.agentState.restored', { at: String(ast.restoredFromBackup.at || '?'), copy: String(ast.restoredFromBackup.corruptCopy || '—') })}</div>`
        : ast.reset
          ? `<div class="note" style="margin-top:16px">${T('server.agentState.wasReset', { at: String(ast.reset.at || '?'), copy: String(ast.reset.corruptCopy || '—') })}</div>`
          : '';
  return `<div class="pad"><h1 class="title">${T('server.title')}</h1><p class="sub">${T('server.sub')}</p>
    <div class="grid2">${cards}</div>${stateNote}${errNote}${busyNote}${elsewhereNote}${serverConsole()}${banner}${agent}
    <div style="margin-top:16px">${acctNewCard()}${policyCard()}${secretsCard()}${pathfindingCard()}${hotpatchCard()}${agentCfgCard()}${localAgentCard()}</div>
    ${serverAdvanced()}</div>`;
}

function server() { return isAdminMode() ? adminServer() : playerServer(); }

// ── settings ──
// The theme card is first in Settings; click = apply instantly + save to localStorage.
function themeSect() {
  const chk = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4 8-9"/></svg>';
  const card = (id, name, desc, prev) => `<button class="themecard" data-act="setTheme" data-arg="${id}">
      ${S.theme === id ? `<span class="ring"></span><span class="tick">${chk}</span>` : ''}
      <div class="prev ${id}">${prev}</div>
      <div class="body"><b>${name}</b><small>${desc}</small></div>
    </button>`;
  // The previews show their own theme regardless of the active one, so the colours are fixed here.
  const classicPrev = '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#c8a04f" stroke-width="1.4"><path d="M12 2 22 12 12 22 2 12z"/><path d="M12 6.5 17.5 12 12 17.5 6.5 12z" fill="#c8a04f" fill-opacity=".25"/></svg>';
  return `<div class="sect">
    <div class="flex between center" style="margin-bottom:4px"><h4 style="margin:0">${T('settings.theme.title')}</h4><span style="color:var(--muted);font-size:11px;font-weight:800">${T('settings.theme.autosave')}</span></div>
    <p class="hint" style="margin:6px 0 14px">${T('settings.theme.hint')}</p>
    <div class="grid2" style="gap:12px">
      ${card('classic', T('settings.theme.classic'), T('settings.theme.classicDesc'), classicPrev)}
      ${card('summer', T('settings.theme.summer'), T('settings.theme.summerDesc'), '<span class="minibuoy"></span>')}
    </div>
  </div>`;
}

// Language row: only drawn when more than one language ships (lang/index.json). The <select> is
// handled by the 'change' listener (data-lang), not by data-model: the switch saves the setting and
// reloads the dictionary before re-rendering.
function languageLine() {
  const langs = I18N.available();
  if (langs.length < 2) return '';
  const cur = (S.settings.language || 'en').toLowerCase();
  const opts = langs.map((l) => `<option value="${esc(l.code)}"${l.code === cur ? ' selected' : ''}>${esc(l.name)}</option>`).join('');
  return `<div class="line"><div><b>${T('settings.app.language')}</b><small>${T('settings.app.languageDesc')}</small></div>
    <select class="txt" style="width:180px" data-lang>${opts}</select></div>`;
}

function settings() {
  const s = S.settings;
  return `<div class="pad" style="max-width:820px">
    <h1 class="title">${T('settings.title')}</h1><p class="sub">${T('settings.sub')}</p>

    ${themeSect()}

    <div class="sect"><h4>${T('settings.server.title')}</h4>
      <label class="fld" style="margin-top:0">${T('settings.server.addressPort')}</label>
      <input class="txt mono" data-model="addrEdit" value="${esc(addrShown())}" placeholder="${T('login.serverPlaceholder')}" spellcheck="false" autocomplete="off"/>
      <p class="hint" style="margin:10px 0 0">${T('settings.server.addressHint')}</p>
    </div>

    <div class="sect"><h4>${T('settings.source.title')}</h4>
      <div class="grid2">${[{ id: 'cdn', label: t('wizard.source.cdn') }, ...(S.versions.some((v) => v.hasDrive) ? [{ id: 'drive', label: 'Google Drive' }] : []), ...allMirrors()]
        .map((o) => `<div class="opt ${s.source === o.id ? 'sel' : ''}" data-act="setDefaultSource" data-arg="${esc(o.id)}"><div class="h">${esc(o.label)}</div></div>`).join('')}</div>
      <p class="hint" style="margin:10px 0 0">${T('settings.source.hint')}</p>
    </div>

    <div class="sect"><h4>${T('settings.install.title')}</h4>
      <div class="line"><div><b>${T('settings.install.gentle')}</b><small>${T('settings.install.gentleDesc')}</small></div>${toggle('sgentle', s.gentleExtract !== false)}</div>
      <label class="fld">${T('settings.install.workers')}</label>
      <div class="flex gap center"><input class="txt mono" style="width:90px" data-model="settings.extractWorkers" value="${s.extractWorkers || 0}"/>
        <small style="color:var(--inkSoft);font-size:11.5px;font-weight:600;line-height:1.45">${T('settings.install.workersDesc')}</small></div>
    </div>

    ${S.versions.some((v) => v.hasEnhancements) ? `<div class="sect"><h4>${T('settings.game.title')}</h4>
      <div class="line"><div><b>${T('settings.game.enhancements')}</b><small>${T('settings.game.enhancementsDesc', { versions: S.versions.filter((v) => v.hasEnhancements).map((v) => v.id).join(', ') })}</small></div>${toggle('senh', s.enhancements !== false)}</div>
    </div>` : ''}

    <div class="sect"><div class="flex between center" style="margin-bottom:8px"><h4 style="margin:0">Fiddler Classic</h4>
      <span style="color:${S.fiddler.installed ? '#4d8a51' : '#b57b2a'};font-size:11.5px;font-weight:800">${S.fiddler.installed ? T('settings.fiddler.installed') : T('settings.fiddler.notInstalled')}</span></div>
      <div class="line"><b>${T('common.decryptHttps')}</b>${toggle('sdecrypt', s.fiddlerDecrypt)}</div>
      <div class="line"><div><b>${T('settings.fiddler.noUac')}</b><small>${S.fiddler.isAdmin
        ? T('settings.fiddler.noUacAdmin')
        : T('settings.fiddler.noUacStandard')} ${T('settings.fiddler.noUacFallback')}</small></div>${toggle('snouac', s.fiddlerNoUac !== false)}</div>
      <div class="line"><div><b>${T('settings.fiddler.cert')}</b><small>${S.fiddler.certTrusted ? T('settings.fiddler.installed') : T('settings.fiddler.certNeeds')}</small></div>
        ${S.fiddler.certTrusted ? `<span style="color:#4d8a51;font-weight:800;font-size:12px">${I.check} ${T('common.yes')}</span>` : `<button class="btn" style="padding:8px 14px;font-size:12px" data-act="trustCert">${T('settings.fiddler.approveCert')}</button>`}</div>
    </div>

    <div class="sect"><h4>${T('settings.app.title')}</h4>
      ${languageLine()}
      <div class="line"><div><b>${T('settings.app.startup')}</b><small>${T('settings.app.startupDesc')}</small></div>${toggle('startup', S.runAtStartup)}</div>
      <div class="line"><div><b>${T('settings.app.background')}</b><small>${T('settings.app.backgroundDesc')}</small></div><span style="color:#4d8a51;font-weight:800;font-size:12px">${I.check} ${T('common.active')}</span></div>
      ${profileLine()}
    </div>

    <div class="flex gap" style="margin-bottom:18px"><button class="btn glow" data-act="saveSettings">${T('settings.save')}</button></div>

    ${aboutSect()}
  </div>`;
}

// "About Relic" — credits for the third-party pieces the launcher ships or relies on (README has the
// full list). Links open in the system browser through the backend (shell.openUrl).
const CREDITS = [
  { name: 'mhynot2 — khang06', note: 'about.credit.mhynot2', url: 'https://github.com/khang06/mhynot2' },
  { name: 'Akebi GC — NctimeAza/AnimeGame-Cheat-3.3 v1.2.3', note: 'about.credit.ee', url: 'https://github.com/NctimeAza/AnimeGame-Cheat-3.3' },
  { name: '2.8 global-metadata.dat patch', note: 'about.credit.metadata', url: '' },
  { name: 'GAA server data fixes — @AZ#7011', note: 'about.credit.gaaFixes', url: '' },
  { name: 'Dimbreath / AnimeGameData', note: 'about.credit.gamedata', url: 'https://gitlab.com/Dimbreath/AnimeGameData' },
  { name: 'Enka.Network', note: 'about.credit.enka', url: 'https://enka.network' },
  { name: 'Fiddler Classic — Telerik/Progress', note: 'about.credit.fiddler', url: 'https://www.telerik.com/fiddler/fiddler-classic' },
  { name: 'Cinzel · Mulish · JetBrains Mono', note: 'about.credit.fonts', url: 'https://fonts.google.com' },
];
function aboutSect() {
  const rows = CREDITS.map((c) => `<div class="line"><div><b>${esc(c.name)}</b><small>${T(c.note)}</small></div>
    ${c.url ? `<button class="btn ghost" style="padding:7px 12px;font-size:11.5px" data-act="openUrl" data-arg="${esc(c.url)}">${T('about.open')}</button>` : ''}</div>`).join('');
  return `<div class="sect" style="margin-top:18px"><h4>${T('about.title')}</h4>
    <p class="hint">${T('about.intro')}</p>
    <div class="line"><div><b>${T('help.guideTitle')}</b><small>${T('help.guideSub')}</small></div>
      <button class="btn ghost" style="padding:7px 12px;font-size:11.5px" data-act="openGuide">${T('about.open')}</button></div>
    ${rows}
    <p class="hint" style="margin:12px 0 0">${T('about.legal')}</p>
  </div>`;
}

// Which account data the OFFICIAL Genshin client would start on. A session that ended without a
// clean restore leaves a version's profile loaded, and the live client would then read (and
// overwrite) private-server data — so this has to be visible and fixable, not just logged.
function profileLine() {
  const p = S.profile;
  if (!p) return `<div class="line"><div><b>${T('settings.profile.title')}</b><small>${T('common.checking')}</small></div></div>`;
  if (p.liveActive) {
    return `<div class="line"><div><b>${T('settings.profile.title')}</b><small>${T('settings.profile.liveLoaded')}</small></div>
      <span style="color:#4d8a51;font-weight:800;font-size:12px">${I.check} ${T('settings.profile.liveBadge')}</span></div>`;
  }
  const why = p.torn
    ? T('settings.profile.torn')
    : T('settings.profile.versionLoaded', { version: p.active });
  const blocked = p.gameRunning ? ' ' + T('settings.profile.closeGame') : '';
  return `<div class="line"><div><b style="color:#b57b2a">${T('settings.profile.notActive')}</b><small>${why}${blocked}
    ${T('settings.profile.warn')}</small></div>
    <button class="btn" style="padding:8px 14px;font-size:12px" data-act="restoreLive" ${p.gameRunning ? 'disabled' : ''}>${T('settings.profile.restore')}</button></div>`;
}

function toggle(name, on) {
  return `<button class="toggle ${on ? 'on' : ''}" data-act="toggle" data-arg="${name}"><span class="knob"></span></button>`;
}
function toast() {
  // Errors keep the close button: they stay on screen much longer than the rest (see toastMs), so
  // they must be dismissable before their time.
  const close = S.toast.kind === 'bad'
    ? `<button class="toast-x" data-act="toast.close" title="${T('common.close')}">×</button>` : '';
  return `<div class="toast ${S.toast.kind || ''}">${esc(S.toast.msg)}${close}</div>`;
}

// ─────────────────────────── actions ───────────────────────────
function setModel(path, value) {
  const parts = path.split('.');
  let o = S;
  for (let i = 0; i < parts.length - 1; i++) o = o[parts[i]];
  const key = parts[parts.length - 1];
  if (typeof o[key] === 'number') { const n = parseInt(value, 10); o[key] = isNaN(n) ? o[key] : n; }
  else o[key] = value;
}
// Every toast goes away on its own — errors included, which used to stay until closed by hand. Errors
// get a time PROPORTIONAL to the text though: the ones that arrive here are often two-sentence
// instructions about what to do, and 3.2 s would cut them mid-sentence. ~70 ms/character, between 7
// and 15 seconds; the × button stays for whoever wants it sooner, and the messages that really MUST
// be read are windows (S.notice) anyway, not toasts.
function toastMs(msg, kind) {
  if (kind !== 'bad') return 3200;
  return Math.min(15000, Math.max(7000, String(msg || '').length * 70));
}
function showToast(msg, kind) {
  S.toast = { msg, kind };
  render();
  clearTimeout(showToast._t);
  showToast._t = setTimeout(hideToast, toastMs(msg, kind));
}
// Hiding the toast does NOT go through render(): rewriting the whole page moves the focus and the
// caret out of the field the user may be typing in right then — and the error toast now leaves on its
// own, exactly in the seconds the user is correcting what was wrong. The toast is a single node,
// drawn last in #app, so it is removed directly; render() stays only as a safety net.
function hideToast() {
  S.toast = null;
  clearTimeout(showToast._t);
  const el = document.querySelector('#app > .toast');
  if (el) el.remove(); else render();
}
function addLog(t, m) { S.log.push({ t, m }); if (S.log.length > 80) S.log.shift(); }

// Dependency-free copy: WebView2 serves the UI from https://relic.app/ (secure context), so
// navigator.clipboard is available — but if policy refuses it, textarea + execCommand remains the
// variant that works anywhere. The failure says right away what had to be typed, so the user is not
// left stuck.
async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch (e) { /* fall through to the variant below */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) { return false; }
}

const ACTIONS = {
  'toast.close': () => hideToast(),
  'win.min': () => rpc('window.minimize'),
  'win.close': () => rpc('window.close'),
  go: (arg) => {
    S.picker = null; // navigating away must not leave a modal picker hanging over another screen
    S.screen = arg; render();
    if (arg === 'server' && S.serverConfigured) { checkServers(); if (isAdminMode()) { loadPolicy(); loadLocalAgent(); loadHotpatch(S.hotpatch.version || S.selectedVersionId || (S.versions[0] && S.versions[0].id)); loadAcctTemplatesAll(); loadAgentCfg(); } }
    if (arg === 'commands') loadGameData();
    if (arg === 'settings') loadProfileStatus();
  },

  selectVersion: (id) => {
    if (S.installing) return showToast(t('toast.installInProgress'), 'bad');
    if (isInstalled(id)) {
      S.selectedVersionId = id; rpc('version.select', { id }); render();
      if (S.screen === 'commands') loadGameData();
    }
    else { S.wiz.versionId = id; S.installStep = 0; S.screen = 'install'; render(); }
  },
  // PLAY first goes through a FRESH server check: the polling cache can be half a minute old —
  // exactly the window in which an admin stops the stack. With the version's server stopped (or
  // unknown), the game would start and sit on network errors without any explanation, so the user
  // chooses explicitly. The actual launch is playGo.
  play: async (id) => {
    if (S.installing) return showToast(t('toast.installInProgress'), 'bad');
    if (S.playing) return showToast(t('toast.alreadyStarting'), 'bad');
    if (!S.serverConfigured) return ACTIONS.playGo(id);

    // The splash also acts as the modal guard while the check lasts (max ~5s). An ownership token,
    // not versionId: two presses on PLAY for the same version would otherwise be indistinguishable,
    // and the old invocation would adopt the new one's splash and launch twice.
    // check:true keeps the play.done/play.error events of a PREVIOUS session away from this splash —
    // they only close the launch splash, not the pre-check.
    const tok = {};
    S.playing = { versionId: id, msg: t('play.checkingServer'), check: true, tok };
    render();
    const st = await freshServerCheck(5000);
    if (!S.playing || S.playing.tok !== tok) {
      // The splash is no longer ours: "Hide" pressed during the check = giving up (nothing has been
      // launched yet); replaced by a newer invocation = stay quiet, that one speaks.
      if (!S.playing) showToast(t('toast.playCancelled'), 'ok');
      return;
    }
    S.playing = null;

    // Both reply shapes through the cache's own normaliser, and every fact from THIS reply: it used to
    // read st.versions, which the player-mode {public, status} reply does not have (a LIVE server got
    // "The server is stopped"), and `live` from S.srv, which a poll may have superseded meanwhile.
    const vs = statusVersions(st);
    const entry = vs && vs[id];
    const agentErr = !st || statusUnknownOf(st);
    const down = entry && Array.isArray(entry.servicesDown) ? entry.servicesDown : [];
    // The public snapshot flags `degraded` without naming the services (down stays empty).
    const degraded = down.length > 0 || !!(entry && entry.degraded);
    if (entry && entry.up && !agentErr && !degraded) return ACTIONS.playGo(id);

    // Stack "up" but with dead game services = precisely the white-screen scenario: the client
    // connects to dispatch, but the dead gameserver leaves it on an empty screen. The agent restarts
    // them on its own (~2 minutes) — better a question now than a game started for nothing.
    if (entry && entry.up && !agentErr && degraded) {
      S.confirm = {
        title: t('confirm.selfRepair.title'),
        body: down.length ? t('confirm.selfRepair.body', { services: down.join(', ') }) : t('confirm.selfRepair.bodyNoList'),
        yes: t('confirm.selfRepair.yes'),
        act: 'playGo', arg: id,
        at: Date.now(),
      };
      return render();
    }

    // The server does not have this version at all (present:false). Read before the docker-fog test:
    // present is a filesystem fact on the box. "Stopped ... until its server is started" would promise a
    // start that cannot happen; the wording stays neutral (the admin may be extracting the stack now).
    if (entry && entry.present === false) {
      const hosted = Object.keys(vs).filter((k) => vs[k] && vs[k].present !== false).join(', ');
      S.confirm = {
        title: t('confirm.serverDown.titleAbsent', { id }),
        body: hosted ? t('confirm.serverDown.bodyAbsent', { id, hosted }) : t('confirm.serverDown.bodyAbsentNone', { id }),
        yes: t('confirm.serverDown.yes'),
        act: 'playGo', arg: id,
        at: Date.now(),
      };
      return render();
    }

    // Three situations, three texts: the server runs ANOTHER version / it is really stopped / unknown
    // (agent down or docker in a fog — a generalised up:false is not an assertion).
    const live = Object.keys(vs || {}).find((k) => vs[k] && vs[k].up) || null;
    const body = agentErr
      ? t('confirm.serverDown.bodyUnknown')
      : live && live !== id
        ? t('confirm.serverDown.bodyOther', { live, id })
        : t('confirm.serverDown.bodyDown', { id });
    S.confirm = {
      title: agentErr ? t('confirm.serverDown.titleUnknown') : t('confirm.serverDown.titleDown'),
      body,
      yes: t('confirm.serverDown.yes'),
      act: 'playGo', arg: id,
      at: Date.now(),
    };
    render();
  },
  playGo: (id) => {
    if (S.installing) return showToast(t('toast.installInProgress'), 'bad');
    if (S.playing) return showToast(t('toast.alreadyStarting'), 'bad');
    S.log = [{ t: 'sys', m: t('play.launching', { id }) }];
    // "Starting the game" splash: stays on screen until the client actually appears (play.appeared)
    // or the session fails — the play.log messages flow into it meanwhile.
    S.playing = { versionId: id, msg: '' };
    rpc('play.start', { versionId: id }).catch((e) => { S.playing = null; showToast(e.message, 'bad'); render(); });
    render();
  },
  noticeOk: () => { S.notice = null; render(); },
  noticeCopy: async () => {
    const pw = S.notice && S.notice.pick;
    if (!pw) return;
    const ok = await copyText(pw);
    showToast(ok ? t('toast.passwordCopied') : t('toast.copyFailed', { name: pw }), ok ? 'ok' : 'bad');
  },
  hidePlaySplash: () => { S.playing = null; render(); },
  copyAccount: async (name) => {
    const ok = await copyText(name);
    showToast(ok ? t('toast.accountCopied', { name }) : t('toast.copyFailed', { name }), ok ? 'ok' : 'bad');
  },

  pickVersion: (id) => { S.wiz.versionId = id; render(); },
  pickFolder: async () => {
    try { const r = await rpc('pickFolder', { current: S.settings.installRoot }); if (r && r.path) { S.settings.installRoot = r.path; render(); } }
    catch (e) { showToast(e.message, 'bad'); }
  },
  pickLocalFolder: async () => {
    try {
      const r = await rpc('pickFolder', { current: S.wiz.localPath || S.settings.installRoot, title: t('wizard.pickLocalTitle') });
      if (r && r.path) { S.wiz.localPath = r.path; render(); }
    } catch (e) { showToast(e.message, 'bad'); }
  },
  setTheme: (t) => { applyTheme(t); render(); },
  // wizSourceTouched: a choice made now in the wizard beats the saved preference at the next app.init
  // (re-done at every install.done) — otherwise the choice from the "Source" step would be rewritten
  // under it.
  setSource: (src) => { S.wiz.source = src; S.wizSourceTouched = true; render(); },
  setDefaultSource: (src) => { S.settings.source = src; render(); },
  wizNext: () => { if (S.installStep === 0 && !S.wiz.versionId) return showToast(t('toast.pickVersionFirst'), 'bad'); S.installStep = Math.min(LAST, S.installStep + 1); render(); },
  wizPrev: () => { S.installStep = Math.max(0, S.installStep - 1); render(); },
  wizGoto: (i) => { S.installStep = Math.max(0, Math.min(LAST, parseInt(i, 10) || 0)); render(); },
  // Voices step: a pack ticked / unticked — never the last ticked one (the game needs a voice language).
  wizVoiceToggle: (lang) => {
    const id = S.wiz.versionId || S.selectedVersionId;
    const ticked = selectedVoicePacks(id);
    const on = ticked.some((p) => p.lang === lang);
    if (on && ticked.length <= 1) return showToast(t('toast.pickVoiceFirst'), 'bad');
    S.wiz.voices[lang] = !on; render();
  },
  startInstall: async () => {
    if (S.installing) return; // a fast double-click must not fire install.start twice
    if (!S.wiz.versionId) return showToast(t('toast.pickVersion'), 'bad');
    const isLocal = S.wiz.source === 'local';
    if (isLocal && !S.wiz.localPath.trim()) return showToast(t('toast.pickFolderFirst'), 'bad');
    // "local" is not a DOWNLOAD source — it does not overwrite the cdn/drive preference in settings.
    // (Assigned only after the address below is accepted: a refused address aborts with nothing changed.)
    // Mark installing BEFORE the first await — the await is the double-click window.
    S.activeInstallId = S.wiz.versionId;
    S.activeInstallLocal = isLocal ? S.wiz.localPath.trim() : null;
    S.activeInstallVoices = null;
    // The voice languages ticked at the Voices step (a folder import detects its own on disk). The
    // request is frozen here: Resume replays this very snapshot, whatever the ticks say by then.
    S.activeInstallReq = { type: 'install.start', payload: { versionId: S.activeInstallId, localPath: S.activeInstallLocal || '', voices: isLocal ? [] : selectedVoices(S.activeInstallId) } };
    S.installing = true; S.installDone = false; S.installError = null; S.installPaused = false; S.installStopIntent = null;
    // installPhase MUST reset too: a stale 'Done'/'Extract' from the previous run would gate the
    // Pause/Cancel buttons off until this run's first progress event — which never comes if the
    // connection stalls, leaving a modal overlay with no way out.
    S.installPhase = '';
    S.installFraction = 0; S.installMsg = t('install.preparing');
    resetProgress(); // otherwise the bar would start from where the previous install left it
    render();
    // The address typed in step 3 goes through server.setAddress (parse + TXT ports). A refused one stops
    // here: install.start would template the Fiddler rules for the previous server.
    let moved = false;
    try { moved = await saveAddressEdit(); }
    catch (e) {
      clearActiveInstall();
      showToast(e.message, 'bad');
      return;
    }
    if (!isLocal) S.settings.source = S.wiz.source;
    S.settings.fiddlerDecrypt = S.wiz.decrypt; S.settings.installCert = S.wiz.cert; S.settings.createShortcut = S.wiz.shortcut;
    const saved = await rpc('settings.save', S.settings).catch(() => null);
    // Another server: its status epoch starts now, not at install.done. The backend already holds the
    // new address even when settings.save failed, so re-sync from app.init rather than keep A's label.
    if (moved) applyInit(saved || await rpc('app.init').catch(() => null));
    const req = S.activeInstallReq;
    if (!req || !S.installing) return; // cancelled while the address was being saved — nothing to start
    rpc(req.type, req.payload)
      .catch((e) => { clearActiveInstall(); showToast(e.message, 'bad'); });
  },
  finishInstall: () => {
    const wasError = !!S.installError;
    // No local selection here: install.done → applyInit(d.state) already applied the backend's
    // authoritative selection (selecting wiz.versionId could pick a never-installed version).
    clearActiveInstall();
    S.installDone = false; S.installError = null; S.installPaused = false; S.installStopIntent = null; S.installPhase = '';
    if (!wasError) S.screen = 'library';
    render();
  },

  pauseInstall: () => stopInstall('pause'),
  cancelInstall: () => stopInstall('cancel'),
  resumeInstall: () => {
    if (!S.activeInstallId) return ACTIONS.abandonInstall();
    S.installPaused = false; S.installStopIntent = null;
    S.installPhase = ''; // pre-download again — both stop buttons apply until real progress arrives
    S.installMsg = t('install.resuming'); render();
    // The request that started the job is sent again — install.start and install.addVoices both
    // resume for free: partial zips continue via Range, finished ones short-circuit. The snapshot,
    // never a rebuilt payload: the wizard's ticks may have changed since the job paused.
    const req = S.activeInstallReq || { type: 'install.start', payload: { versionId: S.activeInstallId, localPath: S.activeInstallLocal || '' } };
    rpc(req.type, req.payload)
      .catch((e) => { S.installPaused = true; showToast(e.message, 'bad'); render(); });
  },
  abandonInstall: () => {
    // From the paused overlay: the backend already stopped, this only puts the UI away. The partial
    // download stays in the cache, so re-running the wizard resumes instead of starting over.
    clearActiveInstall();
    S.installPaused = false; S.installStopIntent = null; S.installPhase = '';
    showToast(t('toast.installAbandoned'), 'ok');
    render();
  },
  // ── voice languages (Library) ──
  // data-arg is "version|language"; version ids do not contain "|" (the language may hold parentheses).
  addVoice: (arg) => {
    const s = String(arg || ''), i = s.indexOf('|');
    const id = i > 0 ? s.slice(0, i) : '', lang = i > 0 ? s.slice(i + 1) : '';
    if (!id || !lang) return;
    if (S.installing || S.playing || S.remove) return showToast(t('toast.installInProgress'), 'bad');
    const pack = voicesFor(id).find((p) => p.lang === lang);
    const bytes = pack ? Math.max(0, Number(pack.size)) || 0 : 0;
    // The pack is unpacked next to the game files: the archive plus its contents live on the disk at
    // once, so about twice its size must be free during the add (the backend's preflight refuses less).
    S.confirm = {
      title: t('confirm.voices.title', { lang, id }),
      body: t('confirm.voices.body', { gb: gb(bytes), need: gb(bytes * 2) }),
      yes: t('confirm.voices.yes'),
      act: 'addVoiceGo', arg: { id, voices: [lang] }, at: Date.now(),
    };
    render();
  },
  // One language per job (the rpc takes a list; a second Add follows the first). The same overlay,
  // flags and events as a full install — only the words differ (activeInstallVoices).
  addVoiceGo: (a) => {
    if (!a || !a.id || S.installing) return;
    S.activeInstallId = a.id; S.activeInstallLocal = null; S.activeInstallVoices = a.voices.slice();
    S.installing = true; S.installDone = false; S.installError = null; S.installPaused = false; S.installStopIntent = null;
    S.installPhase = ''; S.installFraction = 0; S.installMsg = t('install.preparing');
    resetProgress();
    S.activeInstallReq = { type: 'install.addVoices', payload: { versionId: a.id, voices: a.voices.slice() } };
    render();
    rpc(S.activeInstallReq.type, S.activeInstallReq.payload)
      .catch((e) => { clearActiveInstall(); showToast(e.message, 'bad'); render(); });
  },

  sendCmd: (cat) => guardedSend(() => sendCommand(cat, S.cmd[cat] || '')),
  sendAvatar: () => guardedSend(async () => {
    const a = curAvatar();
    if (!a) return showToast(t('toast.pickAvatarFirst'), 'bad');
    await sendCommand('character', String(a.id));
  }),
  sendWeapon: () => guardedSend(async () => {
    const w = curWeapon();
    if (!w) return showToast(t('toast.pickWeaponFirst'), 'bad');
    const lvl = clampInt(S.sel.weaponLevel, 1, 90, 90);
    const asc = clampInt(S.sel.weaponPromote, 0, 6, 6);
    // Sent as a raw command, not as category "weapon": that branch parses the whole value as one
    // weapon id, so the level and the ascension would never reach the server. The string below is
    // byte-identical to what GmCommands.AddWeapon(id, level, promoteLevel) builds.
    await sendCommand('raw', `equip add ${w.id} ${lvl} ${asc}`);
  }),
  sendSet: () => guardedSend(async () => {
    const st = curSet();
    if (!st) return showToast(t('toast.pickSetFirst'), 'bad');
    const inv = S.sel.artifactMode !== 'equip';
    // Default "equipped directly" mode: equip add <piece> <level>, which puts the pieces on right
    // away, at the chosen level (default +20), on the character controlled in game — the only way to
    // +20 pieces, because "item add" has no level parameter. The DISPLAYED level (+0..+20) leaves
    // from here; the game counts levels from 1 (+20 = 21 on the wire), and the +1 translation is done
    // by the Backend through GmCommands.GiveArtifactPiece — sent raw, "20" gave +19 pieces. "In
    // inventory" mode: item add <piece> 1 — the pieces arrive UNequipped, at +0 (identical to
    // GiveArtifactSetToInventory).
    const lvl = clampInt(S.sel.artifactLevel, 0, setMaxLevel(st), setMaxLevel(st));
    addLog('sys', inv
      ? t('cmd.log.setSendingInv', { set: st.name, n: st.ids.length })
      : t('cmd.log.setSendingEquip', { set: st.name, n: st.ids.length, level: lvl }));
    render();
    // Sequential, not parallel: five concurrent sign+send round-trips through one agent connection
    // would interleave the console log and stress the HTTP/SSH path for no gain.
    for (let i = 0; i < st.ids.length; i++) {
      try {
        if (inv) await sendCommand('raw', `item add ${st.ids[i]} 1`);
        else await sendCommand('artifact', `${st.ids[i]} ${lvl}`);
      }
      catch (e) {
        addLog('bad', t('cmd.log.pieceFailed', { slot: slotName(i), id: st.ids[i], error: e.message }));
        render(); return;
      }
    }
    addLog('ok', inv
      ? t('cmd.log.setDoneInv', { set: st.name, n: st.ids.length })
      : t('cmd.log.setDone', { set: st.name, n: st.ids.length }));
    render();
  }),
  sendBreak: () => guardedSend(async () => {
    const n = clampInt(S.ingame.promote, 0, 6, 6);
    const lvl = PROMOTE_LEVEL_CAP[n];
    // Identical to GmCommands.BreakCurrentAvatar + SetCurrentAvatarLevel: first "break" (raises the
    // cap), then "level" (raises the level up to it) — both on the character controlled in game.
    // The message only says it was SENT: the server answers OK even when the command had no effect
    // (player disconnected), so there is no way to know here that it was applied.
    await sendCommand('raw', `break ${n}`);
    await sendCommand('raw', `level ${lvl}`);
    addLog('sys', t('cmd.log.breakSent', { n, level: lvl }));
    render();
  }),
  sendC6: () => guardedSend(async () => {
    // Identical to GmCommands.UnlockAllConstellations: "talent unlock all" — the server calls the
    // constellation "talent" (procTalent / forceUnlockAllTalent). Like break/level, it works on the
    // character controlled in game, not on the one chosen in the catalogue above.
    await sendCommand('raw', 'talent unlock all');
    addLog('sys', t('cmd.log.c6Sent'));
    render();
  }),
  sendHeroWit: () => guardedSend(async () => {
    // Identical to GmCommands.GiveHeroWit(500): 10M EXP — enough for level 1 → 90.
    await sendCommand('raw', 'item add 104003 500');
    addLog('sys', t('cmd.log.heroWitSent'));
    render();
  }),
  // Toggles on the player's game session: not guaranteed to persist after a relog, hence the Off
  // button too. The strings are identical to GmCommands.StaminaInfinite / WudiAvatar.
  sendStamina: (arg) => guardedSend(async () => {
    const on = arg !== 'off';
    await sendCommand('raw', `stamina infinite ${on ? 'on' : 'off'}`);
    addLog('sys', on ? t('cmd.log.staminaOn') : t('cmd.log.staminaOff'));
    render();
  }),
  sendWudi: (arg) => guardedSend(async () => {
    const on = arg !== 'off';
    await sendCommand('raw', `wudi global avatar ${on ? 'on' : 'off'}`);
    addLog('sys', on ? t('cmd.log.wudiOn') : t('cmd.log.wudiOff'));
    render();
  }),
  sendCostume: (arg) => guardedSend(async () => {
    const c = COSTUMES.find((x) => x.id === num(arg));
    if (!c) return;
    // Identical to GmCommands.GiveCostume: a single item add, the server turns it into a costume.
    await sendCommand('raw', `item add ${c.id} 1`);
    addLog('sys', t('cmd.log.costumeSent', { name: c.name, char: c.char }));
    render();
  }),
  sendRaw: () => guardedSend(async () => {
    const v = S.rawCmd.trim();
    if (!v) return showToast(t('toast.writeCommand'), 'bad');
    await sendCommand('raw', v);
  }),

  openPicker: (kind) => { S.picker = { kind, q: '', el: '', type: '', r: '' }; render(); },
  closePicker: () => { S.picker = null; render(); },
  noop: () => {}, // the picker box swallows its own clicks so they never reach the closing backdrop
  setPickerEl: (el) => { S.picker.el = el; render(); },
  setPickerType: (t) => { S.picker.type = t; render(); },
  setPickerRar: (r) => { S.picker.r = r ? parseInt(r, 10) : ''; render(); },
  pickAvatar: (id) => { S.sel.avatarId = num(id); S.picker = null; render(); },
  pickWeapon: (id) => { S.sel.weaponId = num(id); S.picker = null; render(); },
  pickSet: (name) => { S.sel.setName = name; S.picker = null; render(); },
  toggleRaw: () => { S.rawOpen = !S.rawOpen; render(); },
  toggleHelp: () => { S.helpOpen = !S.helpOpen; render(); },
  setArtifactMode: (m) => { S.sel.artifactMode = m === 'equip' ? 'equip' : 'inv'; render(); },

  // Generic confirm dialog: Yes runs the remembered action, No only closes. Both ignore clicks inside
  // the arming window (see CONFIRM_ARM_MS) — the dialog simply stays up.
  confirmYes: () => {
    const c = S.confirm;
    if (!c || Date.now() - (c.at || 0) < CONFIRM_ARM_MS) return;
    S.confirm = null;
    // The dialog's own tick goes along as the second argument (undefined when the dialog had no box —
    // the receivers test `=== true`), the selected option as the third. Both live on S.confirm, never
    // in a global: Esc used to close a dialog without clearing one, and the next dialog's box (the
    // removal's "also delete the files") then opened already ticked.
    if (ACTIONS[c.act]) ACTIONS[c.act](c.arg, c.checkbox ? !!c.checkbox.checked : undefined, c.selected); else render();
  },
  confirmNo: () => {
    if (S.confirm && Date.now() - (S.confirm.at || 0) < CONFIRM_ARM_MS) return;
    S.confirm = null; render();
  },
  // An option card of a chooser dialog: the body and the Yes label follow it (confirmOverlay). Under
  // the same arming window as the buttons — a double-click on the opener must not pick an option.
  confirmPick: (id) => {
    const c = S.confirm;
    if (!c || !Array.isArray(c.options) || Date.now() - (c.at || 0) < CONFIRM_ARM_MS) return;
    if (c.options.some((o) => o.id === id)) { c.selected = id; render(); }
  },

  checkServers: () => checkServers(),
  startServer: (id) => runServerJob('start', id),
  stopServer: (id) => runServerJob('stop', id),
  // Both go through the Prepare chooser (agent 3.4): the default save (a wipe — the old "reset"
  // warning is that option's body), the admin's own progress, or the configuration fixes only. The
  // card's "Prepare the server" and "Reset the players' progress..." and the Advanced "Re-apply the
  // GAA progress" all ask the same question; only the job kind differs.
  setupServer: (id) => prepareDialog(id, 'setup'),
  provisionServer: (id) => prepareDialog(id, 'provision'),
  // arg = "kind:version" (version ids hold no ":"); ticked = the pathfinding row (undefined when the
  // dialog had none — an agent older than 3.4 — and then nothing is sent: the backend turns an absent
  // field into null, which the agent reads as "not given"; a plain false would switch it OFF).
  prepareGo: (arg, ticked, selected) => {
    const p = String(arg).split(':');
    const kind = p[0] === 'provision' ? 'provision' : 'setup', id = p[1];
    if (!id) return;
    const extra = { progress: PREPARE_MODES[selected] ? selected : 'default' };
    if (kind === 'setup' && typeof ticked === 'boolean') extra.pathfinding = ticked;
    runServerJob(kind, id, extra);
  },
  // Agent 3.4: the ready-made stack from the Internet Archive (the MISSING card's button) and its Stop.
  fetchServer: (id) => runServerJob('fetch', id),
  fetchCancel: async (id) => {
    const j = S.srvJob;
    if (!j || !j.running || j.kind !== 'fetch' || j.cancelSent || !jobHere() || String(j.version) !== String(id)) return;
    j.cancelSent = true; render();
    try {
      const r = await rpc('server.fetch.cancel', { version: id });
      // stopping:false = no download of this version runs on the agent any more (ours ended, or went
      // on without us after the link was lost): nothing to wait for — re-read the status.
      if (!(r && r.stopping)) { if (S.srvJob === j) j.cancelSent = false; checkServers(true); }
    } catch (e) { if (S.srvJob === j) j.cancelSent = false; showToast(e.message, 'bad'); render(); }
  },
  // Agent 3.4: the pathfinding server on / off (the card's toggle) — a job that edits both compose
  // files and, on a running stack, stops / starts the container (enable ends with the 75 s check).
  pathfindingToggle: (id) => {
    const f = srvFacts(id);
    if (!f || f.pathfinding === null || srvBusy()) return;
    const on = !f.pathfinding;
    S.confirm = {
      title: t(on ? 'confirm.pathfinding.enableTitle' : 'confirm.pathfinding.disableTitle', { id }),
      body: t(on ? 'confirm.pathfinding.enableBody' : 'confirm.pathfinding.disableBody'),
      yes: t(on ? 'confirm.pathfinding.enableYes' : 'confirm.pathfinding.disableYes'),
      act: 'pathfindingGo', arg: `${id}:${on ? 1 : 0}`, at: Date.now(),
    };
    render();
  },
  pathfindingGo: (arg) => { const p = String(arg).split(':'); runServerJob('pathfinding', p[0], { enabled: p[1] === '1' }); },
  // Agent 3.2: set the unreadable agent state file aside and start from an empty state. The policy
  // and every per-version record are forgotten (the stacks and the players' progress are not), so
  // it goes through the same "are you sure?" as the progress reset.
  stateResetAsk: () => {
    S.confirm = {
      title: t('confirm.stateReset.title'),
      body: t('confirm.stateReset.body'),
      yes: t('confirm.stateReset.yes'),
      act: 'stateResetGo', arg: '',
      at: Date.now(),
    };
    render();
  },
  stateResetGo: async () => {
    try {
      await rpc('agent.state.reset', {});
      showToast(t('toast.stateReset'), 'ok');
    } catch (e) { showToast(e.message, 'bad'); }
    checkServers(true);
  },
  eventsServer: (id) => runServerJob('events', id),
  netfixServer: (id) => runServerJob('netfix', id),
  acctCopyOpen: (id) => { S.acctCopy = { version: id, from: '', to: '' }; render(); },
  acctCopyClose: () => { S.acctCopy = null; render(); },
  acctCopyGo: () => {
    const c = S.acctCopy;
    if (!c) return;
    const from = c.from.trim(), to = c.to.trim();
    if (!from || !to) return showToast(t('toast.acctFillBoth'), 'bad');
    if (from.toLowerCase() === to.toLowerCase()) return showToast(t('toast.acctSame'), 'bad');
    S.confirm = {
      title: t('confirm.acctCopy.title', { from, to }),
      body: t('confirm.acctCopy.body', { from, to }),
      yes: t('confirm.acctCopy.yes'),
      act: 'acctCopyRun', arg: '',
      at: Date.now(),
    };
    render();
  },
  acctCopyRun: () => {
    const c = S.acctCopy;
    if (!c) return;
    S.acctCopy = null;
    runServerJob('accountcopy', c.version, { from: c.from.trim(), to: c.to.trim() });
  },
  // data-arg is "version:action"; version ids do not contain ":".
  txtFixes: (arg) => { const p = String(arg).split(':'); runServerJob('txtfixes', p[0], { action: p[1] || 'apply' }); },
  closeSrvLog: () => { S.srvJob = null; render(); },
  toggleSrvAdv: () => { S.srvAdvOpen = !S.srvAdvOpen; render(); },

  toggle: async (name) => {
    if (name === 'startup') {
      const want = !S.runAtStartup;
      try { const r = await rpc('startup.set', { enabled: want }); S.runAtStartup = !!(r && r.runAtStartup); showToast(S.runAtStartup ? t('toast.startupOn') : t('toast.startupOff'), 'ok'); }
      catch (e) { showToast(e.message, 'bad'); }
      render(); return;
    }
    const map = { decrypt: () => S.wiz.decrypt = !S.wiz.decrypt, cert: () => S.wiz.cert = !S.wiz.cert, shortcut: () => S.wiz.shortcut = !S.wiz.shortcut,
      sdecrypt: () => S.settings.fiddlerDecrypt = !S.settings.fiddlerDecrypt,
      snouac: () => S.settings.fiddlerNoUac = S.settings.fiddlerNoUac === false,
      sgentle: () => S.settings.gentleExtract = S.settings.gentleExtract === false,
      senh: () => S.settings.enhancements = S.settings.enhancements === false };
    (map[name] || (() => {}))(); render();
  },

  // The address first: a refused one (the same backend.badAddress text as the start screen) saves nothing.
  // A settings.save failing AFTER the address was accepted re-syncs from app.init: the backend already
  // points at the new server, and the UI must not keep the old label (the failed toggles revert too).
  saveSettings: async () => {
    let moved = false;
    try {
      moved = await saveAddressEdit();
      const r = await rpc('settings.save', S.settings); applyInit(r); showToast(t('toast.settingsSaved'), 'ok');
    } catch (e) {
      if (moved) applyInit(await rpc('app.init').catch(() => null));
      showToast(e.message, 'bad');
    }
  },
  // Language switch: persist the setting, then reload the dictionary and redraw everything in it.
  setLanguage: async (code) => {
    S.settings.language = code;
    await rpc('settings.save', S.settings).catch((e) => showToast(e.message, 'bad'));
    await I18N.init(code);
    render();
  },
  restoreLive: async () => {
    try {
      const r = await rpc('profile.restoreLive');
      showToast((r && r.message) || t('toast.liveRestored'), 'ok');
    } catch (e) { showToast(e.message, 'bad'); }
    loadProfileStatus();
  },

  trustCert: async () => {
    try { const r = await rpc('fiddler.trustCert'); S.fiddler.certTrusted = true; showToast(r && r.already ? t('toast.certAlready') : t('toast.certApproved'), 'ok'); render(); }
    catch (e) { showToast(e.message, 'bad'); }
  },

  // ── start screen / modes ──
  pickMode: (mode) => {
    S.login.panel = mode;
    if (!S.login.address) S.login.address = S.serverAddr.host || ((S.defaults.servers[0] || {}).host) || '';
    render();
  },
  loginTab: (mode) => { S.login.panel = mode; render(); },
  // Leaving the panel / the start screen forgets the entry animations, so they play again next time.
  loginClose: () => { S.login.panel = null; onceForget('login:'); render(); },
  loginChip: (host) => { S.login.address = host; S.login.test = null; render(); },
  loginMute: () => {
    S.login.muted = !S.login.muted;
    try { localStorage.setItem('relic-login-muted', S.login.muted ? '1' : '0'); } catch (e) { /* no localStorage */ }
    rpc('login.mute', { muted: S.login.muted }).catch(() => {});
    render();
  },
  loginAnim: () => {
    S.login.animOff = !S.login.animOff;
    try { localStorage.setItem('relic-login-anim', S.login.animOff ? '0' : '1'); } catch (e) { /* no localStorage */ }
    rpc('login.anim', { off: S.login.animOff }).catch(() => {});
    render();
  },
  loginTest: async () => {
    const address = S.login.address.trim(); if (!address) return;
    // Test dials what Enter will dial: the typed agent port goes along (0 = let the backend resolve it).
    const agentPort = S.login.panel === 'admin' ? loginAgentPort() : null;
    if (agentPort === false) return showToast(t('login.badAgentPort'), 'bad');
    S.login.testing = true; S.login.test = null; render();
    // A late reply for an address no longer in the field (a chip clicked, the text edited meanwhile)
    // describes another server: dropped, its error toast included.
    try { const r = await rpc('server.test', { address, agentPort: agentPort || 0 }); if (S.login.address.trim() === address) S.login.test = r; }
    catch (e) { if (S.login.address.trim() === address) showToast(e.message, 'bad'); }
    S.login.testing = false; render();
  },
  loginEnter: async () => {
    if (!loginCanEnter()) return;
    const address = S.login.address.trim();
    const mode = S.login.panel || 'player';
    const agentPort = mode === 'admin' ? loginAgentPort() : null;
    if (agentPort === false) return showToast(t('login.badAgentPort'), 'bad');
    let warning = null;
    try {
      let d;
      if (mode === 'admin') { const r = await rpc('admin.login', { address, token: S.login.token.trim(), agentPort: agentPort || 0 }); d = r && r.state; warning = r && r.warning; }
      else { await rpc('server.setAddress', { address }); d = await rpc('mode.set', { mode: 'player' }); }
      enterShell(d);
      if (warning) showToast(warning, 'bad');
    } catch (e) { showToast(e.message, 'bad'); }
  },
  // Back to the start screen (the mode and the server can be changed there). The saved mode is
  // cleared so the next start asks again unless the user enters once more.
  changeMode: async () => {
    try { const d = await rpc('mode.set', { mode: '' }); applyInit(d); } catch (e) { /* ignore */ }
    S.login.panel = S.mode || null; S.login.address = S.serverAddr.host || S.login.address;
    S.screen = 'login'; onceForget('login:'); render();
  },
  openUrl: (url) => rpc('shell.openUrl', { url }).catch((e) => showToast(e.message, 'bad')),
  // The vendor's GIO guide book (PDF), shipped with the build — opened in the system viewer.
  openGuide: () => rpc('help.guide').catch((e) => showToast(e.message, 'bad')),

  // ── per-version removal ──
  removeVersion: (id) => {
    const inst = S.installed.find((i) => i.id === id);
    const imported = !!inst && inst.origin === 'import';
    if (S.installing || S.playing) return showToast(t('toast.installInProgress'), 'bad');
    S.confirm = {
      title: imported ? t('lib.remove.confirmImportTitle', { id }) : t('lib.remove.confirmTitle', { id }),
      body: imported ? t('lib.remove.confirmImportBody', { dir: (inst && inst.gameDir) || '' }) : t('lib.remove.confirmBody', { dir: (inst && inst.gameDir) || '' }),
      yes: imported ? t('lib.remove.confirmImportYes') : t('lib.remove.confirmYes'),
      act: 'removeGo', arg: id, at: Date.now(),
      // Imported folders: the files are NOT deleted unless the user ticks the box (unchecked by default).
      checkbox: imported ? { label: t('lib.remove.alsoDelete'), checked: false } : null,
    };
    render();
  },
  removeGo: async (id, ticked) => {
    const inst = S.installed.find((i) => i.id === id);
    const imported = !!inst && inst.origin === 'import';
    const deleteFiles = imported ? ticked === true : true;
    S.remove = { versionId: id, running: true, msg: t('lib.remove.starting'), fraction: 0, error: null, done: false, leftovers: [] };
    render();
    try { await rpc('version.remove', { versionId: id, deleteFiles }); }
    catch (e) { S.remove.running = false; S.remove.error = e.message; render(); }
  },
  removeClose: () => { S.remove = null; render(); },
  confirmCheckbox: () => { const c = S.confirm; if (!c || !c.checkbox) return; c.checkbox.checked = !c.checkbox.checked; render(); },

  // ── player account ──
  // arg = "version:template" of the card that was DRAWN (template ids are [a-z0-9-], version ids hold
  // no ':'), and the click pins that version: until then the card follows the status poll, and a poll
  // that saw the version down for a moment moved it elsewhere — Create then sent another version's
  // choice.
  accountTemplate: (arg) => {
    const [v, id] = String(arg || '').split(':');
    if (!v || !id) return;
    S.account.version = v; S.account.tplBy[v] = id;
    render();
  },
  // The card's version chooser: the typed name stays, each version keeps its own progress choice
  // (tplBy), and the previous version's log / error go.
  accountVersion: (id) => {
    const ac = S.account;
    if (ac.running || !id) return;
    ac.version = id; ac.error = null; ac.lines = [];
    render();
  },
  // "Create a new account" under the list of this launcher's accounts (agent 3.7: several per player, up
  // to the server's maxPerPlayer): the new one joins the list and becomes the login shown in the Library;
  // Cancel folds the form away. account.done closes it for its version. Pinned like any touch of the
  // card: the form must not wander to another version under the player.
  accountAnother: (id) => { if (!id) return; S.account.version = id; S.account.another[id] = true; S.account.error = null; render(); },
  accountAnotherCancel: (id) => {
    const ac = S.account;
    if (ac.running) return;
    delete ac.another[id]; ac.error = null; ac.lines = [];
    render();
  },
  // arg = "version:name" (names hold no ':'): show another of this launcher's accounts as the login the
  // Library and PLAY use for that version. The reply is the init state, whose accounts carry the change.
  accountUse: async (arg) => {
    const s = String(arg || ''), i = s.indexOf(':');
    const version = i > 0 ? s.slice(0, i) : '', name = i > 0 ? s.slice(i + 1) : '';
    if (!version || !name) return;
    try { applyInit(await rpc('account.use', { version, name })); showToast(t('toast.accountUsed', { name, version }), 'ok'); }
    catch (e) { showToast(e.message, 'bad'); }
  },
  accountCreate: async (version) => {
    const ac = S.account;
    if (ac.running || S.acctPending) return;
    // First, before anything is drawn: the form on screen must still be what the server offers. A new
    // policy (another template, signup closed, passwords now verified) can land while the player types
    // — the poll does not redraw over a focused input — and Create then sent the stale choice.
    const offer = acctOffer(version);
    const template = ac.tplBy[version];
    if (acctSig(version) !== ac.shown || !offer || !offer.some((tp) => tp.id === template)) {
      return showToast(t('account.optionsChanged'), 'bad'); // showToast redraws: the card shows the new options
    }
    if (!acctUp(version)) return showToast(t('account.notRunning', { version }), 'bad');
    const limit = acctLimit(version);
    if (limit !== null && acctLive(version).length >= limit) return showToast(t('account.limitReached', { version, limit }), 'bad');
    const name = ac.name.trim();
    if (!ACCOUNT_NAME_RE.test(name)) return showToast(t('toast.accountBadName'), 'bad');
    const pub = S.pub.status;
    const pv = !!(pub && pub.versions && pub.versions[version] && pub.versions[version].passwordVerify);
    if (pv && (ac.password.length < 8 || ac.password.length > 64)) return showToast(t('toast.accountBadPassword'), 'bad');
    // Pinned: the card must not wander to another version (a status poll moving acctVersionCur) while
    // this one's create runs and its result comes back.
    ac.version = version;
    ac.running = true; ac.error = null; ac.lines = []; ac.result = null;
    // name: account.done clears the form only while it still holds this account's name.
    const pend = S.acctPending = { origin: 'player', target: srvTarget(), version, name };
    render();
    // remember is left out: the player's own account is remembered on this PC (the backend's default).
    try { await rpc('account.create', { version, name, password: pv ? ac.password : '', template, origin: 'player' }); }
    catch (e) { if (S.acctPending === pend) S.acctPending = null; ac.running = false; ac.error = e.message; render(); }
  },

  // ── admin: create a player account ──
  // Each version keeps its own progress choice (tplBy).
  acctNewVersion: (id) => {
    const a = S.acctNew;
    if (!id) return;
    a.version = id; a.error = null;
    render();
    loadAcctTemplates(id);
  },
  // "version:template" of the card DRAWN, pinning that version — as on the player card (accountTemplate):
  // acctNewCur follows the running version until then, and a poll that saw it down moved the form.
  acctNewTemplate: (arg) => {
    const [v, id] = String(arg || '').split(':');
    if (!v || !id) return;
    S.acctNew.version = v; S.acctNew.tplBy[v] = id;
    render();
  },
  acctNewRemember: () => {
    const a = S.acctNew;
    if (!a.version && a.shownVersion) a.version = a.shownVersion; // a touch of the form pins it
    a.remember = !a.remember;
    render();
  },
  // "Import them now" (beside the card's not-imported note): the template databases of this stack, as
  // a server job of its own — POST /server/templates/ensure stops nothing. Its end re-reads this card's
  // templates (srvJobSettled).
  acctNewImport: (v) => {
    if (!v) return;
    const f = srvFacts(v);
    if (!f || !f.present || !f.up) return showToast(t('server.acctNew.notLive', { version: v }), 'bad');
    if (srvBusy()) return showToast(t('toast.jobInProgress'), 'bad');
    runServerJob('templates.ensure', v);
  },
  acctNewCreate: async (version) => {
    const a = S.acctNew;
    const v = version || acctNewCur();
    const route = acctNewRoute(v);
    if (route !== 'admin' && route !== 'public') return;
    const f = srvFacts(v);
    if (!f || !f.present || !f.up) return showToast(t('server.acctNew.notLive', { version: v }), 'bad');
    const choices = acctNewChoices(v, route);
    const template = a.tplBy[v];
    if (!choices.list.some((tp) => tp.id === template)) return showToast(t('account.optionsChanged'), 'bad');
    const name = a.name.trim();
    if (!ACCOUNT_NAME_RE.test(name)) return showToast(t('toast.accountBadName'), 'bad');
    // Only what the form showed goes out: without verification there is no field, so nothing typed
    // earlier (under verification) rides along.
    const verify = passwordVerified(v);
    const password = verify ? a.password : '';
    // Typed = it must be valid; empty = the agent generates one (admin route) — the player route has
    // no generator, so under verification it needs one typed. So does the admin's OWN login ("Show it
    // as my login"): Relic calls that password "the password you chose", and a generated one the
    // admin never typed would be a lie shown under every later PLAY.
    const badPassword = password ? !ACCOUNT_PASSWORD_RE.test(password) : verify && (route === 'public' || !!a.remember);
    if (badPassword) return showToast(t('toast.accountBadPassword'), 'bad');
    a.version = v;
    if (route === 'admin') {
      if (srvBusy()) return showToast(t('toast.jobInProgress'), 'bad');
      a.pending = { remember: !!a.remember, name };
      // runServerJob keeps kind / version / target on S.srvJob, never the password.
      runServerJob('accountcreate', v, { name, password, template, remember: !!a.remember });
      return;
    }
    // An agent older than 3.5: the player route, under the policy and its limits (the card says so).
    if (a.running || S.acctPending) return;
    a.running = true; a.error = null; a.lines = []; a.logVersion = v;
    // The typed password is kept with the request only when the server verifies it — the result row
    // then shows what the friend has to type; the agent's public result never carries one.
    const pend = S.acctPending = { origin: 'admin', target: srvTarget(), version: v, template, name, password: verify ? password : null };
    render();
    try { await rpc('account.create', { version: v, name, password, template, remember: !!a.remember, origin: 'admin' }); }
    catch (e) { if (S.acctPending === pend) S.acctPending = null; a.running = false; a.error = e.message; render(); }
  },
  acctNewCopyPw: async (key) => {
    const c = S.acctNew.created.find((x) => String(x.key) === String(key));
    if (!c || !c.password) return;
    const ok = await copyText(c.password);
    showToast(ok ? t('toast.passwordCopied') : t('toast.copyFailed', { name: c.password }), ok ? 'ok' : 'bad');
  },

  // ── admin: policy ──
  policyLoad: () => loadPolicy(),
  // One-field policy merge, sent the moment it is clicked -- Settings' own Save button writes the
  // LAUNCHER's settings and never talks to the agent. The old value goes back on screen if the agent
  // refuses, so the card never shows a choice the server did not take.
  setHotpatchSource: async (arg) => {
    const pol = S.policy.data;
    if (!pol || !HOTPATCH_MIRRORS[arg] || pol.hotpatchSource === arg) return;
    const before = pol.hotpatchSource;
    pol.hotpatchSource = arg; render();
    try {
      S.policy.data = await rpc('account.policy.set', { policy: { hotpatchSource: arg } });
      showToast(t('toast.hotpatchSourceSaved', { source: t(HOTPATCH_MIRRORS[arg].label) }), 'ok');
    } catch (e) {
      if (S.policy.data) S.policy.data.hotpatchSource = before;
      showToast(e.message, 'bad');
    }
    render();
  },
  policyToggleSignup: () => { const p = S.policy.data; p.signup = p.signup || { enabled: false, versions: {} }; p.signup.enabled = !p.signup.enabled; render(); },
  policyTogglePlayerCommands: () => { S.policy.data.playerCommands = !S.policy.data.playerCommands; render(); },
  policyToggleTemplate: (arg) => {
    const [v, tp] = String(arg).split(':');
    const p = S.policy.data; p.signup = p.signup || { enabled: false, versions: {} }; p.signup.versions = p.signup.versions || {};
    const pv = p.signup.versions[v] = p.signup.versions[v] || { templates: ['fresh'], maxPerDay: 50 };
    pv.templates = pv.templates || [];
    if (tp === 'fresh') return; // always allowed
    pv.templates = pv.templates.includes(tp) ? pv.templates.filter((x) => x !== tp) : pv.templates.concat([tp]);
    render();
  },
  policySave: async () => {
    const target = srvTarget();
    try {
      const p = S.policy.data;
      Object.values((p.signup && p.signup.versions) || {}).forEach((pv) => { pv.maxPerDay = clampInt(pv.maxPerDay, 0, 100000, 50); });
      S.policy.data = await rpc('account.policy.set', { policy: p });
      // The admin /status carries the policy too, and the "Create a player account" card's player-route
      // fallback (agent < 3.5) reads it from there: the agent's reply IS the new policy, so that card
      // follows now instead of at the next status read. A copy — the toggles above edit S.policy.data.
      if (srvTarget() === target && S.srv.info && !S.srv.info.public && S.policy.data) S.srv.info.policy = JSON.parse(JSON.stringify(S.policy.data));
      showToast(t('toast.policySaved'), 'ok'); render();
    } catch (e) { showToast(e.message, 'bad'); }
  },

  // ── admin: secrets ──
  secretsVersion: (id) => { S.secrets.version = id; S.secrets.data = null; S.secrets.form = {}; render(); },
  secretsLoad: async (id) => {
    S.secrets.version = id; S.secrets.loading = true; render();
    try { S.secrets.data = await rpc('server.secrets.get', { version: id }); S.secrets.form = {}; }
    catch (e) { showToast(e.message, 'bad'); }
    S.secrets.loading = false; render();
  },
  secretsShow: (id) => {
    const d = S.secrets.data; if (!d) return;
    // The card's own labels, not English literals: this dialog is the one place the values are shown
    // in the clear, and a non-English build must not fall back to hard-coded text here.
    S.notice = { title: t('secrets.showTitle', { version: id }), body:
      `${t('secrets.mysqlRoot')}: ${d.mysqlRoot || '—'}\n${t('secrets.internal')}: ${d.internal || '—'}\n`
      + `${t('secrets.flask')}: ${d.flask || '—'}\n${t('secrets.muip')}: ${muipKeyText(d.muip)}` };
    render();
  },
  secretsGen: (key) => { const a = new Uint8Array(16); crypto.getRandomValues(a); S.secrets.form[key] = Array.from(a).map((b) => b.toString(16).padStart(2, '0')).join(''); render(); },
  secretsApply: (id) => {
    const f = S.secrets.form;
    if (!['mysqlRoot', 'internal', 'flask', 'muip'].some((k) => f[k] && f[k].trim())) return showToast(t('toast.secretsNothing'), 'bad');
    S.confirm = { title: t('confirm.secrets.title', { id }), body: t('confirm.secrets.body'), yes: t('confirm.secrets.yes'), act: 'secretsApplyGo', arg: id, at: Date.now() };
    render();
  },
  secretsApplyGo: (id) => { const f = S.secrets.form; S.secrets.form = {}; S.secrets.data = null; runServerJob('secrets.set', id, { mysqlRoot: f.mysqlRoot || '', internal: f.internal || '', flask: f.flask || '', muip: f.muip || '' }); },

  // ── admin: official 2021 hotpatch mirror ──
  hotpatchVersion: (id) => { S.hotpatch.version = id; S.hotpatch.data = null; render(); loadHotpatch(id); },
  hotpatchLoad: (id) => loadHotpatch(id, true),
  // The sizes in these dialogs are the server's own numbers for THIS version (1.6 and 2.8 differ).
  hotpatchEnable: (id) => {
    const d = hotpatchDataOf(id);
    const p = voicePlan(d);
    const names = (list) => list.map((l) => l.name).join(', ');
    let body = t('confirm.hotpatch.enableBody', { mb: mb(d && d.files && d.files.bytes) });
    // Said BEFORE the fixes are advertised: the voice languages nobody will be served, the selected
    // ones the mirror cannot fetch (on-demand off), and the ones left to the on-demand fallback.
    if (p && p.unserved.length) body += ' ' + t('confirm.hotpatch.voiceEnableWarn', { langs: names(p.unserved) });
    if (p && p.unfetched.length) body += ' ' + t('confirm.hotpatch.voiceEnableUnfetched', { langs: names(p.unfetched) });
    if (p && p.onDemand.length) body += ' ' + t('confirm.hotpatch.voiceEnableOnDemand', { langs: names(p.onDemand) }) + (voiceShortWait(id) ? ' ' + t('hotpatch.voice.shortWait') : '');
    S.confirm = { title: t('confirm.hotpatch.enableTitle', { id }), body, yes: t('confirm.hotpatch.enableYes'), act: 'hotpatchEnableGo', arg: id, at: Date.now() };
    render();
  },
  hotpatchEnableGo: (id) => runServerJob('hotpatch', id, { enabled: true }),
  // Enabled but files are missing on the box: the same enable job fetches only what is missing and
  // restarts nothing when the advertised configuration did not change (agent 3.1).
  hotpatchRefetch: (id) => {
    S.confirm = { title: t('confirm.hotpatch.refetchTitle', { id }), body: t('confirm.hotpatch.refetchBody'), yes: t('confirm.hotpatch.refetchYes'), act: 'hotpatchEnableGo', arg: id, at: Date.now() };
    render();
  },
  hotpatchDisable: (id) => {
    const d = hotpatchDataOf(id);
    const p = voicePlan(d);
    // The purge deletes the whole mirrored branch — the voice packs with it (the selection is kept).
    const sizes = { mb: mb(d && d.files && d.files.cachedBytes), gb: gb(p ? p.diskBytes : 0) };
    const label = p && p.diskBytes > 0 ? t('confirm.hotpatch.voiceDisablePurge', sizes) : t('confirm.hotpatch.purge', sizes);
    S.confirm = { title: t('confirm.hotpatch.disableTitle', { id }), body: t('confirm.hotpatch.disableBody'), yes: t('confirm.hotpatch.disableYes'), checkbox: { label, checked: false }, act: 'hotpatchDisableGo', arg: id, at: Date.now() };
    render();
  },
  hotpatchDisableGo: (id, purge) => runServerJob('hotpatch', id, { enabled: false, purge: purge === true }),
  // Voice packs (agent 3.3): the ticks are local until the confirmed job saves them on the server.
  hotpatchVoiceToggle: (lang) => {
    const sel = S.hotpatch.voiceSel;
    if (!sel || srvBusy()) return;
    sel[lang] = sel[lang] !== true; render();
  },
  hotpatchVoice: (id) => {
    const p = voicePlan(hotpatchDataOf(id));
    if (!p) return;
    const names = (list) => list.map((l) => l.name).join(', ');
    // What ONE player of a language downloads is that language's whole set, whatever the server has cached.
    const perLang = p.ticked.map((l) => t('confirm.hotpatch.voiceLang', { name: l.name, gb: gb(l.bytes) })).join(', ');
    let body = !p.ticked.length ? t('confirm.hotpatch.voiceNoneBody')
      : t(p.download > 0 ? 'confirm.hotpatch.voiceBody' : 'confirm.hotpatch.voiceCachedBody', { gb: gb(p.download), langs: names(p.ticked), perLang });
    // On 2.8 the on-demand path does not save a login (voiceShortWait): said while GBs are still owed.
    if (p.download > 0 && voiceShortWait(id)) body += ' ' + t('hotpatch.voice.shortWait');
    // Unticked with something on disk: complete packs keep being served; what a stopped run left
    // unfinished is served to nobody. The box below deletes both.
    const kept = p.loose.filter((l) => l.cachedBytes > 0), leftover = p.loose.filter((l) => !(l.cachedBytes > 0));
    if (kept.length) body += ' ' + t('confirm.hotpatch.voiceKeep', { langs: names(kept) });
    if (leftover.length) body += ' ' + t('confirm.hotpatch.voiceLeftover', { langs: names(leftover) });
    // The agent's free-space rule, said before it answers 409. The purge box is never pre-ticked (it
    // deletes) — the admin is told that this run needs it.
    const space = { need: gb(p.download), free: gb(p.free), reserve: gb(p.reserve), langs: names(p.loose), gb: gb(p.looseBytes) };
    if (p.hard) body += ' ' + t('hotpatch.voice.noSpace', space);
    else if (p.needsPurge) body += ' ' + t('confirm.hotpatch.voiceNeedsPurge', space);
    S.confirm = {
      title: t(!p.ticked.length ? 'confirm.hotpatch.voiceNoneTitle' : p.download > 0 ? 'confirm.hotpatch.voiceTitle' : 'confirm.hotpatch.voiceApplyTitle', { id }),
      body, yes: t(p.ticked.length && p.download > 0 ? 'confirm.hotpatch.voiceYes' : 'confirm.hotpatch.voiceApplyYes'),
      checkbox: p.loose.length ? { label: t('confirm.hotpatch.voicePurge', { gb: gb(p.looseBytes) }), checked: false } : null,
      // The list is frozen with the numbers the admin read: a refresh behind the dialog cannot change it.
      act: 'hotpatchVoiceGo', arg: { id, languages: p.ticked.map((l) => l.name) }, at: Date.now(),
    };
    render();
  },
  hotpatchVoiceGo: (a, purge) => runServerJob('hotpatch.voice', a.id, { languages: a.languages, purge: purge === true }),
  // Stop the running voice job of this version (agent 3.3: POST {version, cancel:true} — a plain call,
  // not a job, so it gets through while this launcher follows that very job). The job then ends
  // within about one chunk — as server.error "Stopped on request …" when it is ours — and the file it
  // was fetching keeps its .part, which the next run resumes. One click: the button stays dead until
  // the job's state changes (refreshHotpatchAfterJob) or the call failed.
  hotpatchVoiceStop: async (id) => {
    if (S.hotpatch.voiceStop === id || !voiceJobRunning(id)) return;
    const target = srvTarget();
    const release = () => { if (S.hotpatch.voiceStop === id) S.hotpatch.voiceStop = null; };
    S.hotpatch.voiceStop = id; render();
    try {
      const r = await rpc('server.hotpatch.voice.cancel', { version: id });
      if (srvTarget() !== target) return;
      if (r && r.stopping) showToast(t('hotpatch.voice.stopRequested'), 'ok');
      // Nothing to stop: the agent has just said that no voice job of this version runs. A tracked one
      // is therefore over, whatever /status named since — a watchdog job beside it masks its end in
      // trackHotpatchBusy, and the Stop button stayed drawn until that job was over too. Drop it and
      // re-read the card, then the status.
      else {
        const b = S.hotpatch.busy;
        if (b && b.kind === 'hotpatch-voice' && b.version === String(id)) { S.hotpatch.busy = null; refreshHotpatchAfterJob(b.kind, id, true); }
        release(); showToast(t('hotpatch.voice.stopNone'), 'ok'); checkServers(true);
      }
    } catch (e) {
      if (srvTarget() !== target) return;
      release(); showToast(e.message, 'bad');
    }
  },

  // ── admin: agent settings (agent 3.6) ──
  agentCfgLoad: () => loadAgentCfg(),
  agentCfgOpen: () => { S.agentCfg.open = !S.agentCfg.open; render(); },
  agentCfgToggle: (key) => {
    const c = S.agentCfg, s = agentCfgSettings().find((x) => x.key === key);
    if (!s || s.env || !c.data || !c.data.writable || c.saving || c.restarting) return;
    c.form[key] = agentCfgField(s) === '1' ? '0' : '1';
    render();
  },
  // Only the changed fields, as strings ('' = remove the line). The agent validates everything before it
  // writes anything and names the key it refuses (400 / 409), which the toast shows as it is.
  agentCfgSave: async () => {
    const c = S.agentCfg, d = c.data;
    if (!d || d.tooOld || !d.writable || c.saving || c.restarting) return;
    const changed = agentCfgChanged();
    if (!changed.length) return;
    const bad = changed.find((s) => (s.type === 'mib' || s.type === 'seconds') && !/^\d*$/.test(agentCfgField(s).trim()));
    if (bad) return showToast(t('agentCfg.badNumber', { setting: agentCfgLabel(bad.key) }), 'bad');
    const set = {};
    changed.forEach((s) => { set[s.key] = agentCfgField(s).trim(); });
    // A settings read still in flight (the Server page opened again, a folder job settled) holds the
    // file from before this save and must not land over its reply: drop it, and the loading flag it
    // would have cleared.
    agentCfgSeq++; c.loading = false;
    c.saving = true; render();
    try {
      const r = await rpc('agent.config.set', { set });
      if (S.agentCfg !== c) return; // another server now: this reply describes the old one
      agentCfgApply(r, false);
      c.restartRequired = r && Array.isArray(r.restartRequired) ? r.restartRequired : [];
      showToast(t('toast.agentCfgSaved'), 'ok');
      // A live key may show elsewhere (the server name in the status, the local agent's facts).
      refreshServersQuiet();
      if (r && r.local) loadLocalAgent();
    } catch (e) {
      if (S.agentCfg === c) showToast(e.message, 'bad');
    }
    if (S.agentCfg === c) { c.saving = false; render(); }
  },
  // The backend asks the agent to restart and follows it until it answers with a new start time (up to
  // a minute); back:false is not an error — the agent may be slow, or supervised elsewhere. No job may
  // run meanwhile (the agent refuses with 409; the button is dead under srvBusy()).
  agentRestart: async () => {
    const c = S.agentCfg, d = c.data;
    if (!d || !d.restart || c.restarting || srvBusy()) return;
    const local = !!d.local;
    c.restarting = true; render();
    try {
      const r = await rpc('agent.restart');
      if (S.agentCfg !== c) return;
      // back:false carries the backend's own words (where to look on the box) when it has them.
      const slow = !!r && r.back === false;
      showToast(slow ? (typeof r.message === 'string' && r.message ? r.message : t('toast.agentRestartSlow')) : t('toast.agentRestarted'), slow ? 'bad' : 'ok');
    } catch (e) {
      if (S.agentCfg !== c) return;
      showToast(e.message, 'bad');
    }
    c.restarting = false; c.restartRequired = [];
    render();
    // A restarted agent may run with another mirror folder, another public address...: read it all again.
    loadAgentCfg(); checkServers(true);
    if (S.hotpatch.data && S.hotpatch.version) loadHotpatch(S.hotpatch.version);
    if (local) loadLocalAgent();
  },
  agentCfgReloc: (id) => {
    const c = S.agentCfg, d = c.data;
    if (!d || d.tooOld || !d.writable || c.restarting || srvBusy()) return;
    const vi = agentCfgVersion(id);
    clearTimeout(relocTimer);
    c.reloc = { version: id, from: vi.dir, dir: '', mode: vi.present ? 'move' : 'repoint', check: null, checking: false, soon: false, seq: 0, at: Date.now() };
    render();
  },
  // The option cards, Cancel and Start of the folder dialog ignore the first ~400 ms like the confirm
  // dialog's: the second click of a double-click on "Change..." must not pick an option.
  relocMode: (m) => {
    const r = S.agentCfg.reloc;
    if (!r || Date.now() - (r.at || 0) < CONFIRM_ARM_MS || r.mode === m || !relocModes(r).some((o) => o.id === m && !o.dis)) return;
    r.mode = m;
    render();
    relocCheck(); // "remove" needs no folder; the other two re-check the folder already typed under the new mode
  },
  relocClose: () => {
    const r = S.agentCfg.reloc;
    if (r && Date.now() - (r.at || 0) < CONFIRM_ARM_MS) return;
    clearTimeout(relocTimer); S.agentCfg.reloc = null; render();
  },
  relocCheck: () => relocCheck(),
  relocPick: async () => {
    const r = S.agentCfg.reloc;
    if (!r || r.mode === 'remove') return;
    try {
      const p = await rpc('pickFolder', { current: String(r.dir || '').trim() || r.from || '', title: t('agentCfg.reloc.pickTitle') });
      if (p && p.path && S.agentCfg.reloc === r) { r.dir = p.path; render(); relocCheck(); }
    } catch (e) { showToast(e.message, 'bad'); }
  },
  // The dialog closes and the job's log takes over (the console, the version card's WORKING). The
  // check's verdict on the drive lets the console offer Stop from the first second of a cross-drive
  // copy — also for an unknown verdict (null), which the agent copies too; a copy the check did not
  // foresee (a rename refused across mount points) brings Stop with its own log line (server.log).
  relocStart: () => {
    const r = S.agentCfg.reloc;
    if (!r || Date.now() - (r.at || 0) < CONFIRM_ARM_MS || !relocStartOk(r)) return;
    const q = relocQuery(r), cross = q.mode === 'move' && r.check.res.sameVolume !== true;
    clearTimeout(relocTimer); S.agentCfg.reloc = null;
    runServerJob('relocate', q.version, { dir: q.dir, mode: q.mode });
    const j = S.srvJob;
    if (j && j.running && j.kind === 'relocate' && j.version === q.version) { j.crossDrive = cross; render(); }
  },
  // Stop a cross-drive copy (POST /server/relocate {version, cancel:true} — a plain call that gets
  // through while the job runs): the job ends as "Stopped on request", the copy is removed and the old
  // folder stays in use. One click, dead until the job's state changes — fetchCancel's rule.
  relocCancel: async (id) => {
    const j = S.srvJob;
    if (!j || !j.running || j.kind !== 'relocate' || j.cancelSent || !jobHere() || String(j.version) !== String(id)) return;
    j.cancelSent = true; render();
    try {
      const r = await rpc('server.relocate.cancel', { version: id });
      if (!(r && r.stopping)) { if (S.srvJob === j) j.cancelSent = false; checkServers(true); }
    } catch (e) { if (S.srvJob === j) j.cancelSent = false; showToast(e.message, 'bad'); render(); }
  },

  // ── admin: local Windows agent ──
  localAgentStart: async () => { try { await rpc('localagent.start'); showToast(t('toast.localAgentStarted'), 'ok'); } catch (e) { showToast(e.message, 'bad'); } loadLocalAgent(); },
  localAgentStop: async () => { try { await rpc('localagent.stop'); } catch (e) { showToast(e.message, 'bad'); } loadLocalAgent(); },
  localAgentAutostart: async () => { const la = S.localAgent.status; try { await rpc('localagent.autostart', { enabled: !(la && la.autostart) }); } catch (e) { showToast(e.message, 'bad'); } loadLocalAgent(); },
  localAgentFirewall: async () => { try { await rpc('localagent.firewall'); showToast(t('toast.firewallOpened'), 'ok'); } catch (e) { showToast(e.message, 'bad'); } loadLocalAgent(); },
  localAgentOpenLog: () => rpc('localagent.openLog').catch((e) => showToast(e.message, 'bad')),
  // Start screen → Server Admin mode with the agent installed on this PC. Transactional on the backend
  // (health first, a start only when needed — under the Fiddler guard —, the config token validated
  // against /status, and only then the same connection the install saves); the reply is admin.login's
  // shape, so the tail is loginEnter's.
  localAgentEnter: async () => {
    if (S.localAgent.busy) return;
    // No render() here: the button says what it is doing by itself (see syncLocalAgentButtons),
    // and rebuilding the start screen mid-click is exactly the "refresh" this avoids.
    S.localAgent.busy = true; syncLocalAgentButtons();
    try {
      const r = await rpc('localagent.enter');
      enterShell(r && r.state);
      showToast(t('toast.localAgentEntered', { host: S.serverAddr.host }), 'ok');
      if (r && r.warning) showToast(r.warning, 'bad');
    } catch (e) { showToast(e.message, 'bad'); }
    // enterShell() has already re-rendered on success (another screen); on a failure the start
    // screen is untouched and only the two buttons go back to normal.
    S.localAgent.busy = false; syncLocalAgentButtons();
  },
  // The confirm names what goes (the agent folder and its size, the state file, the mirrored hotpatch
  // files) and what stays (the docker stacks, the databases, every player's progress). The facts come
  // from one localagent.status read — allowed from the start screen too, the rpc has no admin gate.
  // The only elevated step (the firewall rule) is the dialog's checkbox, off by default.
  localAgentUninstall: async () => {
    if (S.localAgent.busy) return;
    let st;
    try { st = await rpc('localagent.status'); } catch (e) { return showToast(e.message, 'bad'); }
    S.localAgent.status = st;
    const stacks = [st.dir16, st.dir28].filter(Boolean).join(', ') || t('confirm.localAgentUninstall.noStacks');
    S.confirm = {
      title: t('confirm.localAgentUninstall.title'),
      body: t('confirm.localAgentUninstall.body', { dir: st.root || '', stacks, size: st.rootBytes == null ? '' : t('confirm.localAgentUninstall.size', { mb: mb(st.rootBytes) }) }),
      yes: t('confirm.localAgentUninstall.yes'),
      act: 'localAgentUninstallGo', arg: null, at: Date.now(),
      checkbox: st.firewallRule ? { label: t('confirm.localAgentUninstall.firewall', { port: st.port }), checked: false } : null,
    };
    render();
  },
  localAgentUninstallGo: async (_, ticked) => {
    S.localAgent.busy = true; render();
    try {
      const r = await rpc('localagent.uninstall', { firewall: ticked === true });
      S.localAgent.status = null;
      if (r && r.state) applyInit(r.state);
      const rep = (r && r.report) || {};
      const left = Array.isArray(rep.leftovers) ? rep.leftovers.length : 0;
      showToast(left ? t('toast.localAgentUninstalledPartial', { n: left }) : t('toast.localAgentUninstalled'), left ? 'bad' : 'ok');
      if (rep.firewallError) showToast(rep.firewallError, 'bad');
      // The launcher was in admin mode on that very agent and has no token left: the backend dropped
      // the mode (like "Change mode or server"), so the shell has nothing to show — back to the panel.
      if (S.mode === '' && S.screen !== 'login') { S.login.panel = 'admin'; S.login.address = S.serverAddr.host || S.login.address; S.screen = 'login'; onceForget('login:'); }
    } catch (e) { showToast(e.message, 'bad'); }
    S.localAgent.busy = false; render();
    if (S.screen === 'server') loadLocalAgent();
  },

  // ── install-agent overlay ──
  // autoDir = the folder fields the form filled in by itself ({dir16: path}) — see deployToggle.
  deployOpen: () => { S.agentInstall = { open: true, target: 'linux', form: deployDefaultForm('linux'), autoDir: {}, running: false, lines: [], done: false, error: null, result: null }; render(); },
  deployOpenWindows: () => { S.agentInstall = { open: true, target: 'windows', form: deployDefaultForm('windows'), autoDir: {}, running: false, lines: [], done: false, error: null, result: null }; render(); prefillWindowsForm(); },
  // A folder the form filled in by itself follows the target (the other OS's path would be refused, or
  // land somewhere odd): the new target's conventional path while its toggle is off. Anything the admin
  // typed or picked never equals the recorded value and stays as it is.
  deployTarget: (tg) => {
    const a = S.agentInstall; if (!a) return;
    a.target = tg;
    const auto = a.autoDir || (a.autoDir = {});
    Object.keys(DEPLOY_DIRS).forEach((k) => {
      const fld = DEPLOY_DIRS[k][0];
      if (!auto[fld] || a.form[fld] !== auto[fld]) { delete auto[fld]; return; }
      if (a.form[k]) { a.form[fld] = ''; delete auto[fld]; }
      else a.form[fld] = auto[fld] = DEPLOY_DIRS[k][tg === 'windows' ? 1 : 2];
    });
    render(); if (tg === 'windows') prefillWindowsForm();
  },
  deployToggle: (k) => {
    const a = S.agentInstall; if (!a) return;
    const f = a.form; f[k] = !f[k];
    // "I already have it" switched OFF: the folder is now the download target, so a blank one gets the
    // conventional path of the target — /home/<v>_live on Linux, C:\relic_servers\<v>_live on Windows
    // (the agent creates it; Browse still picks another). Switched on, nothing is filled: that folder
    // must already hold the extracted package, and a made-up path would only fail the install's check —
    // so a path filled here on the way off is taken back on the way on, unless the admin changed it.
    const dirOf = DEPLOY_DIRS[k];
    if (dirOf) {
      const fld = dirOf[0], auto = a.autoDir || (a.autoDir = {});
      if (!f[k] && !String(f[fld] || '').trim()) f[fld] = auto[fld] = dirOf[a.target === 'windows' ? 1 : 2];
      else if (f[k] && auto[fld] && f[fld] === auto[fld]) { f[fld] = ''; delete auto[fld]; }
    }
    render();
  },
  deployGenToken: () => { const a = new Uint8Array(24); crypto.getRandomValues(a); S.agentInstall.form.token = Array.from(a).map((b) => b.toString(16).padStart(2, '0')).join(''); render(); },
  deployPickKey: async () => {
    try { const r = await rpc('pickFile', { title: t('deploy.ssh.pickKeyTitle'), filter: 'Private keys (*.ppk;*.pem;id_*)|*.ppk;*.pem;id_*|All files (*.*)|*.*' }); if (r && r.path) { S.agentInstall.form.sshKeyPath = r.path; render(); } }
    catch (e) { showToast(e.message, 'bad'); }
  },
  // The picker's title follows the stack's toggle: an extracted package to point at, or (toggle off)
  // the folder the agent will download and install the server into.
  deployPickDir: async (k) => {
    const f = S.agentInstall.form;
    const have = { dir16: 'have16', dir28: 'have28' }[k];
    const title = have && f[have] === false ? t('deploy.stacks.pickTitleInstall') : t('deploy.stacks.pickTitle');
    try { const r = await rpc('pickFolder', { current: f[k] || '', title }); if (r && r.path && S.agentInstall) { S.agentInstall.form[k] = r.path; render(); } }
    catch (e) { showToast(e.message, 'bad'); }
  },
  deployClose: () => {
    const a = S.agentInstall;
    S.agentInstall = null;
    // Installed from the start screen: the backend already saved the connection and switched to admin
    // mode — closing the summary enters the launcher instead of dropping the user back on the panel.
    if (a && a.done && S.screen === 'login' && S.mode === 'admin' && S.serverConfigured) {
      S.login.panel = null; S.login.token = ''; S.login.agentPort = '';
      // No status reset here: a.done is only set by deploy.done, which already started a fresh read.
      // A stack download queued by this install is running: the Server page is where its log is.
      ACTIONS.go(jobRunning() && S.srvJob.kind === 'fetch' ? 'server' : 'library');
      return;
    }
    render();
  },
  // Back to the form after a failure, keeping everything typed (host, paths, IPs — and the token, so a
  // retry does not mint a second one the box may already be using). Only the log goes.
  deployBack: () => {
    const a = S.agentInstall; if (!a) return;
    a.error = null; a.done = false; a.running = false; a.lines = []; a.result = null;
    render();
  },
  deployStart: async () => {
    const a = S.agentInstall; if (!a) return;
    a.lines = []; a.error = null; a.done = false; a.running = true; render();
    // fetch16/fetch28 = the "I already have it" toggles, inverted: a version switched off is marked for
    // the archive.org download (the plan skips its folder check; deploy.done lists it in `fetch`).
    const payload = Object.assign({ target: a.target, fetch16: a.form.have16 === false, fetch28: a.form.have28 === false }, a.form);
    try { await rpc(a.target === 'windows' ? 'localagent.install' : 'agent.install', payload); }
    catch (e) { a.running = false; a.error = e.message; render(); }
  },
};

// Defaults of the install-agent form: the address already typed on the start screen for the SSH
// host, the standard listen address, and a fresh token.
// sshPort must be seeded as a NUMBER: setModel only parses an edit when the value it replaces is
// already one, so a '22' string stayed a string all the way to the backend, where reading it threw
// "The requested operation requires an element of type 'Number'" before the install ever started.
// sshHost goes through hostOf(): the start-screen address is documented to allow ":port", and SSH
// must not be pointed at "host:21000".
// have16/have28 = "I already have the package extracted" (default on: the folder is checked, nothing
// is downloaded); muipKey empty = the agent picks a random sign key when it prepares the stack.
// The conventional folder of each server stack, per "I already have it" toggle: [form field, Windows,
// Linux] — the placeholders of the install form and the value a blank field gets when its toggle goes
// off (the download target). Never a default for a toggle that is on (see deployToggle).
const DEPLOY_DIRS = {
  have16: ['dir16', 'C:\\relic_servers\\1.6_live', '/home/1.6_live'],
  have28: ['dir28', 'C:\\relic_servers\\2.8_live', '/home/2.8_live'],
};
function deployDefaultForm(target) {
  const f = { sshHost: hostOf(S.login.address) || S.serverAddr.host || '', sshPort: 22, sshUser: 'root', sshPassword: '', sshKeyPath: '', sshKeyPassphrase: '', sudoPassword: '',
    dir16: '', dir28: '', have16: true, have28: true, bindIp: '', advertisedIp: '', advertisedHost: '', listen: '0.0.0.0:18080', token: '', muipHost: '', muipKey: '', serverName: '', region: 'dev_docker', autostart: true, upgradeForce: false };
  const a = new Uint8Array(24); crypto.getRandomValues(a); f.token = Array.from(a).map((b) => b.toString(16).padStart(2, '0')).join('');
  if (target === 'windows') { f.sshHost = ''; }
  return f;
}

// The Windows form pre-filled from the agent already installed on this PC (localagent.status: its
// stack folders, its bind IP and MUIP host, its public IP / DNS name, its name and its listen address),
// so a re-install / upgrade does not ask for them again — the form writes GIO_BIND_IP and GIO_MUIP_HOST
// even when blank, and a blank one would drop what the Agent settings card saved. Blank fields only —
// never over something the admin typed (a folder the form filled in by itself counts as blank); the
// listen address only while the form still holds the standard one — and the status is read here when
// the Server page has not loaded it (the start screen).
async function prefillWindowsForm() {
  const a = S.agentInstall;
  if (!a || !S.isWindows) return;
  let st = S.localAgent.status;
  if (!st) {
    if (!(S.localAgent.init && S.localAgent.init.installed)) return;
    try { st = await rpc('localagent.status'); } catch (e) { return; }
    if (S.agentInstall !== a) return; // the form was closed meanwhile
    S.localAgent.status = st;
  }
  if (!st || !st.installed) return;
  const f = a.form, auto = a.autoDir || {};
  const blank = (k) => !String(f[k] || '').trim();
  const given = (v) => typeof v === 'string' && v.trim() !== '';
  ['dir16', 'dir28'].forEach((k) => {
    if ((blank(k) || (auto[k] && f[k] === auto[k])) && given(st[k])) { f[k] = st[k].trim(); delete auto[k]; }
  });
  // The configured bind IP itself (bindIp); a backend without it: the dialled host, when not loopback.
  if (typeof st.bindIp === 'string') { if (blank('bindIp') && given(st.bindIp)) f.bindIp = st.bindIp.trim(); }
  else if (blank('bindIp') && st.host && st.host !== '127.0.0.1') f.bindIp = String(st.host);
  if (blank('muipHost') && given(st.muipHost)) f.muipHost = st.muipHost.trim();
  if (blank('advertisedIp') && given(st.advertisedIp)) f.advertisedIp = st.advertisedIp.trim();
  if (blank('advertisedHost') && given(st.advertisedHost)) f.advertisedHost = st.advertisedHost.trim();
  if (blank('serverName') && given(st.serverName)) f.serverName = st.serverName.trim();
  if (String(f.listen || '').trim() === '0.0.0.0:18080' && given(st.listen)) f.listen = st.listen.trim();
  render();
}

// The tail every way into the shell shares (the start screen's Enter, "Use the agent on this PC"):
// forget the panel, apply the reply's init state — another server or mode is a new status epoch,
// started inside applyInit (srvRetarget); re-entering the same one keeps its still-valid status and
// just refreshes it — and open the Library.
function enterShell(d) {
  // An install form opened on the start screen and left there must not follow us into the shell.
  if (S.agentInstall && !S.agentInstall.running) S.agentInstall = null;
  S.login.token = ''; S.login.agentPort = ''; S.login.panel = null; onceForget('login:');
  const moved = applyInit(d);
  ACTIONS.go('library');
  if (!moved) refreshServersQuiet();
}

// The stack downloads an agent install queued (S.fetchQueue): one at a time — the agent runs one
// operation anyway — the next started by the server.done of the previous; a failed or stopped one
// empties the queue (the card's own button resumes it).
function startNextFetch() {
  if (!S.fetchQueue.length || jobRunning() || !S.serverConfigured) return;
  runServerJob('fetch', S.fetchQueue.shift());
}

// Every way an install job ends for the UI — closed, abandoned, cancelled, refused at start —
// forgets the same facts in one place, so a voices job can never leave its title or its request
// behind for the next install.
function clearActiveInstall() {
  S.installing = false; S.activeInstallId = null; S.activeInstallLocal = null;
  S.activeInstallVoices = null; S.activeInstallReq = null;
}

// ── "Prepare the server" chooser (agent 3.4) ──
// The agent's version as the last admin status named it, compared as dotted numbers. false when it
// is unknown (no status yet) or not a version (the public snapshot sends the major only — the dialog
// is admin-only, so that never decides anything here).
function agentAtLeast(min) {
  const cur = String((S.srv.info && S.srv.info.agent) || '').trim();
  if (!/^\d+(\.\d+)*$/.test(cur)) return false;
  const a = cur.split('.').map(Number), b = String(min).split('.').map(Number);
  for (let i = 0; i < Math.max(a.length, b.length); i++) { const x = a[i] || 0, y = b[i] || 0; if (x !== y) return x > y; }
  return true;
}
// The three provisioning choices, with their texts (translation keys, translated when drawn). Only
// `default` is a wipe — every account created on the version goes with the import — hence its red Yes.
const PREPARE_MODES = {
  default: { label: 'confirm.prepare.opt.default', sub: 'confirm.prepare.opt.default.sub', body: 'confirm.prepare.body.default', yes: 'confirm.prepare.yes.default', danger: true },
  keep: { label: 'confirm.prepare.opt.keep', sub: 'confirm.prepare.opt.keep.sub', body: 'confirm.prepare.body.keep', yes: 'confirm.prepare.yes.keep', danger: false },
  fixes: { label: 'confirm.prepare.opt.fixes', sub: 'confirm.prepare.opt.fixes.sub', body: 'confirm.prepare.body.fixes', yes: 'confirm.prepare.yes.fixes', danger: false },
};
// kind = 'setup' (the card's Prepare) | 'provision' (the reset button, the Advanced re-apply). An
// agent older than 3.4 ignores the field and imports the save whatever was chosen, so it is offered
// the default only, with the reason. The pathfinding row rides on setup alone (the agent applies it
// BEFORE the bootstrap renders the compose file): its default is the file's current state, else the
// agent's own default, else on.
function prepareDialog(id, kind) {
  const f = srvFacts(id);
  const info = S.srv.info || {};
  const modern = agentAtLeast('3.4');
  const modes = modern ? ['default', 'keep', 'fixes'] : ['default'];
  const account = catalogueAccountOf(id) || '—';
  const bodyFor = {}, yesFor = {};
  modes.forEach((m) => { bodyFor[m] = t(PREPARE_MODES[m].body); yesFor[m] = t(PREPARE_MODES[m].yes); });
  if (!modern) bodyFor.default = t('confirm.prepare.agentTooOld', { agent: info.agent || '?' }) + ' ' + bodyFor.default;
  const pf = f && f.pathfinding !== null ? f.pathfinding : (typeof info.pathfindingDefault === 'boolean' ? info.pathfindingDefault : true);
  S.confirm = {
    title: t('confirm.prepare.title', { id }), wide: true,
    options: modes.map((m) => ({ id: m, label: t(PREPARE_MODES[m].label), sub: t(PREPARE_MODES[m].sub, { account }), danger: PREPARE_MODES[m].danger })),
    selected: 'default', bodyFor, yesFor,
    checkbox: kind === 'setup' && modern ? { label: t('server.prepare.pathfinding'), sub: t('server.prepare.pathfindingSub'), checked: pf } : null,
    act: 'prepareGo', arg: kind + ':' + id,
    at: Date.now(), // starts the anti-double-click arming window
  };
  render();
}

async function loadPolicy() {
  if (!isAdminMode()) return;
  S.policy.loading = true; render();
  try { S.policy.data = await rpc('account.policy.get'); }
  catch (e) { S.policy.data = null; }
  S.policy.loading = false;
  if (S.screen === 'server' || S.screen === 'settings') render();
}

// GET /server/templates of one version for the "Create a player account" card: which progress
// templates are imported on that stack (with their labels). The last request per version wins, and
// only under the target it was sent to — the rule of loadHotpatch; the counter is module-wide so a
// reset S.acctNew (srvRetarget) can never re-issue a number an older, still in-flight request holds.
// A failed read leaves list null: the card falls back to the admin /status records.
let acctTplSeq = 0;
async function loadAcctTemplates(id) {
  if (!isAdminMode() || !S.serverConfigured || !id) return;
  const a = S.acctNew, seq = ++acctTplSeq, target = srvTarget();
  const mine = () => S.acctNew === a && a.tplSeq[id] === seq && srvTarget() === target;
  a.tplSeq[id] = seq;
  a.tpl[id] = Object.assign({}, a.tpl[id], { loading: true });
  let list = null;
  try {
    const r = await rpc('server.templates', { version: id });
    // imported: the agent's own verdict; one that does not send it is read from the record itself.
    if (r && Array.isArray(r.templates)) list = r.templates.filter((x) => x && typeof x.id === 'string' && x.id).map((x) => ({
      id: x.id, label: typeof x.label === 'string' && x.label ? x.label : x.id,
      imported: typeof x.imported === 'boolean' ? x.imported : !!(x.createdAt && !x.error),
    }));
  } catch (e) { /* an older agent, or unreachable: list stays null */ }
  if (!mine()) return;
  a.tpl[id] = { list, loading: false };
  if (S.screen === 'server' && !typingNow()) render();
}
const loadAcctTemplatesAll = () => S.versions.forEach((v) => loadAcctTemplates(v.id));

// GET /server/hotpatch of one version. Silent by default (an agent older than 3.1 answers 404 and
// the section simply offers "Load status"); loud = the user pressed the button and wants the reason.
// The last REQUEST wins, and only under the target it was sent to (the rule of the status reads, see
// srvSeq): before, the previous server's late reply was drawn — voice rows, ticks and a live Mirror
// button — under the new server's name and keyed as the new server's, and two replies of one server
// could land out of order. srvRetarget bumps the seq as well, so A → B → A drops A's old reply too.
let hotpatchSeq = 0;
async function loadHotpatch(id, loud) {
  if (!isAdminMode() || !S.serverConfigured || !id) return;
  const seq = ++hotpatchSeq, target = srvTarget();
  const mine = () => seq === hotpatchSeq && srvTarget() === target;
  S.hotpatch.version = id; S.hotpatch.loading = true; if (S.screen === 'server') render();
  try { const d = await rpc('server.hotpatch.get', { version: id }); if (mine()) { S.hotpatch.data = d; syncVoiceSel(id, d, target); } }
  catch (e) { if (mine()) { S.hotpatch.data = null; if (loud) showToast(e.message, 'bad'); } }
  if (!mine()) return; // the newer request owns `loading` and the redraw
  S.hotpatch.loading = false;
  if (S.screen === 'server') render();
}
// While one of the two hotpatch jobs runs, this card is the only place its progress shows as numbers
// ("Files on the server", the per-language "cached x/y"): the job log scrolls past, the counters used
// to stand still until the job ended or the admin pressed Refresh. So re-read them on a short interval
// while such a job downloads — quietly: no "checking" state (the Refresh button would blink disabled
// every tick) and no redraw unless the agent's answer actually changed or while the user is typing,
// the rule of refreshServersQuiet. The tick is a no-op the rest of the time: GET /server/hotpatch stats
// every mirrored file and walks every voice-pack folder on the box, which is not something to ask for
// every few seconds when nothing is moving. One request at a time (hotpatchQuiet), and the seq/target
// discipline of loadHotpatch applies unchanged — another server's late reply is never drawn here.
const HOTPATCH_POLL_MS = 5000;
let hotpatchQuiet = false;
async function refreshHotpatchQuiet() {
  const id = hotpatchCur();
  if (!isAdminMode() || !S.serverConfigured || !id || S.screen !== 'server') return;
  if (hotpatchQuiet || S.hotpatch.loading || !hotpatchJobRunning(id)) return;
  const shown = hotpatchDataOf(id);
  if (!shown || !shown.available) return; // nothing loaded (or no manifest): the card has no numbers to move
  const seq = ++hotpatchSeq, target = srvTarget();
  const before = JSON.stringify(S.hotpatch.data);
  hotpatchQuiet = true;
  try {
    const d = await rpc('server.hotpatch.get', { version: id });
    if (seq !== hotpatchSeq || srvTarget() !== target) return; // a tab switch / a loud Refresh owns the card now
    S.hotpatch.data = d; syncVoiceSel(id, d, target);
  } catch (e) {
    return; // a failed poll asserts nothing: the card keeps the numbers it has, the job log says the rest
  } finally {
    hotpatchQuiet = false;
  }
  if (JSON.stringify(S.hotpatch.data) !== before && S.screen === 'server' && !typingNow()) render();
}
// After a server job the mirror state may have changed (hotpatch itself, but also start / provision /
// netfix re-apply the advertisement): re-read it when the section had been loaded.
// A finished voice job — ours (server.done / server.error) or one /status stopped naming (the agent's
// 'hotpatch-voice': started elsewhere, or ours after the link was lost — trackHotpatchBusy) — re-takes
// the ticks from the server's selection; a failed one of ours keeps them unless the selection changed
// on the server (refused before it was saved — not enough disk, say — the admin unticks one and retries).
// Never a tab switch: a voice job runs for an hour, the admin is likely on the other version's tab by
// then, and loadHotpatch names the tab before its reply is in. That tab's chip reloads it anyway.
function refreshHotpatchAfterJob(kind, version, ok) {
  const h = S.hotpatch;
  const voice = kind === 'hotpatch.voice' || kind === 'hotpatch-voice';
  if (kind !== 'hotpatch' && !voice) { if (h.data && h.version) loadHotpatch(h.version); return; }
  if (voice && h.voiceStop === String(version)) h.voiceStop = null; // that job's state changed: Stop is live again
  if (String(hotpatchCur()) !== String(version)) return;
  if (voice && ok) h.voiceKey = '';
  loadHotpatch(hotpatchCur());
}
// The hotpatch / voice job the admin /status names, remembered so that its END is seen: server.done /
// server.error exist only for a job this launcher started and still follows. Without this the card of
// a job started elsewhere (or gone on after the link was lost) stayed as it was before the job, and its
// Apply silently dropped a language another admin had selected meanwhile (languages[] is a full replace).
function trackHotpatchBusy(busy) {
  const h = S.hotpatch, prev = h.busy, kind = busy && busy.kind;
  if (prev && prev.kind === 'hotpatch-voice' && BESIDE_VOICE_JOBS[kind]) return; // masked, not over
  const now = kind === 'hotpatch' || kind === 'hotpatch-voice'
    ? { kind, version: String(busy.version == null ? '' : busy.version), id: String(busy.id == null ? '' : busy.id) } : null;
  h.busy = now;
  if (!prev || (now && now.kind === prev.kind && now.version === prev.version && now.id === prev.id)) return;
  // A job of ours followed on THIS server: its server.done / server.error re-reads the card. One
  // followed on another server says nothing about this card — its events never re-read it.
  if (jobHere()) return;
  refreshHotpatchAfterJob(prev.kind, prev.version, true);
}

// GET /agent/config for the "Agent settings" card. The last request wins, and only under the target it
// was sent to and for the card object it was sent for (srvRetarget / deploy.done replace it) — the rule
// of loadHotpatch. A background re-read keeps what the admin has typed and not saved (agentCfgApply). A
// failed read drops the data like the other cards (loadPolicy): the card offers Retry with the reason.
let agentCfgSeq = 0;
async function loadAgentCfg() {
  if (!isAdminMode() || !S.serverConfigured) return;
  const c = S.agentCfg, seq = ++agentCfgSeq, target = srvTarget();
  const mine = () => seq === agentCfgSeq && S.agentCfg === c && srvTarget() === target;
  c.loading = true;
  if (S.screen === 'server' && !c.data && !typingNow()) render();
  try {
    const d = await rpc('agent.config.get');
    if (!mine()) return;
    agentCfgApply(d, true);
    c.error = '';
  } catch (e) {
    if (!mine()) return;
    c.data = null; c.form = {}; c.error = e.message || String(e);
  }
  c.loading = false;
  if (S.screen === 'server' && !typingNow()) render();
}

async function loadLocalAgent() {
  if (!S.isWindows || !isAdminMode()) return;
  try { S.localAgent.status = await rpc('localagent.status'); }
  catch (e) { S.localAgent.status = null; }
  if (S.screen === 'server') render();
}

function sendCommand(category, value) {
  const version = S.selectedVersionId || (S.versions[0] && S.versions[0].id);
  return rpc('command.send', { version, category, value, uid: S.cmdUid.trim() });
}

// Pause and cancel share one backend mechanic (the download is resumable, so "pause" is a cancel
// whose partial files the next start picks up) — the stored intent decides what install.cancelled
// does. The intent doubles as the in-flight flag: it disables both buttons until the event lands.
async function stopInstall(intent) {
  if (!S.installing || S.installPaused || S.installStopIntent || S.installDone || S.installError) return;
  S.installStopIntent = intent; render();
  try {
    const r = await rpc('install.cancel');
    // Nothing was running (the install finished while the click was in flight) — undo the intent;
    // the install.done/install.error that raced us paints the real outcome.
    if (!r || !r.cancelling) { S.installStopIntent = null; render(); }
  } catch (e) { S.installStopIntent = null; showToast(e.message, 'bad'); render(); }
}

// Every send goes through here: same preconditions as before, plus an in-flight flag set BEFORE the
// first await — a double-click on "Send set" would otherwise fire ten GM commands.
async function guardedSend(fn) {
  if (!S.serverConfigured) return showToast(t('toast.serverNotConfigured'), 'bad');
  // Player mode: only when the server's policy opens GM commands to players (the backend routes the
  // call through the agent's public endpoint, which refuses otherwise).
  if (!isAdminMode() && !playerCommandsAllowed()) return showToast(t('toast.commandsAdminOnly'), 'bad');
  if (!S.cmdUid.trim()) return showToast(t('toast.enterUid'), 'bad');
  if (S.sending) return showToast(t('toast.alreadySending'), 'bad');
  S.sending = true; render();
  try { await fn(); }
  catch (e) { addLog('bad', t('cmd.log.error', { error: e.message })); }
  finally { S.sending = false; render(); }
}

// Never fatal: an older backend has no profile.status case, and the settings screen must still open.
async function loadProfileStatus() {
  try { S.profile = await rpc('profile.status'); }
  catch (e) { S.profile = null; }
  if (S.screen === 'settings') render();
}

// Epoch of the status requests: three paths write S.srv from independent rpcs (checkServers,
// freshServerCheck, refreshServersQuiet), and the replies arrive in network order, not request
// order — a stuck poll (60 s HTTP timeout) would otherwise overwrite, minutes later, the fresh data
// of a Check pressed in between. The last REQUEST wins: any reply from a superseded epoch is dropped.
// A change of target (srvRetarget) is an epoch boundary too.
let srvSeq = 0;
const srvSeqCurrent = (seq) => seq === srvSeq;
// The seq of the status request that currently owns S.srv (0 = none). A quiet poll skips only while
// that owner is still CURRENT: a superseded request (another server's, say) never blocks a new one,
// and each request clears the mark only if it is still its own.
let srvPollSeq = 0;
// No retarget before boot's first applyInit has been applied and the first poll issued.
let srvBooted = false;
// What the status describes: the mode (admin /status vs the public snapshot), the server's host and
// agent port, and whether there is anything to ask at all. Tagged on every request at send time; a
// reply that lands under another target is dropped (defence in depth next to the epoch bump).
const srvTarget = () => [S.mode, S.serverAddr.host, S.serverAddr.agentPort, S.serverConfigured].join('|');

// A new target — another host, agent port, mode, or configured/unconfigured — starts a new epoch.
// Before, a switch reset S.srv and called refreshServersQuiet, which returned at once while a poll for
// the PREVIOUS server was still in flight and never bumped srvSeq: that poll's reply was then ingested
// as the new server's status ("Server offline" under the new name until the next 30 s tick), and
// Settings / the wizard / the Advanced form did not even reset. The bump drops every in-flight reply;
// 'checking' (not idle) because a read of the new target starts right away.
function srvRetarget() {
  srvSeq++; srvPollSeq = 0;
  S.srv = { state: S.serverConfigured ? 'checking' : 'idle', error: '', versions: {}, info: null };
  S.pub.status = null;
  S.srvAccount = {}; // the previous server's word on the in-game login: not this one's
  // Server packages queued for the box we just left (deploy.done fills this). Starting the next one
  // after a switch would download a 2.5 GB stack onto the NEW server, which may already have it or
  // may not be the one the admin was installing — and the job the admin is watching is the old box's.
  S.fetchQueue = [];
  // Plaintext secrets of the previous server (MySQL root, the shared internal password, Flask): the
  // card is keyed by version only, so without this the next Load-less render shows box A's passwords
  // under box B's name.
  S.secrets = { version: null, data: null, form: {}, loading: false };
  // Likewise the signup card's last result: it belongs to the server it was created on — and so do the
  // admin card's form, its template reads and the session's list of created accounts (passwords
  // included). A create still in flight keeps S.acctPending, so its late events cannot land in these.
  S.account = accountBlank();
  S.acctNew = acctNewBlank();
  // The hotpatch card is the previous server's as well — rows, ticks, a live Mirror button. In-flight
  // reads are dropped by the seq; on the Server screen the new target's card is read right away.
  hotpatchSeq++; S.hotpatch = hotpatchBlank();
  // The admin policy belongs to the server it was read from too -- signups, player commands and the
  // hotpatch mirror source: without this the new server would keep showing the old one's choices
  // until the Server page (its only reader) is opened again.
  S.policy = { data: null, loading: false };
  // The agent-settings card — its file, its folders, an open folder dialog — is the previous agent's too.
  agentCfgSeq++; clearTimeout(relocTimer); S.agentCfg = agentCfgBlank();
  refreshServersQuiet();
  if (S.screen === 'server') { loadHotpatch(hotpatchCur()); loadPolicy(); loadAcctTemplatesAll(); loadAgentCfg(); }
}
const typingNow = () => {
  const a = document.activeElement;
  return !!a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA');
};

// One reader for both shapes server.status can answer with: the admin's full /status (versions[id] =
// {up, present, bootstrapped, servicesDown, ...}) or, in player mode, {public:true, status:{...}} —
// the agent's token-less snapshot (versions[id] = {present, up, healthy, defaultAccount, ...}). Both
// land in S.srv.versions in the admin shape, so the badges and cards read one structure; the public
// snapshot is kept whole in S.pub.status (signup policy, player commands, default accounts).
// statusVersions is the pure half, shared with the PLAY pre-check: null for no reply.
// The public snapshot says only healthy:false, never WHICH services are down: that is `degraded` with an
// empty servicesDown — never a placeholder name, which leaked into the texts as "down ((services))".
function statusVersions(st) {
  if (!st) return null;
  if (!st.public) return st.versions || {};
  const pub = st.status || {};
  const versions = {};
  Object.keys(pub.versions || {}).forEach((id) => {
    const e = pub.versions[id] || {};
    versions[id] = { up: !!e.up, present: e.present !== false, bootstrapped: true, account: e.defaultAccount || '',
      servicesDown: [], degraded: !!(e.up && e.healthy === false), passwordVerify: !!e.passwordVerify, generation: e.generation,
      // {enabled, res, data, silence} — the badge on the library card/hero reads .enabled.
      hotpatch: e.hotpatch && typeof e.hotpatch === 'object' ? e.hotpatch : null };
  });
  return versions;
}
// The reply says nothing about what is RUNNING: the admin /status top-level error (docker compose ls
// failed on the box) or its public projection, statusUnknown (agent 3.2 — a flag, no text). '' = none.
function statusUnknownOf(st) {
  if (!st) return '';
  if (st.public) return st.status && st.status.statusUnknown ? t('server.public.statusUnknown') : '';
  return st.error ? String(st.error) : '';
}
// The server's own word on the pre-made in-game login of every version it HAS (present), kept per
// target in S.srvAccount (accountOf reads it before the catalogue; srvRetarget clears it). Admin
// /status: `account`, but only when the agent says whether the shipped save is in the database (agent
// 3.4 `defaultAccount` true/false — null = its state file is unreadable, no answer; an older agent
// sends no flag and names the manifest account whether or not the save is in, so it is not taken as an
// answer either). Public snapshot: `defaultAccount`, '' meaning NONE.
function noteServerAccounts(st) {
  if (!st) return;
  const vs = (st.public ? st.status && st.status.versions : st.versions) || {};
  Object.keys(vs).forEach((id) => {
    const e = vs[id];
    if (!e || typeof e !== 'object' || e.present === false) return;
    if (st.public) S.srvAccount[id] = typeof e.defaultAccount === 'string' ? e.defaultAccount : '';
    else if (typeof e.defaultAccount === 'boolean') S.srvAccount[id] = e.defaultAccount && typeof e.account === 'string' ? e.account : '';
  });
}
function ingestStatus(st) {
  noteServerAccounts(st);
  if (st && st.public) {
    const pub = st.status || {};
    S.pub.status = pub;
    S.srv.versions = statusVersions(st);
    // error: the field the admin reply carries, so the srvDockerError guards (badge, pill) apply to a
    // public snapshot whose docker read failed on the box too.
    S.srv.info = { public: true, agent: pub.agent, name: pub.name, playerCommands: !!pub.playerCommands, error: statusUnknownOf(st) };
    return;
  }
  S.srv.versions = (st && st.versions) || {};
  S.srv.info = st || null;
  if (st && st.policy) S.pub.status = Object.assign(S.pub.status || {}, { playerCommands: !!st.policy.playerCommands });
  if (st) trackHotpatchBusy(st.busy);
}

// silent: the automatic refresh after an operation. There is a single toast, so a generic toast from
// here would erase the message saying WHAT failed; the error stays written on screen anyway.
async function checkServers(silent) {
  if (!S.serverConfigured) return;
  const seq = ++srvSeq; srvPollSeq = seq;
  const target = srvTarget();
  S.srv.state = 'checking'; S.srv.error = '';
  render();
  try {
    const st = await rpc('server.status');
    if (!srvSeqCurrent(seq) || srvTarget() !== target) return;
    ingestStatus(st);
    S.srv.state = 'ok';
  } catch (e) {
    if (!srvSeqCurrent(seq) || srvTarget() !== target) return;
    // Without fresh data nothing can be asserted: the cards go to "unverified", not OFFLINE (an
    // invented OFFLINE would offer Start for a version that may not even exist on the server).
    // S.pub.status is kept: it is the SAME server's last snapshot (a target change drops it in
    // srvRetarget), the cards already gate on S.srv.state, and clearing it on one failed read took
    // away the Commands tab, the signup card and the shown in-game login until the next poll.
    S.srv.state = 'error'; S.srv.error = e.message; S.srv.versions = {}; S.srv.info = null;
    if (!silent) showToast(t('toast.serverUnreachable', { error: e.message }), 'bad');
  } finally {
    if (srvPollSeq === seq) srvPollSeq = 0;
  }
  render();
}

// The pre-launch check: fresh and with a short timeout. rpc('server.status') can hang for tens of
// seconds on a host that does not answer (the HttpClient timeout is 60s), and the PLAY button must
// not freeze that long — after the timeout it answers null ("unknown"), and the cached state is
// refreshed anyway when the late rpc actually arrives.
function freshServerCheck(timeoutMs) {
  const seq = ++srvSeq; srvPollSeq = seq; // owning it keeps the 30 s poll from superseding the check
  const target = srvTarget();
  const fetchP = rpc('server.status').then((st) => {
    if (srvTarget() !== target) return null; // another server / mode now: this reply describes neither
    if (!srvSeqCurrent(seq)) return st; // the caller gets its reply, the cache stays the newer request's
    ingestStatus(st);
    S.srv.state = 'ok'; S.srv.error = '';
    if (!typingNow()) render(); // a reply arriving after the timeout would have no other render to show it
    return st;
  }).catch((e) => {
    // The failure of an abandoned request asserts nothing — the cache is not cleared for it; only a
    // still-current one becomes the badge's "unverified" state.
    if (srvSeqCurrent(seq) && srvTarget() === target) {
      S.srv.state = 'error'; S.srv.error = e.message; S.srv.versions = {}; S.srv.info = null; // S.pub.status kept, see checkServers
      if (!typingNow()) render();
    }
    return null;
  }).finally(() => { if (srvPollSeq === seq) srvPollSeq = 0; });
  return Promise.race([fetchP, new Promise((res) => setTimeout(() => res(null), timeoutMs))]);
}

// Background refresh for the library badge. Deliberately different from checkServers: no "checking"
// state (the badge would flicker on every poll) and no render unless something actually changed —
// and not even then over an input the user is typing in (the rewrite in render would steal the focus
// and the caret).
const SRV_POLL_MS = 30000;
async function refreshServersQuiet() {
  // Skips only while a CURRENT request (a Check, the PLAY pre-check, another poll) owns S.srv.
  if (!S.serverConfigured || (srvPollSeq && srvSeqCurrent(srvPollSeq))) return;
  const seq = ++srvSeq; srvPollSeq = seq;
  const target = srvTarget();
  // The signup policy and the player-commands flag count as a change too: they live only in the
  // public snapshot, and a new policy that landed without a redraw left the account card offering the
  // old choices (accountCreate refuses a stale form now, but the card should show the new one). The
  // admin /status carries the whole policy: the admin card's player-route fallback (agent < 3.5) reads it.
  const before = JSON.stringify([S.srv.state, S.srv.versions, S.srv.info && S.srv.info.busy,
    S.pub.status && [S.pub.status.signup, S.pub.status.playerCommands], S.srv.info && S.srv.info.policy]);
  try {
    const st = await rpc('server.status');
    if (!srvSeqCurrent(seq) || srvTarget() !== target) return;
    ingestStatus(st);
    S.srv.state = 'ok'; S.srv.error = '';
  } catch (e) {
    if (!srvSeqCurrent(seq) || srvTarget() !== target) return;
    S.srv.state = 'error'; S.srv.error = e.message; S.srv.versions = {}; S.srv.info = null; // S.pub.status kept, see checkServers
  } finally {
    if (srvPollSeq === seq) srvPollSeq = 0;
  }
  const after = JSON.stringify([S.srv.state, S.srv.versions, S.srv.info && S.srv.info.busy,
    S.pub.status && [S.pub.status.signup, S.pub.status.playerCommands], S.srv.info && S.srv.info.policy]);
  if (after !== before && !typingNow()) render();
}

// One operation at a time: the agent refuses the second one with 409, and the buttons are blocked
// anyway while one runs — this catches the double press before the first render. The result does
// not come from the rpc (which answers immediately) but from the server.log / server.done /
// server.error events.
async function runServerJob(kind, version, extra) {
  if (jobRunning()) return showToast(t('toast.jobInProgress'), 'bad');
  if (!S.serverConfigured) return showToast(t('toast.serverNotConfigured'), 'bad');
  // target / host: the server this job runs on (see jobHere). purge: asked for — a Disable job's
  // result says `purged: false` both when it was not asked and when it failed. progress: the Prepare
  // choice a setup / provision job carries (its title says so — jobTitleOf). enabled: which way a
  // hotpatch job goes (true = Enable / Fetch missing, false = Disable) — the card's State line says
  // so from the click on, while the agent still answers with the state from before the run.
  S.srvJob = { kind, version, target: srvTarget(), host: S.serverAddr.host, purge: !!(extra && extra.purge === true),
    progress: extra && typeof extra.progress === 'string' ? extra.progress : '',
    enabled: extra && typeof extra.enabled === 'boolean' ? extra.enabled : null, lines: [], running: true, error: '' };
  render();
  try {
    await rpc('server.' + kind, Object.assign({ version }, extra || {}));
    // Accepted and running: re-read the hotpatch card NOW rather than leaving it as it was until the
    // next tick — a voice run saves the new selection (the rows' "Selected") in its first seconds.
    if (kind === 'hotpatch' || kind === 'hotpatch.voice') refreshHotpatchQuiet();
  }
  catch (e) {
    S.srvJob.running = false; S.srvJob.error = e.message;
    srvLine(t('server.log.error', { error: e.message }));
    showToast(e.message, 'bad');
    render();
  }
}

// The events re-create it if the user closed the log in the meantime: an operation running on the
// server must stay visible until it ends. (The events carry no server, so a re-created job is taken
// for the current one's; the job runServerJob made keeps the tag of the server it was started on.)
// A few rpcs are named apart from the kind their events carry (SRVJOB_EVENT_KIND): that job is the
// same one, renamed — it keeps its target and its console instead of being re-created under the
// server on screen when its first event lands.
const SRVJOB_EVENT_KIND = { 'secrets.set': 'secrets', 'templates.ensure': 'templates' };
function srvJobFor(d) {
  const kind = (d && d.kind) || '', version = (d && d.version) || '';
  if (S.srvJob && S.srvJob.running && S.srvJob.version === version && SRVJOB_EVENT_KIND[S.srvJob.kind] === kind) S.srvJob.kind = kind;
  if (!S.srvJob || S.srvJob.kind !== kind || S.srvJob.version !== version)
    S.srvJob = { kind, version, target: srvTarget(), host: S.serverAddr.host, purge: false, progress: '', enabled: null, lines: [], running: true, error: '' };
  return S.srvJob;
}
// The agent's own result of a finished job. server.done carries the job's last snapshot ({state,
// result, …}); an agent older than 2.0 answered with the result itself.
function jobResultOf(d) {
  const r = d && d.result;
  if (!r || typeof r !== 'object') return null;
  return r.result && typeof r.result === 'object' ? r.result : r;
}

// A bootstrap spits out thousands of lines; keep the tail, like the commands console.
const SRVLOG_MAX = 400;
function srvLine(text) {
  const s = String(text == null ? '' : text);
  if (!S.srvJob) return s;
  S.srvJob.lines.push(s);
  if (S.srvJob.lines.length > SRVLOG_MAX) S.srvJob.lines.shift();
  return s;
}

// A docker image pull sends hundreds of lines per second: a render() per line would rewrite all of
// #app. The new line is appended straight into the console, with the same scroll rule as
// restoreLogScroll — follow the new line only if the user has not scrolled up through the log.
function pushSrvLine(el, text) {
  const pinned = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
  el.insertAdjacentHTML('beforeend', srvLogLine(text));
  while (el.childElementCount > SRVLOG_MAX) el.removeChild(el.firstElementChild);
  if (pinned) el.scrollTop = el.scrollHeight;
}

// The server address as the user would type it: the host, plus the game port only when it was typed
// explicitly (a TXT or default port is re-resolved at every save, never pinned by the text).
function addrText() {
  const h = S.serverAddr.host || '';
  if (!h || S.serverAddr.source !== 'explicit') return h;
  return (h.includes(':') ? `[${h}]` : h) + ':' + S.serverAddr.port;
}
const addrShown = () => (S.addrEdit != null ? S.addrEdit : addrText());
// Settings and the install wizard save a typed address through the start screen's own path
// (server.setAddress: parse, TXT ports, agent port) and only when it differs from the saved one.
// settings.save used to write the host raw — a second host writer that kept the previous server's
// ports — and no longer reads it. Throws the backend's badAddress text. true = the address changed.
async function saveAddressEdit() {
  if (S.addrEdit == null) return false;
  const typed = S.addrEdit.trim();
  if (typed === addrText()) { S.addrEdit = null; return false; }
  await rpc('server.setAddress', { address: typed });
  S.addrEdit = null;
  return true;
}

// Returns true when the status target moved (and a new status epoch was started).
function applyInit(d) {
  if (!d) return false;
  const prevTarget = srvTarget(), prevHost = S.serverAddr.host;
  S.settings = Object.assign(S.settings, d.settings || {});
  S.versions = d.versions || [];
  S.installed = d.installed || [];
  S.selectedVersionId = d.selectedVersionId || (S.versions[0] && S.versions[0].id);
  S.serverConfigured = !!d.serverConfigured;
  S.runAtStartup = !!d.runAtStartup;
  S.os = d.os || S.os;
  S.fiddler = d.fiddler || S.fiddler;
  // Modes / start screen facts (always refreshed: they are backend truth, not form state).
  if (typeof d.mode === 'string') S.mode = d.mode;
  if (d.defaults) S.defaults = { servers: d.defaults.servers || [], mode: d.defaults.mode || '' };
  if (d.serverAddr) S.serverAddr = Object.assign(S.serverAddr, d.serverAddr);
  if (d.admin) { S.admin.hasToken = !!d.admin.hasToken; S.admin.tokenScope = d.admin.tokenScope || ''; S.admin.mode = d.admin.mode === 'ssh' ? 'ssh' : 'direct'; }
  if (typeof d.isWindows === 'boolean') S.isWindows = d.isWindows;
  // The agent installed on this PC ({installed, host, port, autostart} | null off Windows): every
  // reply that carries the init state refreshes it — an install, an uninstall, a mode change — so
  // the start screen never reads it separately.
  if (d.localAgent !== undefined) S.localAgent.init = d.localAgent;
  if (typeof d.loginMuted === 'boolean' && !S.login._mutedFromStorage) S.login.muted = d.loginMuted;
  if (typeof d.loginAnimOff === 'boolean' && !S.login._animFromStorage) S.login.animOff = d.loginAnimOff;
  if (typeof d.windowHidden === 'boolean') S.login.hidden = d.windowHidden;
  S.accounts = d.accounts || {};
  S.accountLists = d.accountLists && typeof d.accountLists === 'object' ? d.accountLists : {};
  // The last answer of the CURRENT server on the pre-made login, as the backend persisted it (every
  // init-state reply carries it for the server it is for) — the first render's word before a status read.
  S.serverAccounts = d.serverAccounts && typeof d.serverAccounts === 'object' ? d.serverAccounts : {};
  if (Array.isArray(d.voiceLanguages)) S.voiceLanguages = d.voiceLanguages.map(String);
  if (!S.wiz.versionId) S.wiz.versionId = S.selectedVersionId;
  // The wizard's source STARTS from the saved preference. Without this it stays on 'cdn' (the initial
  // value in S) after every app start, and startInstall writes it back over the setting — so a plain
  // walk through the steps silently reset the chosen source. Worse now: the backend compares the
  // preference with the one noted next to the partial file, and a "change" the user did not make
  // THROWS AWAY their partial of tens of GB. What they already chose in the current session is not
  // overwritten ('local' included).
  if (!S.wizSourceTouched && (S.settings.source === 'drive' || S.settings.source === 'cdn'
      || allMirrors().some((m) => m.id === S.settings.source))) S.wiz.source = S.settings.source;
  // The backend may have changed the selected version under us (install.done); refetch the
  // catalogue if the user is looking at it. loadGameData no-ops when it is already current.
  if (S.screen === 'commands') loadGameData();
  // An address typed in Settings / the wizard and not saved belongs to the previous server once the
  // saved one moved under it (start screen, an agent install).
  if (S.serverAddr.host !== prevHost) S.addrEdit = null;
  // Every path that can move the status target lands here (start screen, Settings, wizard, agent
  // installs, mode changes) — one place, so none can be missed.
  if (srvBooted && srvTarget() !== prevTarget) { srvRetarget(); return true; }
  return false;
}

// ─────────────────────────── events ───────────────────────────
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-act]');
  if (!t) return;
  const act = t.getAttribute('data-act');
  // While installing / launching the game / asking for confirmation, the overlay is MODAL: only
  // its own controls and the window buttons work. The overlay only blocks pointer hit-testing —
  // Tab+Enter still reaches covered buttons, and Enter on a focused <button> fires a synthesized
  // click that lands here.
  // toast.close always passes: the toast is drawn OVER the overlay (z-index 40 > 20), so a button
  // that is visible and does not react would simply be broken for the user.
  // S.agentInstall / S.remove draw the same modal overlay, so they belong in the same list — without
  // them a Tab+Enter still reached the buttons behind a running deploy.
  if ((S.installing || S.playing || S.confirm || S.notice || S.agentInstall || S.remove || S.agentCfg.reloc) && !t.closest('.overlay')
    && act !== 'win.min' && act !== 'win.close' && act !== 'toast.close') return;
  const arg = t.getAttribute('data-arg');
  if (ACTIONS[act]) { e.preventDefault(); ACTIONS[act](arg); }
});
document.addEventListener('input', (e) => {
  // The per-version signup limit cannot use data-model: setModel splits the path on '.', and the key is
  // a version id ("1.6"), so "…versions.1.6.maxPerDay" walked into undefined and threw on every keystroke
  // — the typed number never reached the policy that gets saved.
  const pm = e.target.closest('[data-policy-max]');
  if (pm) {
    const d = S.policy && S.policy.data; if (!d) return;
    d.signup = d.signup || { enabled: false, versions: {} };
    d.signup.versions = d.signup.versions || {};
    const id = pm.getAttribute('data-arg');
    const pv = d.signup.versions[id] = d.signup.versions[id] || { templates: ['fresh'], maxPerDay: 50 };
    const n = parseInt(pm.value, 10);
    if (!isNaN(n)) pv.maxPerDay = n;
    return;
  }
  // Accounts one launcher may create on the version (agent 3.7 maxPerPlayer) — drawn only when the agent
  // reports the field, so an older agent is never sent a key it would ignore.
  const pp = e.target.closest('[data-policy-maxplayer]');
  if (pp) {
    const d = S.policy && S.policy.data; if (!d || !d.signup || !d.signup.versions) return;
    const pv = d.signup.versions[pp.getAttribute('data-arg')];
    const n = parseInt(pp.value, 10);
    if (pv && !isNaN(n)) pv.maxPerPlayer = n;
    return;
  }
  const t = e.target.closest('[data-model]');
  if (!t) return;
  const path = t.getAttribute('data-model');
  setModel(path, t.value);
  // The first keystroke in an account form pins the version of the card that was DRAWN — no render.
  // Until then the card follows the status poll (acctVersionCur / acctNewCur), and a poll that saw the
  // version down for a moment moved the form, and the progress choice with it, to another version.
  if ((path === 'account.name' || path === 'account.password') && !S.account.version && S.account.shown) S.account.version = S.account.shown.split('|')[0];
  if ((path === 'acctNew.name' || path === 'acctNew.password') && !S.acctNew.version && S.acctNew.shownVersion) S.acctNew.version = S.acctNew.shownVersion;
  if (t.hasAttribute('data-filter')) filterPicker(t.value); // deliberately NOT a render
  if (path.startsWith('login.')) syncLoginCta();           // same: no render, just the button state
  if (path.startsWith('agentCfg.form.')) syncAgentCfgSave(); // same: the Save button and its count
  if (path === 'agentCfg.reloc.dir') relocCheckSoon();      // the agent's dry run, debounced — no render
  // A Test result belongs to the address it tested: editing the address drops it — the line only, no
  // render (that would take the caret).
  if (path === 'login.address' && S.login.test) {
    S.login.test = null;
    const line = document.querySelector('.lg-status');
    if (line) line.remove();
  }
});
// Enter in a start-screen field = the CTA (the address alone is enough — Test is optional).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || S.screen !== 'login') return;
  const t = e.target.closest('input[data-model^="login."]');
  if (!t) return;
  e.preventDefault();
  if (loginCanEnter()) ACTIONS.loginEnter();
});
// The language <select> in Settings (see languageLine).
document.addEventListener('change', (e) => {
  const el = e.target.closest('select[data-lang]');
  if (!el) return;
  ACTIONS.setLanguage(el.value);
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (S.confirm) { S.confirm = null; render(); return; } // Esc on "are you sure?" = Cancel
  if (S.notice) { S.notice = null; render(); return; }   // Esc on the notice = Understood
  if (S.agentCfg.reloc) { clearTimeout(relocTimer); S.agentCfg.reloc = null; render(); return; } // = Cancel
  if (S.picker && !S.installing) { S.picker = null; render(); }
});
// <img> load failures do not bubble, so this has to be a capture-phase listener. It covers the
// character stage and the picker tiles alike: a splash file missing from the build shows the
// element-tinted initial instead of a broken-image box.
document.addEventListener('error', (e) => {
  const img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  const wrap = img.closest('[data-artwrap]');
  if (wrap) wrap.classList.add('noart');
}, true);
document.addEventListener('mousedown', (e) => {
  const bar = e.target.closest('[data-drag]');
  if (bar && !e.target.closest('button')) rpc('window.drag');
});

// backend events
// A progress event does NOT redraw the overlay if nothing structural changed in it: the full rewrite
// of #app restarted the ring's rotation and the waves from zero at every percent. The text is set in
// place, and the percent is carried by the tween (syncProgress → rAF).
on('install.progress', (d) => {
  S.installFraction = d.fraction || 0; S.installMsg = d.message || ''; S.installPhase = d.phase;
  if (!S.installing) return;
  if (patchInstallLive()) syncProgress(); else render();
});
on('install.done', (d) => {
  if (d && d.versionId) S.activeInstallId = d.versionId; // backend's id is authoritative
  // A voices job: the languages the backend actually added (d.voices) name the outcome; an empty list
  // (every one was already on disk) keeps the requested names — they ARE part of the version now.
  if (S.activeInstallVoices && d && Array.isArray(d.voices) && d.voices.length) S.activeInstallVoices = d.voices.map(String);
  const voices = S.activeInstallVoices;
  S.installDone = true;
  S.installPaused = false; S.installStopIntent = null; // a stop that raced the finish lost — show done
  if (d && d.state) applyInit(d.state);
  if (S.screen !== 'install') showToast(voices ? t('toast.voicesAdded', { langs: voices.join(', '), id: S.activeInstallId || '' }) : t('toast.installed', { id: S.activeInstallId || '' }), 'ok');
  render();
});
on('install.error', (d) => {
  S.installError = (d && d.message) || t('common.unknownError');
  S.installPaused = false; S.installStopIntent = null; // the error overlay must never hide behind "paused"
  if (S.screen !== 'install') showToast(t('toast.installFailed', { error: S.installError }), 'bad');
  render();
});
on('install.cancelled', () => {
  const paused = S.installStopIntent === 'pause';
  S.installStopIntent = null;
  if (paused) S.installPaused = true; // overlay flips to the paused state, Resume/Give up take over
  else {
    const wasLocal = !!S.activeInstallLocal, wasVoices = !!S.activeInstallVoices;
    clearActiveInstall();
    S.installPaused = false; S.installPhase = '';
    showToast(wasVoices ? t('toast.voicesCancelled') : wasLocal ? t('toast.importCancelled') : t('toast.installCancelled'), 'ok');
  }
  render();
});
// The !S.picker guard: the log sits behind the modal anyway, and a streaming play session must not
// rebuild the tile grid or steal the caret while the user is typing in the picker's search box.
// While the "Starting the game" splash is up, every line also becomes its status.
on('play.log', (d) => {
  addLog('sys', d.text);
  if (S.playing) { S.playing.msg = d.text; render(); return; }
  if (S.screen === 'commands' && !S.picker) render();
});
// The client has actually appeared — the splash did its job; from here the player looks at the game.
// All three bypass the PRE-check splash (check): the events come from a game session, and the
// pre-check is not one — a stray play.done from the session that just ended would otherwise close
// its splash and the launch would look cancelled out of nowhere.
on('play.appeared', (d) => {
  if (!S.playing || S.playing.check) return;
  S.playing = null;
  // d.enhancements: the F1 menu really is in the process (not merely wanted) — the backend says so.
  showToast(d && d.enhancements ? t('toast.gameRunningEe') : t('toast.gameRunning'), 'ok');
  render();
});
on('play.done', () => { if (!(S.playing && S.playing.check)) S.playing = null; showToast(t('toast.sessionEnded'), 'ok'); render(); });
on('play.error', (d) => { if (!(S.playing && S.playing.check)) S.playing = null; showToast(t('toast.launchError', { error: (d && d.message) }), 'bad'); render(); });
// Relic closed the game because the user closed Fiddler — a modal window, not a toast: the message
// explaining why the game vanished from the screen must not erase itself after a few seconds. The
// backend has already pulled the app window out of the tray so the dialog is actually seen.
on('play.fiddlerClosed', (d) => {
  if (!(S.playing && S.playing.check)) S.playing = null;
  S.notice = {
    title: t('play.fiddlerClosed.title'),
    body: (d && d.message) || t('play.fiddlerClosed.body'),
  };
  render();
});
on('command.log', (d) => { addLog(d.type, d.text); if (S.screen === 'commands' && !S.picker) render(); const el = document.getElementById('cmdlog'); if (el) el.scrollTop = el.scrollHeight; });
on('server.log', (d) => {
  const prev = S.srvJob;
  const j = srvJobFor(d);
  // A new job, or a still-empty console (showing the placeholder), asks for one full draw.
  const fresh = j !== prev || j.lines.length === 0;
  const text = srvLine(d && d.text);
  // A folder move that turns into a copy says so in every case — the planned cross-drive copy, or a
  // rename the system refused after all (a bind mount of the same disk): Stop appears with that line
  // (one full draw), whatever the dialog's check expected, and also on a job re-created from the events.
  const stoppable = j.kind === 'relocate' && j.running && !j.crossDrive && /\bCopying \d+ files\b/.test(text);
  if (stoppable) j.crossDrive = true;
  if (S.screen !== 'server' || S.picker) return;
  const el = fresh || stoppable ? null : document.getElementById('srvlog');
  if (el) pushSrvLine(el, text); else render();
});
on('server.done', (d) => {
  const j = srvJobFor(d); j.running = false;
  // The job is DONE although a purge that was asked for did not delete everything (Windows: a client
  // was being served one of the files): the agent says so in the result only — voice job: the
  // languages in `purgeFailed`; Disable: `purged: false`. There is a single toast, so the warning
  // takes the place of "done" (the job log keeps the agent's own line).
  const res = jobResultOf(d);
  const stuck = j.kind === 'hotpatch.voice' && res && Array.isArray(res.purgeFailed)
    ? res.purgeFailed.filter((n) => typeof n === 'string' && n) : [];
  if (stuck.length) showToast(t('hotpatch.voice.purgeFailed', { langs: stuck.join(', ') }), 'bad');
  else if (j.kind === 'hotpatch' && j.purge && res && res.purged === false) showToast(t('confirm.hotpatch.purgeFailed'), 'bad');
  // An account created from the admin card of THIS server: into its list, with its own toast. One that
  // ran on another server (the address changed meanwhile) has no list to go to (see acctNewMovedDone).
  else if (j.kind === 'accountcreate' && j.target === srvTarget()) acctNewDone(j, res);
  else if (j.kind === 'accountcreate') acctNewMovedDone(j, res);
  // A folder change says where the version lives now ('' = taken off the agent, the files kept). The
  // job is DONE even when the server it stopped did not start again in the new place (restartError —
  // by then the change is made): that is a warning, not a success. restarted:false alone is no failure
  // (the server was not running, or the new folder holds no server — it stays down by design).
  else if (j.kind === 'relocate' && res && typeof res.to === 'string') {
    const noStart = res.to && res.restarted !== true && typeof res.restartError === 'string' && res.restartError.trim() ? res.restartError.trim() : '';
    if (noStart) showToast(t('toast.relocateNotRestarted', { version: j.version, dir: res.to, error: noStart }), 'bad');
    else showToast(res.to ? t('toast.relocateDone', { version: j.version, dir: res.to }) : t('toast.relocateRemoved', { version: j.version }), 'ok');
  }
  else showToast(t('toast.jobDone', { job: jobTitleOf(j), version: j.version }), 'ok');
  srvJobSettled(j, true);
  // A finished stack download: the next queued one (an install that marked both versions).
  if (j.kind === 'fetch') startNextFetch();
});
on('server.error', (d) => {
  const j = srvJobFor(d); j.running = false;
  j.error = (d && d.message) || t('common.unknownError');
  srvLine(t('server.log.error', { error: j.error }));
  // A stopped voice job — or a stopped stack download — ends as an error on the wire: the agent's 409
  // "Stopped on request: …" (kept in the log above). The toast goes by THAT sentence — whoever pressed
  // Stop, this admin or another one — and never by the click: a job that really failed after a Stop
  // click (the disk filled up, the link was lost) must not be reported as a clean stop that "run it
  // again" would continue. A stopped or failed download also drops the rest of the queue: the admin
  // resumes it from the card, one version at a time.
  const stopped = /^Stopped on request\b/.test(j.error);
  if (j.kind === 'fetch') S.fetchQueue = [];
  // A failed account creation keeps the typed name (a taken name is corrected, not retyped).
  if (j.kind === 'accountcreate' && j.target === srvTarget()) S.acctNew.pending = null;
  if (j.kind === 'hotpatch.voice' && stopped) showToast(t('hotpatch.voice.stopped', { version: j.version }), 'ok');
  else if (j.kind === 'fetch' && stopped) showToast(t('toast.fetchStopped', { version: j.version }), 'ok');
  else if (j.kind === 'relocate' && stopped) showToast(t('toast.relocateStopped', { version: j.version }), 'ok');
  else showToast(t('toast.jobFailed', { job: jobTitleOf(j), error: j.error }), 'bad');
  srvJobSettled(j); // a failed enable may still have cached files
});
// After server.done / server.error: the real state comes from the agent, it is not inferred from the
// job's outcome. A job that ran on ANOTHER server (the address was changed meanwhile, see jobHere)
// says nothing about the hotpatch card on screen: only the status is read again, which also brings
// this server's buttons back to life.
function srvJobSettled(j, ok) {
  const here = j.target === srvTarget();
  // Our own hotpatch / voice job: forget what /status said about it, so the read below starts from
  // scratch (a job that goes on without us — the link was lost — is picked up again by that read).
  if (here && (j.kind === 'hotpatch' || j.kind === 'hotpatch.voice')) S.hotpatch.busy = null;
  checkServers(true);
  if (here) refreshHotpatchAfterJob(j.kind, j.version, ok);
  // The jobs that import (or drop) template databases: the account card's choices may have changed. An
  // account creation too: a template_not_ready refusal means the card offered a template the stack no
  // longer has imported — re-read, the card drops it and says so.
  if (here && (j.kind === 'templates' || j.kind === 'setup' || j.kind === 'provision' || j.kind === 'accountcreate')) loadAcctTemplates(j.version);
  // A folder change — done, failed or stopped — rewrote (or kept) the agent's configuration: the
  // settings card's folders, and on this PC the local agent's facts (its stack folders), are read again.
  if (here && j.kind === 'relocate') { loadAgentCfg(); loadLocalAgent(); }
}
// The admin route's result (server.done of an 'accountcreate' job of this server): the session list
// gets the row — the password only when the agent returned one (verification on: typed or generated)
// —, the form clears the name and password for the next one (version and template stay), and a
// "show it as my login" request re-reads the init state, whose accounts the backend has just updated.
// No pending request = the form was reset while the job ran (the admin went to another server and
// came back): what it asked is unknown, and the backend may have filed the login — re-read it too;
// what the form holds now was typed after the reset and stays.
function acctNewDone(j, res) {
  const a = S.acctNew, r = res || {}, pend = a.pending;
  a.pending = null;
  const name = typeof r.name === 'string' && r.name ? r.name : (pend && pend.name) || '';
  const tpl = typeof r.template === 'string' && r.template ? r.template : 'fresh';
  acctNewPush({ version: j.version, name, uid: r.uid, templateLabel: acctNewTplLabel(j.version, tpl),
    password: typeof r.password === 'string' && r.password ? r.password : null, generated: !!r.passwordGenerated });
  // The form is cleared only while it still holds the account just created: the admin may already be
  // typing the next one, which the result must not wipe. "Show it as my login" goes back off either
  // way: it is a choice about THIS account — left on, the next one typed (a friend's) would silently
  // replace the login it has just filed.
  if (pend && a.name.trim() === pend.name) { a.name = ''; a.password = ''; }
  a.remember = false;
  showToast(t('toast.adminAccountCreated', { name, version: j.version }), 'ok');
  if (!pend || pend.remember) rpc('app.init').then((d) => { applyInit(d); if (!typingNow()) render(); }).catch(() => {});
}
// An admin-route create that finished on ANOTHER server (the address or the mode changed while it ran):
// its card and list are gone. A password the server GENERATED exists nowhere else — the agent returns
// it once, in this result — so it goes into a modal window that stays until dismissed; anything else is
// the account's own toast. The password is never put in S.srvJob or a log line.
function acctNewMovedDone(j, res) {
  const r = res || {};
  const name = typeof r.name === 'string' && r.name ? r.name : '—';
  const host = j.host || '—';
  if (r.passwordGenerated && typeof r.password === 'string' && r.password) {
    S.notice = { title: t('server.acctNew.movedTitle', { host }), body: t('server.acctNew.movedBody', { host, version: j.version, name }), pick: r.password };
    render();
  } else showToast(t('toast.adminAccountCreated', { name, version: j.version }), 'ok');
}
// The dev autoinstall sends no voices: the backend's default (English) applies; the request is kept
// like a real one so Resume works.
on('dev.autoinstall', (d) => { if (S.installing) return; S.picker = null; S.wiz.versionId = d.versionId; S.activeInstallId = d.versionId; S.activeInstallLocal = null; S.activeInstallVoices = null; S.activeInstallReq = { type: 'install.start', payload: { versionId: d.versionId } }; S.screen = 'install'; S.installStep = LAST; S.installing = true; S.installDone = false; S.installError = null; S.installPaused = false; S.installStopIntent = null; S.installPhase = ''; S.installFraction = 0; S.installMsg = t('install.preparing'); resetProgress(); render(); rpc('install.start', { versionId: d.versionId }).catch((e) => { clearActiveInstall(); showToast(e.message, 'bad'); }); });

// per-version removal
on('remove.progress', (d) => { if (!S.remove) return; S.remove.msg = d.message || ''; S.remove.fraction = d.fraction || 0; render(); });
on('remove.done', (d) => {
  if (d && d.state) applyInit(d.state);
  if (S.remove) { S.remove.running = false; S.remove.done = true; S.remove.leftovers = (d && d.leftovers) || []; }
  render();
});
on('remove.error', (d) => { if (S.remove) { S.remove.running = false; S.remove.error = (d && d.message) || t('common.unknownError'); } render(); });
// agent installs (remote SSH or this PC) — streamed into the overlay console
on('deploy.log', (d) => {
  const a = S.agentInstall; if (!a) return;
  a.lines.push(String((d && d.text) || '')); if (a.lines.length > 400) a.lines.shift();
  const el = document.getElementById('deploylog');
  if (el) { el.insertAdjacentHTML('beforeend', `<div>${esc(a.lines[a.lines.length - 1])}</div>`); el.scrollTop = el.scrollHeight; } else render();
});
on('deploy.done', (d) => {
  const a = S.agentInstall;
  // Another host / agent port / mode: applyInit already started the new status epoch and its read. The
  // same box (an upgrade) keeps its target, so the reset and the fresh check are done here.
  const moved = !!(d && d.state) && applyInit(d.state);
  if (a) { a.running = false; a.done = true; a.result = d || null; }
  if (!moved) S.srv = { state: 'idle', error: '', versions: {}, info: null };
  S.hotpatch = hotpatchBlank();
  // A new or upgraded agent: another version (3.6 answers /agent/config), another file, other folders.
  agentCfgSeq++; S.agentCfg = agentCfgBlank();
  // The versions the form marked for download (agent 3.4): the agent is up and idle — nothing else
  // can run on a box that was just installed — so the first job starts right away; the summary tells
  // the admin where its log is (deploy.done.fetchStarting). The Linux install sends an empty list when
  // the agent did not answer its health check: the card's own button is the way then.
  S.fetchQueue = Array.isArray(d && d.fetch) ? d.fetch.map(String) : [];
  render(); if (!moved) checkServers(true);
  loadLocalAgent(); loadPolicy(); loadHotpatch(S.selectedVersionId || (S.versions[0] && S.versions[0].id)); loadAgentCfg();
  // A new or upgraded agent (3.5 brings the admin route) — and possibly other stacks: re-read the
  // account card's templates.
  loadAcctTemplatesAll();
  startNextFetch();
});
// The token comes along on a failure too: the installer may already have written it on the box, and it
// is generated in the backend and filtered out of the log — so this is the only place it can be seen.
on('deploy.error', (d) => {
  const a = S.agentInstall;
  if (a) { a.running = false; a.error = (d && d.message) || t('common.unknownError'); a.result = d || null; }
  render();
});
// account creation through the player route (account.create): the player card, or the admin card on
// an agent older than 3.5. The backend echoes the request's origin ('player' | 'admin') and version in
// every event. A create started under ANOTHER target (the server or the mode changed while it ran —
// S.acctPending remembers where it began) only speaks through a toast: the forms on screen are the
// new server's, and its late lines, result or error must never land in them.
const acctMoved = () => !!(S.acctPending && S.acctPending.target !== srvTarget());
const acctFormOf = (d) => (d && d.origin === 'admin' ? S.acctNew : S.account);
// A line goes straight into its console, like server.log / deploy.log: a render() per line rewrote the
// page over the field being typed in (the next account's name) and took the caret. A full draw only
// when the pane is not on screen, and never over a focused field.
const ACCT_LOG_MAX = 60;
on('account.log', (d) => {
  if (acctMoved()) return;
  const ac = acctFormOf(d);
  const text = String((d && d.text) || '');
  ac.lines.push(text); if (ac.lines.length > ACCT_LOG_MAX) ac.lines.shift();
  if (S.screen !== 'server') return;
  const el = document.getElementById(d && d.origin === 'admin' ? 'acctnewlog' : 'acctlog');
  if (el) {
    el.insertAdjacentHTML('beforeend', `<div>${esc(text)}</div>`);
    while (el.childElementCount > ACCT_LOG_MAX) el.removeChild(el.firstElementChild);
    el.scrollTop = el.scrollHeight;
  } else if (!typingNow()) render();
});
on('account.done', (d) => {
  const pend = S.acctPending, moved = acctMoved();
  S.acctPending = null;
  const result = (d && d.result) || {};
  const name = typeof result.name === 'string' && result.name ? result.name : (pend && pend.name) || '';
  const version = (d && d.version) || (pend && pend.version) || '';
  const admin = !!(d && d.origin === 'admin');
  if (!moved && admin) {
    // The admin card's fallback: a row in its session list. The public result never carries a
    // password — the row shows the one typed, which the request kept only under verification.
    const a = S.acctNew;
    a.running = false; a.lines = []; a.error = null;
    const tpl = typeof result.template === 'string' && result.template ? result.template : (pend && pend.template) || 'fresh';
    acctNewPush({ version, name, uid: result.uid, templateLabel: acctNewTplLabel(version, tpl),
      password: pend && pend.origin === 'admin' && pend.password ? pend.password : null, generated: false });
    // As on the admin route (acctNewDone): the name / password only while they are still this
    // account's (the next one may be half typed), "Show it as my login" always.
    if (pend && a.name.trim() === pend.name) { a.name = ''; a.password = ''; }
    a.remember = false;
  } else if (!moved) {
    const ac = S.account; ac.running = false; ac.result = d && d.result; ac.lines = [];
    if (pend && ac.name.trim() === pend.name) { ac.name = ''; ac.password = ''; }
    if (version) delete ac.another[version]; // the new login is the remembered one now
  }
  if (d && d.state) applyInit(d.state);
  showToast(admin ? t('toast.adminAccountCreated', { name, version }) : t('toast.accountCreated', { name }), 'ok');
  render();
});
on('account.error', (d) => {
  const pend = S.acctPending, moved = acctMoved();
  S.acctPending = null;
  const msg = (d && d.message) || t('common.unknownError');
  if (moved) return showToast(t('toast.jobFailed', { job: t('server.job.accountcreate'), error: msg }), 'bad');
  const ac = acctFormOf(d); ac.running = false; ac.error = msg; render();
  // The admin card's fallback, as after an admin-route job (srvJobSettled): a template that is not
  // imported any more is dropped from the card by a fresh read.
  if (d && d.origin === 'admin') loadAcctTemplates(d.version || (pend && pend.version));
});
// The window went to the tray / came back: pause or resume the start-screen media. Nothing to
// redraw — the DOM is untouched, only the play state follows.
on('window.visibility', (d) => { S.login.hidden = !!(d && d.hidden); LOGIN_MEDIA.sync(); });

// ─────────────────────────── boot ───────────────────────────
// No first render() before app.init AND the language have both resolved: the dictionary must be
// loaded before anything is drawn, and the language to load comes from the saved settings.
(async function boot() {
  let init = null;
  try { init = await rpc('app.init'); } catch (e) { /* backend not ready */ }
  try { await I18N.init((init && init.settings && init.settings.language) || 'en'); } catch (e) { /* English stays */ }
  S.log = [{ t: 'sys', m: t('cmd.console.ready') }];
  // Music preference: localStorage first (instant, survives a reset of state.json), backend second.
  try { const m = localStorage.getItem('relic-login-muted'); if (m === '1' || m === '0') { S.login.muted = m === '1'; S.login._mutedFromStorage = true; } } catch (e) { /* no localStorage */ }
  try { const a = localStorage.getItem('relic-login-anim'); if (a === '1' || a === '0') { S.login.animOff = a === '0'; S.login._animFromStorage = true; } } catch (e) { /* no localStorage */ }
  applyInit(init);
  // Start screen: a returning user (mode chosen + server known; admin also needs a token) lands on
  // the pre-filled panel with a single CTA; a fresh install sees "Which Way?".
  const configured = S.mode === 'player' ? !!S.serverAddr.host : S.mode === 'admin' ? !!S.serverAddr.host && S.admin.hasToken : false;
  if (S.mode && configured) { S.login.panel = S.mode; S.login.address = S.serverAddr.host; }
  else if (S.defaults.mode) { S.login.panel = S.defaults.mode; S.login.address = S.serverAddr.host || ((S.defaults.servers[0] || {}).host) || ''; }
  const dev = (location.hash || '').slice(1);
  if (dev) S.screen = dev; // dev aid: start on a given screen
  render();
  if (S.screen === 'commands') loadGameData();
  // The "Server: LIVE/STOPPED" badge in the library: one read at start, then every 30 seconds. The
  // status is a pure read (GET /status on the agent) and runs in parallel with any job — but not more
  // often: every call means a `docker compose ls` on the box.
  srvBooted = true;
  refreshServersQuiet();
  setInterval(refreshServersQuiet, SRV_POLL_MS);
  // The "Official in-version fixes (client hotpatch)" card while it is being filled: a much shorter
  // interval than the status poll, but only while a hotpatch job of the shown version is downloading
  // on this server and the admin is looking at that page (refreshHotpatchQuiet returns at once otherwise).
  setInterval(refreshHotpatchQuiet, HOTPATCH_POLL_MS);
  // Check at every start, not only when Settings is opened: if a session ended without restoring the
  // live profile, the OFFICIAL client would launch on private-server data — the user has to learn
  // that BEFORE they open it, not after.
  await loadProfileStatus();
  if (S.profile && !S.profile.liveActive) {
    showToast(S.profile.gameRunning
      ? t('toast.liveNotActiveRunning')
      : t('toast.liveNotActive'), 'bad');
  }
})();
