// Smoke-test DOM minimal : execute app.js dans node avec un stub de DOM et rend une
// carte serveur complete. Attrape les ReferenceError/TypeError de rendu que ni pytest
// ni Pester ne peuvent voir (regression anyPending du 15/07, invisible pour node --check).
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function makeElement(tag) {
  return {
    tagName: tag, className: "", innerHTML: "", textContent: "", id: "",
    style: {}, dataset: {}, disabled: false, value: "",
    children: [],
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(type, fn) { (this._listeners || (this._listeners = [])).push([type, fn]); },
    querySelector() { return makeElement("div"); },
    querySelectorAll() { return []; },
    setAttribute() {}, remove() {}, focus() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
}

// Cache par id : app.js capture ses elements (const detailFiles = getElementById(...))
// une seule fois au chargement -- sans cache, un 2e appel avec le meme id renverrait un
// autre objet et casserait toute inspection posterieure depuis un test.
const elementsById = new Map();
const documentStub = {
  getElementById: (id) => {
    if (!elementsById.has(id)) elementsById.set(id, makeElement("div"));
    return elementsById.get(id);
  },
  createElement: (t) => makeElement(t),
  addEventListener() {},
  body: makeElement("body"),
};

const sandbox = {
  document: documentStub,
  window: { location: { href: "" } },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({ servers: [] }) }),
  setInterval: () => 0,
  setTimeout: () => 0,
  console,
  URLSearchParams,
  atob, btoa,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const appJs = fs.readFileSync(path.join(__dirname, "../../app/static/app.js"), "utf8");
// pas de "type=module" en prod : les function declarations sont globales, on reproduit ca
vm.runInContext(appJs, sandbox, { filename: "app.js" });

const sampleServer = {
  name: "palworld", display_name: "Palworld", server_appid: 2394010,
  public_buildid: "101",
  state: { process_up: true, players: null, buildid: "100", last_seen: new Date().toISOString(),
           process_started_at: null },
  update_available: true,
  auto_update_blocked: true,
  pending_orders: ["update"],
  order_queue: [{ id: "abc", type: "update", status: "pending", position: 1, total: 2 }],
  workshop_appid: 1623730,
  mods: [{ workshop_id: "1", title: "Mod", thumbnail_url: null, installed: true,
           installed_at: "2026-07-10T00:00:00+00:00", steam_updated_at: null }],
  mods_restart_required: false,
};

// rendu d'une carte avec ordre en attente (cas de la regression anyPending)
const card = vm.runInContext("renderCard", sandbox)(sampleServer);
if (!card) { console.error("renderCard n'a rien retourne"); process.exit(1); }

// variante sans aucun ordre
sampleServer.pending_orders = [];
sampleServer.order_queue = [];
vm.runInContext("renderCard", sandbox)(sampleServer);

// --- vignette Option A : resume mods (compte + 5 derniers evenements) ---
const summaryServer = {
  name: "palworld", workshop_appid: 1623730, mods_restart_required: false,
  mods: [
    { workshop_id: "1", title: "ModAncien", thumbnail_url: null, installed: true,
      installed_at: "2026-07-01T00:00:00+00:00", steam_updated_at: null },
    { workshop_id: "2", title: "ModRecent", thumbnail_url: null, installed: true,
      installed_at: null, steam_updated_at: "2026-07-14T00:00:00+00:00" },
    { workshop_id: "3", title: "ModSansDate", thumbnail_url: null, installed: true,
      installed_at: null, steam_updated_at: null },
  ],
};
const summaryHtml = vm.runInContext("renderModsSummary", sandbox)(summaryServer);
if (!summaryHtml.includes("3 installés")) { console.error("resume mods : compte absent"); process.exit(1); }
if (summaryHtml.indexOf("ModRecent") === -1 || summaryHtml.indexOf("ModAncien") === -1
    || summaryHtml.indexOf("ModRecent") > summaryHtml.indexOf("ModAncien")) {
  console.error("resume mods : tri des 5 derniers incorrect"); process.exit(1);
}
if (summaryHtml.includes("ModSansDate")) { console.error("resume mods : mod sans date liste"); process.exit(1); }
if (summaryHtml.indexOf("ev-updated") === -1 || summaryHtml.indexOf("ev-installed") === -1) {
  console.error("resume mods : libelles installe/maj absents"); process.exit(1);
}
if (vm.runInContext("renderModsSummary", sandbox)({ name: "windrose" }) !== "") {
  console.error("resume mods : doit etre vide sans workshop_appid"); process.exit(1);
}

