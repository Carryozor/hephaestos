// F1 (XSS handlers inline) + F2 (garde anti-chevauchement fetchServers).
// Execute avec : node tests/js/inline-handlers.js   (invoque aussi par tests/test_ui.py)
//
// F1 : esc() protege le contexte HTML, mais le navigateur DECODE les entites d'un attribut
// onclick avant de parser le JS : '${esc(x)}' laisse x sortir de la chaine. La parade est
// jsArg(x) = esc(JSON.stringify(x)). On verifie ici, en rejouant le decodage navigateur
// puis l'evaluation avec des fonctions espionnes, que l'argument recu est EXACTEMENT la
// valeur d'origine et qu'aucun autre appel (alert, ...) n'est execute.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const APP_PATH = path.join(__dirname, "../../app/static/app.js");
const appJs = fs.readFileSync(APP_PATH, "utf8");

function makeElement(tag) {
  return {
    tagName: tag, className: "", innerHTML: "", textContent: "", id: "",
    style: {}, dataset: {}, disabled: false, value: "", children: [],
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, querySelector() { return makeElement("div"); },
    querySelectorAll() { return []; }, setAttribute() {}, remove() {}, focus() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
}

function loadApp(fetchImpl) {
  const byId = new Map();
  const doc = {
    getElementById: (id) => { if (!byId.has(id)) byId.set(id, makeElement("div")); return byId.get(id); },
    createElement: (t) => makeElement(t),
    addEventListener() {}, querySelectorAll: () => [], body: makeElement("body"),
  };
  const sandbox = {
    document: doc, window: { location: { href: "" } },
    fetch: fetchImpl || (async () => ({ ok: true, status: 200, json: async () => ({ servers: [] }) })),
    setInterval: () => 0, setTimeout: () => 0, console, URLSearchParams, atob, btoa,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(appJs, sandbox, { filename: "app.js" });
  return { sandbox, byId, run: (code) => vm.runInContext(code, sandbox) };
}

// ---------- decodage navigateur + evaluation espionne ----------
const decodeEntities = (s) => s
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<")
  .replace(/&gt;/g, ">").replace(/&amp;/g, "&");

function extractHandlers(html) {
  // un attribut HTML se termine au premier " brut : meme regle que le navigateur
  return [...html.matchAll(/\son(?:click|change|input)="([^"]*)"/g)].map(m => decodeEntities(m[1]))
    .filter(code => !code.includes("this."));  // handlers constants bases sur `this` (aucune donnee interpolee)
}

function evalHandler(code) {
  const calls = [];
  const proxy = new Proxy({}, {
    has: () => true,
    get: (_t, k) => (k === Symbol.unscopables ? undefined : (...args) => { calls.push([String(k), args]); }),
  });
  new Function("__p", `with (__p) { ${code} }`)(proxy);
  return calls;
}

// Verifie : chaque handler produit exactement UN appel (aucun code injecte) ; les appels
// attendus sont tous presents avec des arguments strictement egaux.
function assertHandlers(label, html, expected) {
  const handlers = extractHandlers(html);
  const all = [];
  for (const h of handlers) {
    let calls;
    try { calls = evalHandler(h); } catch (e) { throw new Error(`${label}: handler non evaluable (${e.message}) : ${h}`); }
    if (calls.length !== 1) throw new Error(`${label}: ${calls.length} appels (code injecte ?) : ${h}`);
    all.push(calls[0]);
  }
  for (const [fn, ...args] of expected) {
    const hit = all.find(([f, a]) => f === fn && JSON.stringify(a) === JSON.stringify(args));
    if (!hit) {
      throw new Error(`${label}: appel attendu ${fn}(${JSON.stringify(args)}) absent ; recus = ${JSON.stringify(all)}`);
    }
  }
}

const HOSTILE = [
  "a'b", 'a"b', "</script><img src=x onerror=alert(1)>", "x');alert(1);//",
  'x");alert(1);//', "&quot;&#39;&amp;", "line1\nline2\r\nline3", "back\\slash'", "  é😀",
];

function testHostileViews() {
  let views = 0;
  for (const bad of HOSTILE) {
    const { sandbox, byId, run } = loadApp();
    sandbox.__bad = bad;
    run(`currentUser = { username: "boss", role: "admin", servers: [] };`);
    const tag = JSON.stringify(bad).slice(0, 24);

    // 1. carte serveur : bandes mods / bepinex, annulation d'ordre, auto-reboot
    const card = run(`renderCard({ name: __bad, display_name: "X", server_appid: 1, public_buildid: "1",
      state: { process_up: true, players: null, buildid: "1", last_seen: null, process_started_at: null },
      pending_orders: ["update"], order_queue: [{ id: __bad + "-id", type: "update", status: "pending", position: 1, total: 1 }],
      workshop_appid: 1, mods: [{ workshop_id: __bad, title: "t", installed: true, installed_at: null }],
      bepinex_mods: [{ slug: __bad, title: "t", installed_version: "1", latest_version: "1", update_available: false }],
      auto_restart_on_crash: false })`);
    assertHandlers(`carte ${tag}`, card.innerHTML, [
      ["toggleMods", bad], ["toggleBepinex", bad], ["cancelOrder", bad, bad + "-id"],
      ["toggleAutoRestartOnCrash", bad, true],
    ]);
    // players cliquable : nom de serveur reserve, mais le helper doit rester correct
    const pal = run(`renderCard({ name: "palworld", display_name: "P", state: null, pending_orders: [], order_queue: [] })`);
    assertHandlers("carte palworld", pal.innerHTML, [["togglePlayers", "palworld"]]);

    // 2. detail : sauvegardes + formulaire de config
    run(`configCache[__bad] = { display_name: "x", rcon: {}, exe_candidates: [] };
         configEditingFor.add(__bad);`);
    const detail = run(`renderServerDetail({ rcon_info: null, uptime_seconds: null, process: { cpu_percent: null, mem_mb: null }, players: [], playtime_totals: [], connection_log: [], order_history: [], save_backups: [{ file: __bad, size_mb: 1, created: null }] }, __bad)`);
    assertHandlers(`detail ${tag}`, detail, [
      ["restoreSave", bad, bad], ["backupNow", bad], ["saveServerConfig", bad], ["cancelServerConfig", bad],
    ]);
    run(`configEditingFor.delete(__bad)`);
    assertHandlers(`config lecture ${tag}`, run(`(configEditingFor.delete(__bad), renderDetailConfig({ name: __bad, display_name: "x" }))`), []);
    run(`renderConfigBodyIfPresent(__bad)`);
    assertHandlers(`config slot ${tag}`, byId.get(`config-modify-slot-${bad}`) ? byId.get(`config-modify-slot-${bad}`).innerHTML : "", []);

    // 3. mods Workshop + BepInEx (vue detail)
    const mods = run(`renderDetailMods({ name: __bad, workshop_appid: 1, mods_restart_required: false,
      mods: [{ workshop_id: __bad, title: "t", installed: true, installed_at: null, update_available: true }],
      bepinex_mods: [{ slug: __bad, title: "t", installed_version: "1", latest_version: "2", update_available: true },
                     { slug: __bad + "2", title: "u", installed_version: "1", latest_version: "2", update_available: true }] });
      detailMods.innerHTML`);
    assertHandlers(`mods ${tag}`, mods, [
      ["updateMod", bad, bad], ["removeMod", bad, bad], ["updateAllMods", bad], ["addMod", bad],
      ["toggleWorkshopBrowser", bad], ["updateBepInExMods", bad, [bad]], ["updateBepInExMods", bad, null],
      ["checkBepInExUpdates", bad],
    ]);

    // 4. fichiers
    const files = run(`renderFilesSection({ name: __bad, files_listing: { [__bad]: ["a.ini"] }, file_read: null })`);
    assertHandlers(`fichiers ${tag}`, files, [["listFiles", bad, bad]]);
    assertHandlers(`fichiers vide ${tag}`, run(`renderFilesSection({ name: __bad, files_listing: {}, file_read: null })`),
      [["listFiles", bad, "install"]]);
    run(`openFileEditor = { name: __bad, root: "install", path: "a.ini", sha256: "s" }`);
    assertHandlers(`editeur ${tag}`, run(`renderFilesSection({ name: __bad, files_listing: {}, file_read: null })`),
      [["previewFileDiff", bad], ["saveFile", bad, false], ["saveFile", bad, true]]);

    // 5. Workshop browser (grille + detail)
    run(`workshopBrowserState[__bad] = { page: 1, sort: "trend", query: "", results: [{ workshop_id: __bad, title: "t", subscriptions: 1, description: "d" }] };
         renderWorkshopResultsGrid(__bad, workshopBrowserState[__bad].results);`);
    assertHandlers(`workshop grille ${tag}`, byId.get(`workshop-grid-${bad}`).innerHTML, [["selectWorkshopResult", bad, bad]]);
    run(`selectWorkshopResult(__bad, __bad)`);
    assertHandlers(`workshop detail ${tag}`, byId.get(`workshop-detail-${bad}`).innerHTML, [["installWorkshopResult", bad, bad]]);

    // 6. jeux decouverts / adoption / finalisation
    assertHandlers(`decouverts ${tag}`, run(`renderDiscoveredGames([{ appid: 5, name: __bad, buildid: "1" }])`), []);
    assertHandlers(`finalize ${tag}`, run(`finalizeFormHtml(__bad, { display_name: "x", exe_candidates: [] })`),
      [["submitFinalize", bad]]);

    // 7. comptes (username hostile)
    run(`accountsCache = [{ username: __bad, role: "user", servers: [] }, { username: "boss", role: "admin", servers: [] }];
         accountEditorFor = __bad; accountPasswordFor = __bad; renderAccountsList();`);
    assertHandlers(`comptes ${tag}`, byId.get("accountsList").innerHTML, [
      ["saveAccountAccess", bad], ["saveAccountPassword", bad], ["toggleAccountEditor", bad],
      ["togglePasswordEditor", bad], ["deleteAccount", bad],
    ]);
    views += 1;
  }
  return views;
}

// adoption : le slug suggere est derive du nom de jeu (donnee agent)
function testAdoptSlug() {
  const { run } = loadApp();
  const html = run(`renderDiscoveredGames([{ appid: 5, name: "My'Game\\");alert(1);//", buildid: "1" }])`);
  assertHandlers("adopt slug", html, [["openAdopt", 5, "my-game-alert-1"]]);
}

// ---------- F1 non-regression : le source ne doit plus interpoler sans jsArg ----------
const SAFE_INTERPOLATIONS = new Set([
  "!on",                          // booleen calcule (toggleAutoRestartOnCrash)
  "parseInt(g.appid, 10) || 0",   // entier force
  "parseInt(appid, 10) || 0",     // entier force
  "needsUpdate",                  // dans le libelle, pas dans un handler (garde-fou, voir ci-dessous)
]);

function testSourceGuard() {
  const total = (appJs.match(/\bon[a-z]+="/g) || []).length;
  const attrRe = /\bon(click|change|input|submit)="((?:\$\{[^}]*\}|[^"])*)"/g;
  const offenders = [];
  let attrs = 0, withInterp = 0, interpFixed = 0, interpSafe = 0;
  for (const m of appJs.matchAll(attrRe)) {
    attrs += 1;
    const interps = [...m[2].matchAll(/\$\{((?:[^{}]|\{[^}]*\})*)\}/g)].map(x => x[1]);
    if (interps.length) withInterp += 1;
    for (const i of interps) {
      if (/^jsArg\(.*\)$/s.test(i)) interpFixed += 1;
      else if (SAFE_INTERPOLATIONS.has(i) && i !== "needsUpdate") interpSafe += 1;
      else offenders.push(`${m[1]}="${m[2]}" -> \${${i}}`);
    }
    if (/'\$\{|\$\{[^}]*\}'/.test(m[2])) offenders.push(`interpolation entre quotes : ${m[2]}`);
  }
  assert.strictEqual(attrs, total, `regex a rate des attributs on*= (${attrs} sur ${total})`);
  assert.ok(attrs >= 30, `trop peu d'attributs analyses : ${attrs}`);
  console.log(`GREP handlers inline : ${attrs} attributs, ${withInterp} avec interpolation, ` +
              `${interpFixed} interpolations jsArg, ${interpSafe} valeurs sures listees, ${offenders.length} fautives`);
  assert.deepStrictEqual(offenders, [], "interpolations non protegees par jsArg dans un handler inline :\n" + offenders.join("\n"));
}