// --- vignette cliquable : le clic sur un bouton ne doit PAS ouvrir l'overlay ---
vm.runInContext("openServerDetail = (n) => { globalThis.__openedDetail = n; };", sandbox);
const clickCard = vm.runInContext("renderCard", sandbox)(sampleServer);
const clickHandler = (clickCard._listeners || []).find(([t]) => t === "click");
if (!clickHandler) { console.error("vignette : pas de listener click"); process.exit(1); }
sandbox.__openedDetail = null;
clickHandler[1]({ target: { closest: (sel) => ({}) } });   // clic sur un bouton
if (sandbox.__openedDetail !== null) { console.error("vignette : clic bouton ouvre l'overlay"); process.exit(1); }
clickHandler[1]({ target: { closest: (sel) => null } });   // clic sur le fond de la carte
if (sandbox.__openedDetail !== "palworld") { console.error("vignette : clic carte n'ouvre pas l'overlay"); process.exit(1); }

// --- vue plein ecran : colonne mods ---
sandbox.__summaryServer = summaryServer;
const modsColHtml = vm.runInContext(
  "renderDetailMods(__summaryServer); detailMods.innerHTML", sandbox);
if (!modsColHtml.includes("ModAncien") || !modsColHtml.includes("mod-input-palworld")
    || !modsColHtml.includes("workshop-browser-palworld")) {
  console.error("plein ecran : colonne mods incomplete"); process.exit(1);
}
if (!modsColHtml.includes("retirer")) { console.error("plein ecran : bouton retirer absent"); process.exit(1); }

// serveur sans workshop_appid : colonne mods vide (mode une colonne)
const noModsHtml = vm.runInContext(
  "renderDetailMods({ name: 'windrose' }); detailMods.innerHTML", sandbox);
if (noModsHtml !== "") { console.error("plein ecran : colonne mods non vide sans workshop"); process.exit(1); }

// --- en-tete : pastille + boutons d'action partages ---
vm.runInContext("__sample = " + JSON.stringify(sampleServer), sandbox);
const detailBtns = vm.runInContext("buildActionButtons(__sample)", sandbox);
if (!Array.isArray(detailBtns) || detailBtns.length !== 4) {
  console.error("buildActionButtons : 4 boutons attendus"); process.exit(1);
}
vm.runInContext("renderDetailChrome(__sample)", sandbox);

// --- mods : chip d'etat unique + bouton "mettre a jour" ---
const updServer = {
  name: "palworld", workshop_appid: 1623730, mods_restart_required: false,
  mods: [
    { workshop_id: "10", title: "ModAJour", thumbnail_url: null, installed: true,
      installed_at: "2026-07-15T00:00:00+00:00", steam_updated_at: "2026-07-01T00:00:00+00:00",
      update_available: false },
    { workshop_id: "11", title: "ModPerime", thumbnail_url: null, installed: true,
      installed_at: "2026-07-01T00:00:00+00:00", steam_updated_at: "2026-07-15T00:00:00+00:00",
      update_available: true },
    { workshop_id: "12", title: "ModLegacy", thumbnail_url: null, installed: true,
      installed_at: null, steam_updated_at: "2026-07-15T00:00:00+00:00",
      update_available: false },
  ],
};
const updHtml = vm.runInContext("renderDetailMods(__upd); detailMods.innerHTML",
  Object.assign(sandbox, { __upd: updServer }) && sandbox);
if ((updHtml.match(/mod-status mod-needs-update/g) || []).length !== 1) {
  console.error("mods : chip maj disponible attendu sur le seul mod perime"); process.exit(1);
}
if (!updHtml.includes("Mods (3 installés · 1 maj dispo)")) {
  console.error("mods : compteur d'en-tete maj dispo incorrect"); process.exit(1);
}
if ((updHtml.match(/mod-status mod-uptodate/g) || []).length !== 1) {
  console.error("mods : chip a jour attendu sur ModAJour"); process.exit(1);
}
if ((updHtml.match(/mod-status mod-unknown-date/g) || []).length !== 1) {
  console.error("mods : chip etat inconnu attendu sur ModLegacy"); process.exit(1);
}
if ((updHtml.match(/>mettre à jour</g) || []).length !== 2) {
  console.error("mods : bouton attendu sur mod perime + mod legacy sans installed_at"); process.exit(1);
}
if (!updHtml.includes("updateMod('palworld', '11')") || !updHtml.includes("updateMod('palworld', '12')")) {
  console.error("mods : le bouton doit cibler updateMod avec le bon id"); process.exit(1);
}

// --- mods : bouton global "tout mettre a jour" ---
if (!updHtml.includes("tout mettre à jour") || !updHtml.includes("updateAllMods('palworld')")) {
  console.error("mods : bouton tout mettre a jour absent alors que des mods le necessitent"); process.exit(1);
}
const allUpToDate = {
  name: "palworld", workshop_appid: 1623730, mods_restart_required: false,
  mods: [{ workshop_id: "10", title: "ModAJour", thumbnail_url: null, installed: true,
           installed_at: "2026-07-15T00:00:00+00:00", steam_updated_at: "2026-07-01T00:00:00+00:00",
           update_available: false }],
};
const upToDateHtml = vm.runInContext("renderDetailMods(__utd); detailMods.innerHTML",
  Object.assign(sandbox, { __utd: allUpToDate }) && sandbox);
if (upToDateHtml.includes("tout mettre à jour")) {
  console.error("mods : bouton tout mettre a jour affiche sans rien a mettre a jour"); process.exit(1);
}

// --- detail joueurs : nom cliquable vers le profil Steam ---
const playersEl = makeElement("div");
const origGetById = documentStub.getElementById;
documentStub.getElementById = (id) => id === "players-detail-palworld" ? playersEl : makeElement("div");
vm.runInContext("renderPlayersDetailContent", sandbox)("palworld", [
  { name: "Bob & Cie", steamid: "76561198000000001", connected_since_seconds: 60 },
  { name: "SansId", steamid: null, connected_since_seconds: 60 },
  { name: "Louche", steamid: "javascript:alert(1)", connected_since_seconds: 60 },
]);
documentStub.getElementById = origGetById;
if (!playersEl.innerHTML.includes('href="https://steamcommunity.com/profiles/76561198000000001"')) {
  console.error("joueurs : lien profil Steam absent"); process.exit(1);
}
if (!playersEl.innerHTML.includes('target="_blank"') || !playersEl.innerHTML.includes("noopener")) {
  console.error("joueurs : lien profil sans target/_blank noopener"); process.exit(1);
}
if ((playersEl.innerHTML.match(/<a /g) || []).length !== 1) {
  console.error("joueurs : seul un steamid64 valide doit etre linkifie"); process.exit(1);
}
if (!playersEl.innerHTML.includes("Bob &amp; Cie")) {
  console.error("joueurs : nom non echappe dans le lien"); process.exit(1);
}

// --- re-rendu au poll : pas de crash overlay ferme ni ouvert ---
vm.runInContext("renderDetailFromLatest()", sandbox);                       // ferme (detailServerName null)
vm.runInContext("detailServerName = 'palworld'; latestServers = [__sample]; renderDetailFromLatest()", sandbox);

// --- detail : section "Actions recentes" (historique des ordres avec auteur) ---
const detailData = {
  rcon_info: null, uptime_seconds: null, process: { cpu_percent: null, mem_mb: null },
  players: [], playtime_totals: [], connection_log: [],
  order_history: [
    { type: "restart", status: "done", author: "boss", created: "2026-07-16T10:00:00+00:00", detail: "ok", title: null },
    { type: "install_mod", status: "failed", author: "auto", created: "2026-07-16T09:00:00+00:00",
      detail: 'echec "quote" <script>', title: "Mod X" },
  ],
};
const histHtml = vm.runInContext("renderServerDetail", sandbox)(detailData);
if (!histHtml.includes("Actions récentes") || !histHtml.includes("boss")) {
  console.error("historique : section ou auteur absent"); process.exit(1);
}
if (!histHtml.includes("hist-author auto")) {
  console.error("historique : badge auteur auto absent"); process.exit(1);
}
if (!histHtml.includes("échec") || !histHtml.includes("install_mod · Mod X")) {
  console.error("historique : statut echec ou titre du mod absent"); process.exit(1);
}
if (histHtml.includes("<script>")) {
  console.error("historique : detail d'ordre non echappe (XSS)"); process.exit(1);
}