function testJsArgHelper() {
  const { run, sandbox } = loadApp();
  assert.strictEqual(typeof sandbox.jsArg, "function", "jsArg absent");
  assert.strictEqual(run(`jsArg("a'b")`), '&quot;a&#39;b&quot;');
  assert.strictEqual(run(`jsArg(null)`), "null");
  assert.strictEqual(run(`jsArg(["x"])`), "[&quot;x&quot;]");
}

// ---------- F2 : fetchServers anti-chevauchement ----------
const tick = () => new Promise(r => setImmediate(r));
function deferred() { let res, rej; const p = new Promise((a, b) => { res = a; rej = b; }); return { p, res, rej }; }
const okResp = (marker) => ({ ok: true, status: 200, json: async () => ({ servers: [], agent_version: marker }) });

async function testFetchOverlap() {
  const { sandbox, run } = loadApp();
  await tick(); await tick();
  const first = deferred(), second = deferred();
  const queue = [first, second];
  sandbox.fetch = () => queue.shift().p;
  const p1 = run("fetchServers()");
  const p2 = run("fetchServers()");
  second.res(okResp("second"));
  await p2;
  assert.strictEqual(run("agentVersion"), "second");
  first.res(okResp("first"));                // le plus ancien repond APRES
  await p1;
  assert.strictEqual(run("agentVersion"), "second", "reponse perimee a ecrase la plus recente");

  // ordre normal : l'ancien repond d'abord, le recent ensuite -> etat final = recent
  const a = deferred(), b = deferred(); const q2 = [a, b];
  sandbox.fetch = () => q2.shift().p;
  const pa = run("fetchServers()"), pb = run("fetchServers()");
  a.res(okResp("A")); await pa;
  b.res(okResp("B")); await pb;
  assert.strictEqual(run("agentVersion"), "B");
}