// --- comptes : liste admin ---
const accountsEl = makeElement("div");
const origGetById2 = documentStub.getElementById;
documentStub.getElementById = (id) => id === "accountsList" ? accountsEl : makeElement("div");
vm.runInContext(`
  currentUser = { username: "boss", role: "admin", servers: [] };
  accountsCache = [
    { username: "boss", role: "admin", servers: [] },
    { username: "gardien", role: "user", servers: ["palworld"] },
  ];
  renderAccountsList();
`, sandbox);
documentStub.getElementById = origGetById2;
if (!accountsEl.innerHTML.includes("gardien") || !accountsEl.innerHTML.includes("palworld")) {
  console.error("comptes : liste incomplete"); process.exit(1);
}
if (!accountsEl.innerHTML.includes("deleteAccount('gardien')")) {
  console.error("comptes : bouton supprimer absent pour un autre compte"); process.exit(1);
}
if (accountsEl.innerHTML.includes("deleteAccount('boss')")) {
  console.error("comptes : le bouton supprimer ne doit pas viser son propre compte"); process.exit(1);
}

// --- carte : "lance par" (champ admin-only fourni par le backend) ---
sampleServer.started_by = { author: "gardien", at: new Date().toISOString() };
const startedCard = vm.runInContext("renderCard", sandbox)(sampleServer);
if (!startedCard.innerHTML.includes("lancé par") || !startedCard.innerHTML.includes("gardien")) {
  console.error("carte : ligne lance par absente"); process.exit(1);
}
delete sampleServer.started_by;
if (vm.runInContext("renderCard", sandbox)(sampleServer).innerHTML.includes("lancé par")) {
  console.error("carte : ligne lance par affichee sans le champ (compte scope)"); process.exit(1);
}

// --- detail : section Sauvegardes (bouton restaurer admin-only) ---
detailData.save_backups = [{ file: "20260717-020000-pre-update.zip", size_mb: 71.2,
                             created: "2026-07-17T02:00:00+00:00" }];
vm.runInContext(`currentUser = { username: "boss", role: "admin", servers: [] };`, sandbox);
const savesAdmin = vm.runInContext("renderServerDetail", sandbox)(detailData, "palworld");
if (!savesAdmin.includes("Sauvegardes") || !savesAdmin.includes("backupNow('palworld')")) {
  console.error("saves : section ou bouton sauvegarder absent"); process.exit(1);
}
if (!savesAdmin.includes("restoreSave('palworld', '20260717-020000-pre-update.zip')")) {
  console.error("saves : bouton restaurer absent pour un admin"); process.exit(1);
}
vm.runInContext(`currentUser = { username: "gardien", role: "user", servers: ["palworld"] };`, sandbox);
const savesUser = vm.runInContext("renderServerDetail", sandbox)(detailData, "palworld");
if (savesUser.includes("restoreSave(")) {
  console.error("saves : bouton restaurer visible pour un compte non admin"); process.exit(1);
}
vm.runInContext(`currentUser = { username: "boss", role: "admin", servers: [] };`, sandbox);

// --- comptes : editeur de mot de passe stylise (pas de window.prompt) ---
const accountsEl2 = makeElement("div");
const origGetById3 = documentStub.getElementById;
documentStub.getElementById = (id) => id === "accountsList" ? accountsEl2 : makeElement("div");
vm.runInContext(`togglePasswordEditor("gardien")`, sandbox);
documentStub.getElementById = origGetById3;
if (!accountsEl2.innerHTML.includes('id="pw-input-gardien"')
    || !accountsEl2.innerHTML.includes("saveAccountPassword('gardien')")) {
  console.error("comptes : editeur de mot de passe inline absent"); process.exit(1);
}
if (appJs.includes("window.prompt") || appJs.includes("window.alert")) {
  console.error("comptes : dialogue natif window.prompt/alert present"); process.exit(1);
}

// --- vue plein ecran : section Configuration (admin only), Task 5 ---
const configDetailData = {
  rcon_info: null, uptime_seconds: null, process: { cpu_percent: null, mem_mb: null },
  players: [], playtime_totals: [], connection_log: [], order_history: [],
};
sandbox.__cfgServer = { name: "cfgsrv", display_name: "<img src=x onerror=1>" };
vm.runInContext(`
  latestServers = [__cfgServer];
  currentUser = { username: "boss", role: "admin", servers: [] };
`, sandbox);
const cfgHtmlAdmin = vm.runInContext("renderServerDetail", sandbox)(configDetailData, "cfgsrv");
if (!cfgHtmlAdmin.includes('id="config-section-cfgsrv"')) {
  console.error("config : section absente pour un admin"); process.exit(1);
}
if (!cfgHtmlAdmin.includes("Configuration")) {
  console.error("config : titre absent"); process.exit(1);
}
if (cfgHtmlAdmin.includes("<img src=x onerror=1>")) {
  console.error("config : display_name non echappe (XSS)"); process.exit(1);
}
if (!cfgHtmlAdmin.includes("&lt;img src=x onerror=1&gt;")) {
  console.error("config : display_name echappe introuvable"); process.exit(1);
}

vm.runInContext(`currentUser = { username: "gardien", role: "user", servers: ["cfgsrv"] };`, sandbox);
const cfgHtmlUser = vm.runInContext("renderServerDetail", sandbox)(configDetailData, "cfgsrv");
if (cfgHtmlUser.includes('id="config-section-')) {
  console.error("config : section visible pour un compte non admin"); process.exit(1);
}
vm.runInContext(`currentUser = { username: "boss", role: "admin", servers: [] }; latestServers = [];`, sandbox);

// Cartes des etats de deploiement (Lot 2) : ne doivent jamais jeter au rendu.
const installingServer = { name: "vrising", display_name: "V Rising", server_appid: 1829350,
  status: "installing", state: null, public_buildid: null, pending_orders: ["install_game"],
  order_queue: [{ id: "o1", type: "install_game", status: "running", position: 1, total: 1 }] };
const awaitingServer = { ...installingServer, status: "awaiting_setup", pending_orders: [], order_queue: [] };
for (const s of [installingServer, awaitingServer]) {
  const card = sandbox.renderCard(s);
  if (!card || !card.innerHTML.length) throw new Error(`renderCard vide pour status=${s.status}`);
}
// XSS : un nom de jeu decouvert hostile doit ressortir echappe
const discoveredHtml = sandbox.renderDiscoveredGames([
  { appid: 1829350, name: "<img src=x onerror=alert(1)>", installdir: "X", buildid: "1" }]);
if (discoveredHtml.includes("<img src=x")) throw new Error("nom de jeu decouvert non echappe");
// Formulaire de finalisation : candidats agent echappes, adaptateur par defaut generic-graceful
const finalizeHtml = sandbox.finalizeFormHtml("vrising",
  { exe_candidates: ["VRisingServer.exe", "<script>x</script>.exe"] });
if (finalizeHtml.includes("<script>x</script>")) throw new Error("candidat exe non echappe");
if (!finalizeHtml.includes("generic-graceful")) throw new Error("adaptateur par defaut absent");

// --- etat de MAJ par mod : 1 etat explicite et 1 seul par combinaison de champs ---
const modStateCases = [
  [{ installed: true, installed_at: "2026-07-01T00:00:00+00:00", steam_updated_at: "2026-06-01T00:00:00+00:00", update_available: false }, "à jour"],
  [{ installed: true, installed_at: "2026-06-01T00:00:00+00:00", steam_updated_at: "2026-07-01T00:00:00+00:00", update_available: true }, "maj disponible"],
  [{ installed: true, installed_at: null, steam_updated_at: null, update_available: false }, "état inconnu"],
  [{ installed: false, installed_at: null, steam_updated_at: null, update_available: false }, "en attente"],
];
for (const [mod, expected] of modStateCases) {
  const st = sandbox.modUpdateState(mod);
  if (!st.label.includes(expected)) {
    throw new Error(`modUpdateState: attendu "${expected}", obtenu "${st.label}"`);
  }
}

// Section fichiers de config (Lot 3) : ne doit jamais jeter au rendu, meme sur
// listing vide, contenu absent, ou noms de fichiers a caracteres a echapper.
const sampleServerWithFiles = { ...sampleServer,
  files_listing: { install: ["Config/<script>.ini", "a.ini"] }, file_read: null };
sandbox.renderFilesSection(sampleServerWithFiles);
sandbox.renderFilesSection({ ...sampleServer, files_listing: {}, file_read: null });
sandbox.renderFilesSection({ ...sampleServer,
  files_listing: { install: ["a.ini"] },
  file_read: { root: "install", path: "a.ini", content_b64: "aGVsbG8=", sha256: "a".repeat(64) } });
const filesTreeHtml = sandbox.renderFilesSection(sampleServerWithFiles);
if (filesTreeHtml.includes("<script>.ini")) throw new Error("nom de fichier non echappe dans l'arborescence");
// Un nom de fichier Windows legal peut contenir des apostrophes -- esc() protege un
// attribut HTML mais PAS un onclick single-quote (l'entite est decodee avant que le JS
// ne parse la chaine). Le lien fichier ne doit JAMAIS repasser par un onclick inline
// avec le chemin interpole -- uniquement des attributs data-* (jamais executes comme code).
const evilPathHtml = sandbox.renderFilesSection({ ...sampleServer,
  files_listing: { install: ["a');alert(document.cookie);('.ini"] }, file_read: null });