async function testFetchFailureDoesNotBlock() {
  const { sandbox, run } = loadApp();
  await tick(); await tick();
  sandbox.fetch = () => Promise.reject(new Error("reseau"));
  await run("fetchServers()");
  sandbox.fetch = async () => okResp("apres-echec");
  await run("fetchServers()");
  assert.strictEqual(run("agentVersion"), "apres-echec", "garde bloque apres un echec reseau");

  // un echec ANCIEN (arrive apres une reponse recente) ne doit pas afficher d'erreur
  const slow = deferred(), fast = deferred(); const q = [slow, fast];
  sandbox.fetch = () => q.shift().p;
  const ps = run("fetchServers()"), pf = run("fetchServers()");
  fast.res(okResp("recent")); await pf;
  slow.rej(new Error("timeout tardif")); await ps;
  assert.strictEqual(run("agentVersion"), "recent");
  assert.notStrictEqual(run("errorBanner.textContent"), "timeout tardif", "echec obsolete affiche");
}

(async () => {
  const failures = [];
  const step = async (name, fn) => {
    try { const r = await fn(); console.log(`PASS ${name}${r ? " (" + r + ")" : ""}`); }
    catch (e) { failures.push(name); console.log(`FAIL ${name}: ${String(e.message).split("\n")[0].slice(0, 300)}`); }
  };
  await step("jsArg helper", testJsArgHelper);
  await step("vues avec payloads hostiles", () => `${testHostileViews()} payloads`);
  await step("slug d'adoption", testAdoptSlug);
  await step("garde source onclick", testSourceGuard);
  await step("F2 fetchServers chevauchement", testFetchOverlap);
  await step("F2 echec reseau sans blocage", testFetchFailureDoesNotBlock);
  if (failures.length) { console.error(`INLINE HANDLERS FAIL: ${failures.length} test(s)`); process.exit(1); }
  console.log("INLINE HANDLERS OK");
})();