if (evilPathHtml.includes("onclick=\"openFile(")) {
  throw new Error("lien fichier utilise encore un onclick inline avec chemin interpole (XSS)");
}
if (!evilPathHtml.includes('data-path="a&#39;);alert(document.cookie);(&#39;.ini"')) {
  throw new Error("chemin de fichier absent ou mal echappe dans l'attribut data-path");
}

const diff = sandbox.simpleLineDiff("a\nb\nc", "a\nx\nc");
if (!Array.isArray(diff) || diff.length === 0) throw new Error("simpleLineDiff doit renvoyer un tableau non vide");
if (!diff.some(l => l.type === "add") || !diff.some(l => l.type === "del")) {
  throw new Error("simpleLineDiff doit distinguer les lignes ajoutees/supprimees");
}

(async () => {
  // Flush toute promesse en attente depuis le chargement initial du script (avant
  // ce tout premier await du fichier -- ex. un fetchMe() jamais resolu) : sans ca elle
  // se resoudrait au milieu du test ci-dessous, contre le stub fetch du test, et
  // ecraserait currentUser au pire moment.
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));

  // Integration : loadServerDetail doit cabler files_listing/file_read (reponse
  // /detail) jusqu'au rendu de detailFiles. Bug reel trouve en revue Lot 3 :
  // latestServers (source de renderDetailFiles avant le fix) ne porte jamais ces
  // champs -- seul /detail les renvoie -- donnee et rendu ne se rencontraient nulle part.
  vm.runInContext(`currentUser = { username: "boss", role: "admin", servers: [] };`, sandbox);
  sandbox.fetch = async (url) => {
    if (url === "/api/servers/palworld/detail") {
      return { ok: true, status: 200, json: async () => ({
        rcon_info: null, uptime_seconds: null, process: { cpu_percent: null, mem_mb: null },
        players: [], playtime_totals: [], connection_log: [],
        files_listing: { install: ["a.ini"] },
        file_read: { root: "install", path: "a.ini", content_b64: "aGVsbG8=", sha256: "a".repeat(64) },
        order_history: [], save_backups: [],
      }) };
    }
    // Un fetchMe() reste en attente depuis le chargement initial du script (avant
    // le tout premier await de ce fichier) et se resout au 1er point d'attente venu --
    // sans ce cas explicite, il ecraserait currentUser (role admin requis par
    // renderFilesSection) avec le fallback generique {servers:[]}.
    if (url === "/api/me") {
      return { ok: true, status: 200, json: async () => ({ username: "boss", role: "admin", servers: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({ servers: [] }) };
  };
  await sandbox.loadServerDetail("palworld");
  if (!sandbox.document.getElementById("detailFiles").innerHTML.includes("a.ini")) {
    throw new Error("loadServerDetail ne cable pas files_listing jusqu'au rendu de detailFiles");
  }

  // Recherche de jeu (wizard deploiement) : rendu de resultats jamais explose, y
  // compris sur un nom contenant des caracteres a echapper.
  const searchResultsHtml = sandbox.renderDeploySearchResults([
    { appid: 2394010, name: "Palworld Dedicated Server" },
    { appid: 999, name: "<img src=x onerror=alert(1)>" },
  ]);
  if (searchResultsHtml.includes("<img src=x")) throw new Error("nom de resultat de recherche non echappe");
  if (!searchResultsHtml.includes("2394010")) throw new Error("appid absent du rendu des resultats");

  // Panneau detail du wizard de deploiement (image+description+formulaire) : jamais
  // d'exception, y compris avec une description contenant des caracteres a echapper
  // et sans image (double echec du backend, is_proxy=false).
  sandbox.renderDeployDetailPanel({ appid: 2394010, name: "Palworld Dedicated Server",
    header_image: "https://x/header.jpg", description: "Fight & <build> \"Pals\"", is_proxy: true });
  const panelHtml = sandbox.document.getElementById("deploy-detail-panel").innerHTML;
  if (panelHtml.includes("<build>")) throw new Error("description non echappee dans le panneau detail");
  if (!panelHtml.includes("image du jeu de base")) throw new Error("badge is_proxy absent");
  sandbox.renderDeployDetailPanel({ appid: 1, name: "Jeu Inconnu", header_image: null, description: null, is_proxy: false });

  console.log("SMOKE OK");
})().catch(e => { console.error(e); process.exit(1); });
