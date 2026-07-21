const errorBanner = document.getElementById("error");
const grid = document.getElementById("grid");
const searchBox = document.getElementById("searchBox");
const modalOverlay = document.getElementById("modalOverlay");
const modalMessage = document.getElementById("modalMessage");
const modalCancel = document.getElementById("modalCancel");
const modalConfirm = document.getElementById("modalConfirm");
let latestServers = [];
let discoveredGames = [];
let agentVersion = null;
let modalResolve = null;
// Panneaux ouverts par l'utilisateur (players-detail dans la grille) : la grille
// entiere est reconstruite depuis zero a chaque poll (toutes les 10s), ce qui fermait
// silencieusement tout panneau ouvert sans que l'etat JS survivant (ces trackers) ne
// soit jamais reapplique au DOM neuf -- bug signale le 2026-07-15 ("le workshop
// disparait apres un certain temps"). restoreOpenPanels() corrige ca pour la grille ;
// renderDetailFromLatest() fait la meme chose pour l'overlay detail (mods, workshop).
let openPlayersDetail = new Set();
let openFileEditor = null;  // {name, root, path, sha256} pendant l'edition, sinon null
// files_listing/file_read ne sont renvoyes que par GET /detail, jamais par la liste
// /api/servers (latestServers) -- dernier /detail charge, utilise par renderDetailFromLatest
// pour ne pas perdre la section fichiers a chaque poll de 10s (qui ne recharge que la liste).
let lastFilesData = null;

function confirmDialog(message, isHtml = false) {
  return new Promise((resolve) => {
    modalResolve = resolve;
    if (isHtml) {
      // SECURITE : seul point d'injection HTML parametrable de l'app. Tout appelant
      // isHtml=true DOIT passer chaque valeur dynamique par esc() -- une valeur
      // venant de l'agent ou de Steam injectee brute ici = XSS stocke (cf. 13/07).
      modalMessage.innerHTML = message;
    } else {
      modalMessage.textContent = message;
    }
    modalOverlay.classList.add("open");
    modalConfirm.focus();
  });
}

function closeModal(result) {
  modalOverlay.classList.remove("open");
  const outerActions = document.querySelector(".modal-body > .modal-actions");
  if (outerActions) outerActions.style.display = "";
  if (modalResolve) {
    modalResolve(result);
    modalResolve = null;
  }
}

// Modale generique (wizard deploiement, finalisation, adoption) : meme mecanique DOM
// que confirmDialog (conteneur modalOverlay, z-index 200, au-dessus de .detail-overlay
// a 100) mais avec un formulaire qui porte ses propres boutons -- on masque donc les
// boutons Annuler/Confirmer fixes (modalCancel/modalConfirm) pour eviter le doublon ;
// closeModal() les reaffiche systematiquement a la fermeture, quelle que soit la modale.
function openGenericModal(html) {
  modalMessage.innerHTML = html;
  const outerActions = document.querySelector(".modal-body > .modal-actions");
  if (outerActions) outerActions.style.display = "none";
  modalOverlay.classList.add("open");
}

modalCancel.addEventListener("click", () => closeModal(false));
modalConfirm.addEventListener("click", () => closeModal(true));
modalOverlay.addEventListener("click", (e) => {
  if (e.target === modalOverlay) closeModal(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modalOverlay.classList.contains("open")) closeModal(false);
});

const GAME_ASSETS = {
  palworld: { icon: "/static/assets/palworld-icon.png", logo: "/static/assets/palworld-logo.png" },
  windrose: { icon: "/static/assets/windrose-icon.png", symbol: "/static/assets/windrose-symbol.png" },
  valheim: { icon: "/static/assets/valheim-icon.png", logo: "/static/assets/valheim-logo.png" },
};

function showError(msg) {
  errorBanner.textContent = msg;
  errorBanner.style.display = msg ? "block" : "none";
}

function esc(v) {
  return String(v).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function relativeTime(iso) {
  if (!iso) return "jamais";
  const diffMs = Date.now() - new Date(iso).getTime();
  const min = Math.round(diffMs / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  const h = Math.round(min / 60);
  if (h < 48) return `il y a ${h} h`;
  return `il y a ${Math.round(h / 24)} j`;
}

function modUpdateState(m) {
  // Un seul etat lisible par mod, a la place du couple de dates ambigu.
  if (!m.installed) return { cls: "mod-pending", label: "en attente / échec" };
  if (m.update_available) return { cls: "mod-needs-update", label: "maj disponible" };
  if (!m.installed_at) return { cls: "mod-unknown-date", label: "état inconnu — re-baser" };
  return { cls: "mod-uptodate", label: "à jour" };
}

function modLatestEvent(m) {
  const inst = m.installed_at ? new Date(m.installed_at).getTime() : null;
  const upd = m.steam_updated_at ? new Date(m.steam_updated_at).getTime() : null;
  if (inst == null && upd == null) return null;
  if (upd == null || (inst != null && inst >= upd)) {
    return { kind: "installé", cls: "ev-installed", at: m.installed_at, ts: inst };
  }
  return { kind: "maj", cls: "ev-updated", at: m.steam_updated_at, ts: upd };
}

function renderModsSummary(s) {
  if (!("workshop_appid" in s)) return "";
  const mods = s.mods || [];
  const installedCount = mods.filter(m => m.installed).length;
  const needsUpdate = mods.filter(m => m.update_available).length;
  const modsLine = needsUpdate
    ? `${installedCount} installés · <span class="mods-needs-update">${needsUpdate} maj dispo</span>`
    : `${installedCount} installés`;
  const restartBanner = s.mods_restart_required
    ? `<div class="mods-restart-banner">redémarrage requis pour appliquer les mods</div>`
    : "";
  const recent = mods
    .map(m => ({ m, ev: modLatestEvent(m) }))
    .filter(x => x.ev !== null)
    .sort((a, b) => b.ev.ts - a.ev.ts)
    .slice(0, 5);
  const rows = recent.map(({ m, ev }) => `
    <div class="mods-recent-row">
      <span class="mods-ev ${ev.cls}">${ev.kind}</span>
      <span class="mods-recent-title">${esc(m.title)}</span>
      <span class="mods-recent-date">${esc(relativeTime(ev.at))}</span>
    </div>`).join("");
  return `
    <div class="datarow"><span>mods</span><span>${modsLine}</span></div>
    ${restartBanner}
    ${rows ? `<div class="mods-recent">${rows}</div>` : ""}
  `;
}

async function apiCall(path, options = {}) {
  const res = await fetch(path, options);
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("non autorisé");
  }
  return res;
}

function renderNameGroup(s) {
  const assets = GAME_ASSETS[s.name];
  if (!assets) return `<span class="name">${esc(s.display_name)}</span>`;
  const iconHtml = `<img class="game-icon" src="${esc(assets.icon)}" alt="">`;
  if (assets.logo) {
    return `${iconHtml}<img class="logo-word" src="${esc(assets.logo)}" alt="${esc(s.display_name)}">`;
  }
  return `${iconHtml}<img class="symbol-icon" src="${esc(assets.symbol)}" alt=""><span class="name">${esc(s.display_name)}</span>`;
}

function buildActionButtons(s) {
  const up = s.state ? s.state.process_up : null;
  const anyPending = s.pending_orders.length > 0;
  const defs = [
    { type: "start", pendingLabel: "démarrage…", primary: false, disabled: up === true || anyPending },
    { type: "stop", pendingLabel: "arrêt…", primary: false, disabled: up !== true || anyPending },
    { type: "update", pendingLabel: "maj en cours…", primary: true, disabled: !s.update_available || anyPending },
    { type: "restart", pendingLabel: "redémarrage…", primary: false, disabled: anyPending },
  ];
  return defs.map(d => {
    const btn = document.createElement("button");
    btn.className = d.primary ? "action primary" : "action";
    btn.innerHTML = s.pending_orders.includes(d.type) ? spinnerHtml(d.pendingLabel) : d.type;
    btn.disabled = d.disabled;
    btn.onclick = () => sendOrder(s.name, d.type);
    return btn;
  });
}

function renderCard(s) {
  if (s.status === "installing" || s.status === "awaiting_setup") {
    return renderDeployCard(s);
  }
  const state = s.state;
  const up = state ? state.process_up : null;
  const statusClass = up === true ? "up" : up === false ? "down" : "unknown";
  const statusText = up === true ? "up" : up === false ? "down" : "inconnu";
  const clickablePlayers = ["palworld", "windrose"].includes(s.name);
  const players = state && state.players != null ? state.players : "—";
  const local = state ? state.buildid : "—";
  const lastSeen = state ? relativeTime(state.last_seen) : "jamais";
  const updateFlag = `<div class="flag${s.update_available ? "" : " flag-hidden"}">maj disponible</div>`;
  const autoUpdateBlockedFlag = s.auto_update_blocked
    ? `<div class="flag flag-warn">maj auto impossible : joueurs inconnus</div>`
    : "";
  const queue = s.order_queue || [];
  const pendingText = queue.length
    ? `<div class="pending">${queue.map((o) =>
        `${esc(o.type)} (${o.position}/${o.total} dans la file)` +
        (o.status === "pending"
          ? ` <button class="cancel-order" onclick="cancelOrder('${esc(s.name)}', '${esc(o.id)}')" title="annuler cet ordre">✕</button>`
          : " — en cours")
      ).join("<br>")}</div>`
    : "";

  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="row1">
      <div class="name-group">${renderNameGroup(s)}</div>
      <span class="pill ${statusClass}"><span class="dot"></span>${esc(statusText)}</span>
    </div>
    ${updateFlag}
    ${autoUpdateBlockedFlag}
    <div class="datarow${clickablePlayers ? " clickable" : ""}" ${clickablePlayers ? `onclick="togglePlayers('${esc(s.name)}')"` : ""}><span>joueurs</span><span>${esc(players)}</span></div>
    <div class="players-detail" id="players-detail-${esc(s.name)}"></div>
    <div class="datarow"><span>version</span><span>${esc(local)} → ${esc(s.public_buildid)}</span></div>
    ${s.started_by ? `<div class="datarow"><span>lancé par</span><span>${esc(s.started_by.author || "—")}${s.started_by.at ? ` · ${relativeTime(s.started_by.at)}` : ""}</span></div>` : ""}
    <div class="datarow"><span>vu</span><span>${esc(lastSeen)}</span></div>
    ${renderModsSummary(s)}
    ${pendingText}
  `;

  card.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest("button, .datarow.clickable, .players-detail, input, select, .workshop-toggle")) return;
    openServerDetail(s.name);
  });

  const actions = document.createElement("div");
  actions.className = "card-actions";
  for (const btn of buildActionButtons(s)) actions.appendChild(btn);
  card.appendChild(actions);
  return card;
}

function renderDeployCard(s) {
  const isInstalling = s.status === "installing";
  const label = isInstalling ? "installation en cours…" : "en attente de finalisation";
  const queue = s.order_queue || [];
  const pendingText = queue.length
    ? `<div class="pending">${queue.map(o => `${esc(o.type)}${o.status === "running" ? " — en cours" : ""}`).join("<br>")}</div>`
    : "";
  const card = document.createElement("div");
  card.className = "card card-deploy";
  card.innerHTML = `
    <div class="row1">
      <div class="name-group"><span class="name">${esc(s.display_name || s.name)}</span></div>
      <span class="pill unknown"><span class="dot"></span>${esc(label)}</span>
    </div>
    <div class="datarow"><span>appid</span><span>${esc(s.server_appid)}</span></div>
    ${pendingText}`;
  if (!isInstalling && currentUser && currentUser.role === "admin") {
    const actions = document.createElement("div");
    actions.className = "card-actions";
    const btn = document.createElement("button");
    btn.textContent = "Finaliser";
    btn.onclick = () => openFinalize(s.name);
    actions.appendChild(btn);
    card.appendChild(actions);
  }
  return card;
}

// --- deploiement : wizard "deployer un serveur" (POST /api/deploy/servers) ---

const deployOverlay = document.getElementById("deployOverlay");
const deployClose = document.getElementById("deployClose");

function openDeployWizard() {
  document.getElementById("deploy-search-query").value = "";
  document.getElementById("deploy-direct-appid").value = "";
  document.getElementById("deploy-search-results").innerHTML = `<p class="files-empty">tape un nom de jeu ci-dessus</p>`;
  document.getElementById("deploy-detail-panel").innerHTML = `<p class="files-empty">clique un résultat pour voir le détail</p>`;
  deployOverlay.classList.add("open");
  document.getElementById("deploy-search-query").addEventListener("change", searchDeployCandidates);
  document.getElementById("deploy-direct-appid").addEventListener("change", onDeployDirectAppid);
}

function closeDeployWizard() {
  deployOverlay.classList.remove("open");
}

deployClose.addEventListener("click", closeDeployWizard);
deployOverlay.addEventListener("click", (e) => { if (e.target === deployOverlay) closeDeployWizard(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && deployOverlay.classList.contains("open")) closeDeployWizard();
});

function renderDeploySearchResults(results) {
  if (!results.length) return `<p class="files-empty">aucun résultat</p>`;
  return `<ul class="deploy-search-list">${results.map(r => `
    <li><a href="#" class="deploy-search-link" data-appid="${r.appid}" data-name="${esc(r.name)}">
      ${esc(r.name)} <span class="deploy-search-appid">(${r.appid})</span></a></li>`).join("")}
    </ul>`;
}

async function searchDeployCandidates() {
  const q = document.getElementById("deploy-search-query").value.trim();
  const el = document.getElementById("deploy-search-results");
  if (!el) return;
  if (q.length < 2) { el.innerHTML = `<p class="files-empty">tape un nom de jeu ci-dessus</p>`; return; }
  el.innerHTML = `<p class="files-empty">recherche...</p>`;
  try {
    const res = await apiCall(`/api/deploy/search?q=${encodeURIComponent(q)}`);
    if (!res.ok) { el.innerHTML = `<p class="files-empty">erreur de recherche</p>`; return; }
    const { results } = await res.json();
    el.innerHTML = renderDeploySearchResults(results);
    el.querySelectorAll(".deploy-search-link").forEach(link => {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        el.querySelectorAll(".deploy-search-link").forEach(l => l.classList.remove("active"));
        link.classList.add("active");
        selectDeployCandidate(parseInt(link.dataset.appid, 10), link.dataset.name);
      });
    });
  } catch (e) { el.innerHTML = `<p class="files-empty">erreur de recherche</p>`; }
}

async function onDeployDirectAppid() {
  const appid = parseInt(document.getElementById("deploy-direct-appid").value, 10);
  if (!appid) return;
  let name = `AppID ${appid}`;
  try {
    const res = await apiCall(`/api/deploy/appinfo/${appid}`);
    if (res.ok) { const d = await res.json(); if (d.name) name = d.name; }
  } catch (e) { /* pre-remplissage best-effort */ }
  selectDeployCandidate(appid, name);
}

function deployDetailFormHtml(appid, name) {
  const slug = name.toLowerCase().replace(/dedicated server/gi, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
  return `
    <div class="field"><label>Identifiant (slug)</label>
      <input type="text" id="deploy-name" maxlength="32" pattern="[a-z0-9-]+" value="${esc(slug)}"></div>
    <div class="field"><label>Nom affiché</label>
      <input type="text" id="deploy-display" maxlength="60" value="${esc(name)}"></div>
    <input type="hidden" id="deploy-appid" value="${appid}">
    <div class="modal-error" id="deploy-error"></div>
    <div class="btn-row">
      <button onclick="closeDeployWizard()">annuler</button>
      <button onclick="submitDeployWizard()">installer</button>
    </div>`;
}

function renderDeployDetailPanel(state) {
  const el = document.getElementById("deploy-detail-panel");
  if (!el) return;
  const { appid, name, header_image, description, is_proxy, loading } = state;
  if (loading) { el.innerHTML = `<p class="files-empty">chargement...</p>`; return; }
  const img = header_image ? `<img src="${esc(header_image)}" alt="">` : "";
  const badge = is_proxy ? `<span class="badge-proxy">image du jeu de base</span>` : "";
  const desc = description ? `<p class="deploy-desc">${esc(description)}</p>` : "";
  el.innerHTML = `${img}<h4>${esc(name)} ${badge}</h4>
    <div class="appid-line">AppID ${appid}</div>
    ${desc}${deployDetailFormHtml(appid, name)}`;
}

async function selectDeployCandidate(appid, name) {
  renderDeployDetailPanel({ appid, name, loading: true });
  try {
    const res = await apiCall(`/api/deploy/details?appid=${appid}&name=${encodeURIComponent(name)}`);
    const details = res.ok ? await res.json() : { header_image: null, description: null, is_proxy: false };
    renderDeployDetailPanel({ appid, name, ...details });
  } catch (e) {
    renderDeployDetailPanel({ appid, name, header_image: null, description: null, is_proxy: false });
  }
}

async function submitDeployWizard() {
  const appidEl = document.getElementById("deploy-appid");
  const nameEl = document.getElementById("deploy-name");
  const displayEl = document.getElementById("deploy-display");
  const errEl = document.getElementById("deploy-error");
  if (!appidEl || !nameEl || !displayEl) { if (errEl) errEl.textContent = "sélectionnez d'abord un jeu"; return; }
  const body = {
    server_appid: parseInt(appidEl.value, 10),
    name: nameEl.value.trim(),
    display_name: displayEl.value.trim(),
  };
  if (!body.server_appid || !body.name || !body.display_name) {
    errEl.textContent = "tous les champs sont requis"; return;
  }
  const res = await apiCall("/api/deploy/servers", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    errEl.textContent = (await res.json().catch(() => ({}))).detail || `erreur ${res.status}`;
    return;
  }
  closeDeployWizard();
  fetchServers();
}

// --- deploiement : finalisation (POST /api/deploy/servers/{name}/setup) ---

function finalizeFormHtml(name, reg) {
  const candidates = reg.exe_candidates || [];
  const options = candidates.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  return `
    <h3>Finaliser ${esc(reg.display_name || name)}</h3>
    <label>Exécutable serveur <select id="fin-exe">${options}</select></label>
    <label>Arguments <input type="text" id="fin-args" maxlength="500"></label>
    <label>Adaptateur d'arrêt
      <select id="fin-adapter" onchange="document.getElementById('fin-rcon').style.display = this.value === 'rcon-generic' ? '' : 'none'">
        <option value="generic-graceful" selected>generic-graceful (arrêt propre)</option>
        <option value="rcon-generic">rcon-generic (commande RCON)</option>
        <option value="generic-force">generic-force (kill direct)</option>
      </select></label>
    <div id="fin-rcon" style="display:none">
      <label>RCON host <input type="text" id="fin-rcon-host" value="127.0.0.1"></label>
      <label>RCON port <input type="number" id="fin-rcon-port" min="1" max="65535"></label>
      <label>RCON password <input type="password" id="fin-rcon-password" maxlength="128"></label>
      <label>Commande d'arrêt <input type="text" id="fin-rcon-shutdown" value="shutdown" maxlength="200"></label>
      <label>Commande d'annonce ({delay}/{reason} substitués)
        <input type="text" id="fin-rcon-announce" maxlength="200"></label>
    </div>
    <label>Query port A2S (optionnel) <input type="number" id="fin-query-port" min="1" max="65535"></label>
    <label>Dossier de saves (optionnel, relatif à l'install) <input type="text" id="fin-save-dir" maxlength="260"></label>
    <label>Délai d'annonce avant arrêt (s) <input type="number" id="fin-warn" min="0" max="600"></label>
    <label><input type="checkbox" id="fin-start-now" checked> démarrer immédiatement</label>
    <div class="modal-error" id="fin-error"></div>
    <div class="modal-actions">
      <button onclick="closeModal(false)">annuler</button>
      <button onclick="submitFinalize('${esc(name)}')">créer la tâche et activer</button>
    </div>`;
}

async function openFinalize(name) {
  const res = await apiCall(`/api/servers/${encodeURIComponent(name)}/registry`);
  if (!res.ok) { showError("registre inaccessible"); return; }
  const reg = await res.json();
  openGenericModal(finalizeFormHtml(name, reg));
}

async function submitFinalize(name) {
  const adapter = document.getElementById("fin-adapter").value;
  const body = {
    exe_path: document.getElementById("fin-exe").value,
    launch_args: document.getElementById("fin-args").value,
    stop_adapter: adapter,
    query_port: parseInt(document.getElementById("fin-query-port").value, 10) || null,
    save_dir: document.getElementById("fin-save-dir").value || null,
    stop_warn_seconds: parseInt(document.getElementById("fin-warn").value, 10) || null,
    start_now: document.getElementById("fin-start-now").checked,
  };
  if (adapter === "rcon-generic") {
    body.rcon = {
      host: document.getElementById("fin-rcon-host").value,
      port: parseInt(document.getElementById("fin-rcon-port").value, 10),
      password: document.getElementById("fin-rcon-password").value,
      shutdown_command: document.getElementById("fin-rcon-shutdown").value || null,
      announce_command: document.getElementById("fin-rcon-announce").value || null,
    };
  }
  const res = await apiCall(`/api/deploy/servers/${encodeURIComponent(name)}/setup`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    document.getElementById("fin-error").textContent =
      (await res.json().catch(() => ({}))).detail || `erreur ${res.status}`;
    return;
  }
  closeModal(true);
  fetchServers();
}

// --- jeux Steam decouverts sur la machine mais non geres (admin only) ---

function renderDiscoveredGames(games) {
  if (!games || !games.length) return "";
  const rows = games.map(g => `
    <div class="discovered-row">
      <span class="discovered-name">${esc(g.name || "(sans nom)")}</span>
      <span class="discovered-meta">appid ${esc(g.appid)} · build ${esc(g.buildid || "?")}</span>
      <button onclick="openAdopt(${parseInt(g.appid, 10) || 0}, '${esc((g.name || "").toLowerCase().replace(/dedicated server/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32))}')">Adopter</button>
    </div>`).join("");
  return `<div class="discovered-section"><h4>Jeux détectés non gérés</h4>${rows}</div>`;
}

function adoptFormHtml(appid, suggestedName) {
  return `
    <h3>Adopter le serveur détecté</h3>
    <label>Identifiant (slug) <input type="text" id="adopt-name" maxlength="32"
      pattern="[a-z0-9-]+" value="${esc(suggestedName || "")}"></label>
    <label>Nom affiché <input type="text" id="adopt-display" maxlength="60"></label>
    <div class="modal-error" id="adopt-error"></div>
    <div class="modal-actions">
      <button onclick="closeModal(false)">annuler</button>
      <button onclick="submitAdopt(${parseInt(appid, 10) || 0})">adopter</button>
    </div>`;
}

function openAdopt(appid, suggestedName) {
  openGenericModal(adoptFormHtml(appid, suggestedName));
}

async function submitAdopt(appid) {
  const body = {
    appid,
    name: document.getElementById("adopt-name").value.trim(),
    display_name: document.getElementById("adopt-display").value.trim(),
  };
  const errEl = document.getElementById("adopt-error");
  if (!appid || !body.name || !body.display_name) {
    errEl.textContent = "tous les champs sont requis"; return;
  }
  const res = await apiCall("/api/deploy/adopt", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    errEl.textContent = (await res.json().catch(() => ({}))).detail || `erreur ${res.status}`;
    return;
  }
  closeModal(true);
  fetchServers();
}

function spinnerHtml(label) {
  return `<span class="spinner"></span>${esc(label)}`;
}

function formatDuration(seconds) {
  if (seconds < 60) return "< 1 min";
  const min = Math.floor(seconds / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const remMin = min % 60;
  return remMin > 0 ? `${h}h ${remMin}min` : `${h}h`;
}

let playersCache = {};

// steamid vient de l'agent (moins privilégié) : on ne le met dans un href que
// s'il a la forme stricte d'un SteamID64, sinon simple texte échappé.
function playerNameHtml(p) {
  const steamPart = p.steamid ? ` <span style="color: var(--muted)">(${esc(p.steamid)})</span>` : "";
  if (p.steamid && /^\d{17}$/.test(p.steamid)) {
    return `<a class="player-link" href="https://steamcommunity.com/profiles/${p.steamid}" target="_blank" rel="noopener noreferrer">${esc(p.name)}</a>${steamPart}`;
  }
  return esc(p.name) + steamPart;
}

function renderPlayersDetailContent(name, players) {
  const el = document.getElementById(`players-detail-${name}`);
  if (!el) return;
  if (players.length === 0) {
    el.innerHTML = `<div class="players-detail-row"><span>aucun joueur</span></div>`;
    return;
  }
  el.innerHTML = players.map(p =>
    `<div class="players-detail-row"><span>${playerNameHtml(p)}</span><span>${formatDuration(p.connected_since_seconds)}</span></div>`
  ).join("");
}

async function togglePlayers(name) {
  const el = document.getElementById(`players-detail-${name}`);
  if (!el) return;

  const isOpen = el.classList.contains("open");
  if (isOpen) {
    el.classList.remove("open");
    el.innerHTML = "";
    openPlayersDetail.delete(name);
    return;
  }

  el.classList.add("open");
  openPlayersDetail.add(name);
  el.innerHTML = `<div class="players-detail-row"><span>chargement…</span></div>`;

  try {
    const res = await apiCall(`/api/servers/${name}/players`);
    if (!res.ok) {
      el.innerHTML = `<div class="players-detail-row"><span>détail indisponible</span></div>`;
      return;
    }
    const data = await res.json();
    playersCache[name] = data.players;
    renderPlayersDetailContent(name, data.players);
  } catch (e) {
    el.innerHTML = `<div class="players-detail-row"><span>détail indisponible</span></div>`;
  }
}

function restorePlayersDetail(name) {
  if (playersCache[name]) {
    const el = document.getElementById(`players-detail-${name}`);
    if (!el) return;
    el.classList.add("open");
    renderPlayersDetailContent(name, playersCache[name]);
  } else {
    togglePlayers(name);
  }
}

const detailOverlay = document.getElementById("detailOverlay");
const detailTitle = document.getElementById("detailTitle");
const detailStatus = document.getElementById("detailStatus");
const detailActions = document.getElementById("detailActions");
const detailColumns = document.getElementById("detailColumns");
const detailBody = document.getElementById("detailBody");
const detailMods = document.getElementById("detailMods");
const detailFiles = document.getElementById("detailFiles");
// Delegation plutot qu'un onclick inline avec chemin interpole : un nom de fichier
// Windows peut contenir des apostrophes/parentheses/points-virgules -- esc() protege
// un attribut HTML mais PAS un handler onclick single-quote (le navigateur decode
// l'entite AVANT que le JS ne parse la chaine -- stored XSS trouve en revue finale
// 18/07). data-* + dataset ne repassent jamais par l'interpreteur JS.
detailFiles.addEventListener("click", (e) => {
  const link = e.target.closest(".file-open-link");
  if (!link) return;
  e.preventDefault();
  openFile(link.dataset.serverName, link.dataset.root, link.dataset.path);
});
const detailClose = document.getElementById("detailClose");
const detailRefresh = document.getElementById("detailRefresh");
let detailServerName = null;

async function openServerDetail(name) {
  detailServerName = name;
  const server = latestServers.find(s => s.name === name);
  // icone + logo du jeu dans l'en-tete (meme rendu que la vignette) ; renderNameGroup
  // n'insere que des donnees statiques de GAME_ASSETS + display_name echappe
  detailTitle.innerHTML = server ? renderNameGroup(server) : esc(name);
  if (server) {
    renderDetailChrome(server);
    renderDetailMods(server);
  }
  // files_listing/file_read viennent de /detail (jamais de latestServers) : rien de
  // valide a afficher avant que loadServerDetail() resolve -- reset explicite plutot
  // que de risquer un flash avec les donnees du serveur precedemment ouvert.
  lastFilesData = null;
  detailFiles.innerHTML = "";
  detailOverlay.classList.add("open");
  await loadServerDetail(name);
}

function closeServerDetail() {
  if (detailServerName) delete workshopBrowserState[detailServerName];
  detailOverlay.classList.remove("open");
  detailServerName = null;
}

async function loadServerDetail(name) {
  detailBody.innerHTML = `<div class="detail-empty">chargement…</div>`;
  try {
    const res = await apiCall(`/api/servers/${name}/detail`);
    if (!res.ok) {
      detailBody.innerHTML = `<div class="detail-empty">détail indisponible</div>`;
      return;
    }
    const data = await res.json();
    detailBody.innerHTML = renderServerDetail(data, name);
    lastFilesData = { name, files_listing: data.files_listing || {}, file_read: data.file_read || null };
    renderDetailFiles(lastFilesData);
  } catch (e) {
    detailBody.innerHTML = `<div class="detail-empty">détail indisponible</div>`;
  }
}

function renderServerDetail(d, name) {
  const sections = [];
  const isAdmin = currentUser && currentUser.role === "admin";

  if (name) {
    const configHtml = renderDetailConfig({ name, display_name: (latestServers.find(x => x.name === name) || {}).display_name });
    if (configHtml) sections.push(configHtml);

    const rows = (d.save_backups || []).map(b => {
      const when = b.created ? new Date(b.created).toLocaleString("fr-FR") : "—";
      const size = b.size_mb != null ? ` · ${Number(b.size_mb).toFixed(0)} Mo` : "";
      const restoreBtn = isAdmin
        ? ` <button class="save-btn save-restore" onclick="restoreSave('${esc(name)}', '${esc(b.file)}')">restaurer</button>`
        : "";
      return `<div class="detail-row"><span>${esc(b.file)}</span><span>${esc(when)}${size}${restoreBtn}</span></div>`;
    }).join("");
    sections.push(`<div class="detail-section">
      <div class="mods-head"><h4>Sauvegardes</h4><button class="save-btn" onclick="backupNow('${esc(name)}')">sauvegarder maintenant</button></div>
      ${rows || `<div class="detail-empty">aucune sauvegarde</div>`}
    </div>`);
  }

  if (d.rcon_info) {
    sections.push(`<div class="detail-section"><h4>Info serveur</h4><div class="detail-row"><span>${esc(d.rcon_info)}</span></div></div>`);
  }

  const hasProcessInfo = d.uptime_seconds != null || d.process.cpu_percent != null || d.process.mem_mb != null;
  if (hasProcessInfo) {
    const rows = [];
    if (d.uptime_seconds != null) rows.push(`<div class="detail-row"><span>uptime</span><span>${formatDuration(d.uptime_seconds)}</span></div>`);
    if (d.process.cpu_percent != null) rows.push(`<div class="detail-row"><span>CPU</span><span>${d.process.cpu_percent.toFixed(1)}%</span></div>`);
    if (d.process.mem_mb != null) rows.push(`<div class="detail-row"><span>RAM</span><span>${d.process.mem_mb.toFixed(0)} Mo</span></div>`);
    sections.push(`<div class="detail-section"><h4>Process</h4>${rows.join("")}</div>`);
  }

  if (d.players.length > 0) {
    const rows = d.players.map(p =>
      `<div class="detail-row"><span>${playerNameHtml(p)}</span><span>${formatDuration(p.connected_since_seconds)}</span></div>`
    ).join("");
    sections.push(`<div class="detail-section"><h4>Joueurs connectés</h4>${rows}</div>`);
  }

  if (d.playtime_totals.length > 0) {
    const rows = d.playtime_totals.map(t =>
      `<div class="detail-row"><span>${esc(t.name)}</span><span>${formatDuration(t.total_seconds)}</span></div>`
    ).join("");
    sections.push(`<div class="detail-section"><h4>Temps de jeu total</h4>${rows}</div>`);
  }

  if (d.connection_log.length > 0) {
    const rows = d.connection_log.map(e => {
      const start = new Date(e.connected_at).toLocaleString("fr-FR");
      const durationSeconds = e.disconnected_at
        ? (new Date(e.disconnected_at) - new Date(e.connected_at)) / 1000
        : null;
      const durationText = durationSeconds != null ? formatDuration(durationSeconds) : "en cours";
      return `<div class="detail-row"><span>${esc(e.name)}</span><span>${esc(start)} — ${durationText}</span></div>`;
    }).join("");
    sections.push(`<div class="detail-section"><h4>Historique (7 jours)</h4>${rows}</div>`);
  }

  if ((d.order_history || []).length > 0) {
    const rows = d.order_history.map(o => {
      const when = new Date(o.created).toLocaleString("fr-FR");
      const label = o.title ? `${o.type} · ${o.title}` : o.type;
      const outcome = o.status === "done" ? "ok" : "échec";
      return `<div class="detail-row" ${o.detail ? `title="${esc(o.detail)}"` : ""}>
        <span>${esc(label)} <span class="hist-author${o.author === "auto" ? " auto" : ""}">${esc(o.author || "—")}</span></span>
        <span>${esc(when)} · <span class="hist-status ${o.status === "done" ? "ok" : "ko"}">${outcome}</span></span>
      </div>`;
    }).join("");
    sections.push(`<div class="detail-section"><h4>Actions récentes</h4>${rows}</div>`);
  }

  if (sections.length === 0) {
    return `<div class="detail-empty">aucune information disponible pour ce serveur</div>`;
  }
  return sections.join("");
}

// --- configuration serveur (admin only) : GET/PUT /api/servers/{name}/registry ---
// Le backend ne renvoie jamais le mot de passe RCON (rcon.password_set: bool a la place) ;
// le formulaire ne l'ecrase donc que si le champ est rempli. renderServerDetail() n'est
// invoquee que par loadServerDetail() (ouverture + bouton Actualiser), jamais par le poll
// 10s (renderDetailFromLatest() ne touche que le chrome + la colonne mods) : la carte de
// configuration et son brouillon d'edition ne peuvent donc pas etre ecrases en arriere-plan.
const configCache = {};             // name -> registre (masque) le plus recemment charge
const configEditingFor = new Set(); // noms dont le formulaire d'edition est actuellement ouvert
const STOP_ADAPTERS = ["palworld-rcon", "generic-graceful", "generic-force", "rcon-generic"];

function renderDetailConfig(s) {
  if (!s || !currentUser || currentUser.role !== "admin") return "";
  const name = s.name;
  if (!configCache[name]) loadServerConfig(name);
  const reg = configCache[name];
  const isEditing = configEditingFor.has(name);
  const body = !reg
    ? `<div class="detail-empty">chargement…</div>`
    : isEditing ? configFormHtml(name, reg) : configReadHtml(reg);
  const modifyBtn = (reg && !isEditing)
    ? `<button class="save-btn" onclick="editServerConfig('${esc(name)}')">modifier</button>`
    : "";
  return `<div class="detail-section" id="config-section-${esc(name)}">
    <div class="mods-head"><h4>Configuration — ${esc(s.display_name)}</h4><span id="config-modify-slot-${esc(name)}">${modifyBtn}</span></div>
    <div id="config-body-${esc(name)}">${body}</div>
  </div>`;
}

function configReadHtml(reg) {
  const rcon = reg.rcon || null;
  const rconLine = rcon
    ? `${esc(rcon.host || "—")}:${esc(rcon.port != null ? rcon.port : "—")} · mot de passe ${rcon.password_set ? "défini" : "non défini"}`
    : "—";
  const rows = [
    ["statut", reg.status],
    ["process", reg.process],
    ["tâche de démarrage", reg.start_task],
    ["arguments de lancement", reg.launch_args],
    ["adaptateur d'arrêt", reg.stop_adapter],
    ["port de requête", reg.query_port],
    ["dossier de sauvegarde", reg.save_dir],
    ["délai d'avertissement (s)", reg.stop_warn_seconds],
  ];
  const rowsHtml = rows.map(([label, val]) =>
    `<div class="detail-row"><span>${esc(label)}</span><span>${val != null && val !== "" ? esc(val) : "—"}</span></div>`
  ).join("");
  return `${rowsHtml}<div class="detail-row"><span>rcon</span><span>${rconLine}</span></div>`;
}

function configFormHtml(name, reg) {
  const rcon = reg.rcon || {};
  const n = esc(name);
  const opt = (val, cur) => `<option value="${esc(val)}" ${cur === val ? "selected" : ""}>${esc(val)}</option>`;
  return `<form class="config-form" onsubmit="return false">
    <label>statut
      <select id="cfg-status-${n}">${opt("active", reg.status)}${opt("disabled", reg.status)}</select>
    </label>
    <label>process <input type="text" id="cfg-process-${n}" value="${esc(reg.process || "")}"></label>
    <label>tâche de démarrage <input type="text" id="cfg-start_task-${n}" value="${esc(reg.start_task || "")}"></label>
    <label>arguments de lancement <input type="text" id="cfg-launch_args-${n}" value="${esc(reg.launch_args || "")}"></label>
    <label>adaptateur d'arrêt
      <select id="cfg-stop_adapter-${n}">${STOP_ADAPTERS.map(v => opt(v, reg.stop_adapter)).join("")}</select>
    </label>
    <label>rcon — hôte <input type="text" id="cfg-rcon_host-${n}" value="${esc(rcon.host || "")}"></label>
    <label>rcon — port <input type="number" id="cfg-rcon_port-${n}" value="${rcon.port != null ? esc(rcon.port) : ""}"></label>
    <label>rcon — mot de passe <input type="password" id="cfg-rcon_password-${n}" placeholder="inchangé si vide" autocomplete="off"></label>
    <label>rcon — commande d'arrêt <input type="text" id="cfg-rcon_shutdown-${n}" value="${esc(rcon.shutdown_command || "")}"></label>
    <label>rcon — commande d'annonce <input type="text" id="cfg-rcon_announce-${n}" value="${esc(rcon.announce_command || "")}"></label>
    <label>port de requête <input type="number" id="cfg-query_port-${n}" value="${reg.query_port != null ? esc(reg.query_port) : ""}"></label>
    <label>dossier de sauvegarde <input type="text" id="cfg-save_dir-${n}" value="${esc(reg.save_dir || "")}"></label>
    <label>délai d'avertissement (s) <input type="number" id="cfg-stop_warn_seconds-${n}" value="${reg.stop_warn_seconds != null ? esc(reg.stop_warn_seconds) : ""}"></label>
    <div class="config-form-actions">
      <button type="button" class="save-btn" onclick="saveServerConfig('${n}')">enregistrer</button>
      <button type="button" class="save-btn" onclick="cancelServerConfig('${n}')">annuler</button>
    </div>
  </form>`;
}

async function loadServerConfig(name) {
  try {
    const res = await apiCall(`/api/servers/${name}/registry`);
    if (!res.ok) return;
    configCache[name] = await res.json();
    renderConfigBodyIfPresent(name);
  } catch (e) { /* configuration indisponible : le bouton "modifier" reste absent, retente au prochain rendu */ }
}

function renderConfigBodyIfPresent(name) {
  const el = document.getElementById(`config-body-${name}`);
  if (!el) return; // overlay ferme ou serveur different entre-temps
  const reg = configCache[name];
  if (!reg) { el.innerHTML = `<div class="detail-empty">configuration indisponible</div>`; return; }
  const isEditing = configEditingFor.has(name);
  el.innerHTML = isEditing ? configFormHtml(name, reg) : configReadHtml(reg);
  const slot = document.getElementById(`config-modify-slot-${name}`);
  if (slot) {
    slot.innerHTML = !isEditing
      ? `<button class="save-btn" onclick="editServerConfig('${esc(name)}')">modifier</button>`
      : "";
  }
}

async function editServerConfig(name) {
  const el = document.getElementById(`config-body-${name}`);
  if (el) el.innerHTML = `<div class="detail-empty">chargement…</div>`;
  try {
    const res = await apiCall(`/api/servers/${name}/registry`);
    if (!res.ok) {
      showError(`Configuration indisponible (${res.status}).`);
      renderConfigBodyIfPresent(name);
      return;
    }
    configCache[name] = await res.json();
    configEditingFor.add(name);
    renderConfigBodyIfPresent(name);
  } catch (e) {
    showError(String(e.message || e));
    renderConfigBodyIfPresent(name);
  }
}

function cancelServerConfig(name) {
  configEditingFor.delete(name);
  renderConfigBodyIfPresent(name);
}

async function saveServerConfig(name) {
  const loaded = configCache[name] || {};
  const val = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  const body = {};

  const strField = (key) => {
    const cur = val(`cfg-${key}-${name}`).trim();
    const before = loaded[key] != null ? String(loaded[key]) : "";
    if (cur !== before) body[key] = cur === "" ? null : cur;
  };
  const numField = (key) => {
    const raw = val(`cfg-${key}-${name}`).trim();
    const before = loaded[key] != null ? String(loaded[key]) : "";
    if (raw !== before) body[key] = raw === "" ? null : Number(raw);
  };

  strField("status");
  strField("process");
  strField("start_task");
  strField("launch_args");
  strField("stop_adapter");
  numField("query_port");
  strField("save_dir");
  numField("stop_warn_seconds");

  const rconLoaded = loaded.rcon || {};
  const rHost = val(`cfg-rcon_host-${name}`).trim();
  const rPort = val(`cfg-rcon_port-${name}`).trim();
  const rPassword = val(`cfg-rcon_password-${name}`);
  const rShutdown = val(`cfg-rcon_shutdown-${name}`).trim();
  const rAnnounce = val(`cfg-rcon_announce-${name}`).trim();
  const rconChanged =
    rHost !== (rconLoaded.host || "") ||
    rPort !== (rconLoaded.port != null ? String(rconLoaded.port) : "") ||
    rShutdown !== (rconLoaded.shutdown_command || "") ||
    rAnnounce !== (rconLoaded.announce_command || "") ||
    rPassword !== "";
  if (rconChanged) {
    body.rcon = {
      host: rHost,
      port: Number(rPort),
      shutdown_command: rShutdown || null,
      announce_command: rAnnounce || null,
    };
    if (rPassword !== "") body.rcon.password = rPassword;
  }

  try {
    const res = await apiCall(`/api/servers/${name}/registry`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      showError(`Erreur enregistrement configuration : ${errBody.detail || res.status}`);
      return;
    }
    configCache[name] = await res.json();
    configEditingFor.delete(name);
    showError("");
    const el = document.getElementById(`config-body-${name}`);
    renderConfigBodyIfPresent(name);
    if (el) {
      el.innerHTML = `<div class="config-saved-banner">Enregistré — appliqué par l'agent au prochain cycle (≤ 2 min)</div>${el.innerHTML}`;
    }
  } catch (e) {
    showError(String(e.message || e));
  }
}

function renderDetailChrome(s) {
  const up = s.state ? s.state.process_up : null;
  const statusClass = up === true ? "up" : up === false ? "down" : "unknown";
  const statusText = up === true ? "up" : up === false ? "down" : "inconnu";
  detailStatus.innerHTML = `<span class="pill ${statusClass}"><span class="dot"></span>${esc(statusText)}</span>`;
  detailActions.innerHTML = "";
  for (const btn of buildActionButtons(s)) detailActions.appendChild(btn);
}

function renderDetailMods(s) {
  if (!("workshop_appid" in s)) {
    detailColumns.classList.add("single");
    detailMods.innerHTML = "";
    return;
  }
  detailColumns.classList.remove("single");
  const mods = s.mods || [];
  const restartBanner = s.mods_restart_required
    ? `<div class="mods-restart-banner">redémarrage requis pour appliquer les mods</div>`
    : "";
  const modRows = mods.map(m => {
    const st = modUpdateState(m);
    const dates = [];
    if (m.installed_at) dates.push(`installé ${relativeTime(m.installed_at)}`);
    if (m.steam_updated_at) dates.push(`publié sur Steam ${relativeTime(m.steam_updated_at)}`);
    // Bouton aussi pour un mod installe sans installed_at (installe avant la feature) :
    // etat inconnu -> permettre la re-installation, qui pose la date de reference.
    const updateBtn = (m.update_available || (m.installed && !m.installed_at))
      ? `<button class="mod-update-btn" onclick="updateMod('${esc(s.name)}', '${esc(m.workshop_id)}')">mettre à jour</button>`
      : "";
    return `
    <div class="mod-row">
      ${m.thumbnail_url ? `<img src="${esc(m.thumbnail_url)}" alt="">` : ""}
      <span class="mod-title">${esc(m.title)}<span class="mod-dates">${esc(dates.join(" · ") || "—")}</span></span>
      <span class="mod-status ${st.cls}">${st.label}</span>
      ${updateBtn}
      <button onclick="removeMod('${esc(s.name)}', '${esc(m.workshop_id)}')">retirer</button>
    </div>`;
  }).join("");
  const anyToUpdate = mods.some(m => m.update_available || (m.installed && !m.installed_at));
  const updateAllBtn = anyToUpdate
    ? `<button class="mod-update-btn mod-update-all" onclick="updateAllMods('${esc(s.name)}')">tout mettre à jour</button>`
    : "";
  const installedCount = mods.filter(m => m.installed).length;
  const needsUpdate = mods.filter(m => m.update_available).length;
  detailMods.innerHTML = `
    <div class="detail-section">
      <div class="mods-head"><h4>Mods (${installedCount} installés${needsUpdate ? ` · ${needsUpdate} maj dispo` : ""})</h4>${updateAllBtn}</div>
      ${restartBanner}
      ${modRows || `<div class="detail-empty">aucun mod</div>`}
      <div class="mod-add-row">
        <input type="text" id="mod-input-${esc(s.name)}" placeholder="lien ou ID Workshop">
        <button onclick="addMod('${esc(s.name)}')">ajouter</button>
      </div>
      <span class="workshop-toggle" onclick="toggleWorkshopBrowser('${esc(s.name)}')">parcourir le Workshop</span>
      <div class="workshop-browser" id="workshop-browser-${esc(s.name)}"></div>
    </div>`;
}

function simpleLineDiff(oldText, newText) {
  // LCS ligne-a-ligne fait main (pas de librairie externe -- le projet n'a ni build
  // step ni dependance JS). Suffisant pour des fichiers de config (dizaines/centaines
  // de lignes), pas un Myers diff optimise.
  const a = oldText.split("\n"), b = newText.split("\n");
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out.push({ type: "same", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: "del", text: a[i] }); i++; }
    else { out.push({ type: "add", text: b[j] }); j++; }
  }
  while (i < a.length) { out.push({ type: "del", text: a[i] }); i++; }
  while (j < b.length) { out.push({ type: "add", text: b[j] }); j++; }
  return out;
}

function fileTreeHtml(name, listing) {
  const roots = Object.keys(listing || {});
  if (roots.length === 0) {
    return `<p class="files-empty">Aucun listing pour l'instant.
      <button onclick="listFiles('${esc(name)}', 'install')">lister le dossier d'install</button></p>`;
  }
  return roots.map(root => `
    <div class="files-root">
      <strong>${esc(root)}</strong>
      <button onclick="listFiles('${esc(name)}', '${esc(root)}')">rafraichir</button>
      <ul>${(listing[root] || []).map(p => `
        <li><a href="#" class="file-open-link" data-server-name="${esc(name)}" data-root="${esc(root)}" data-path="${esc(p)}">${esc(p)}</a></li>`).join("")}
      </ul>
    </div>`).join("");
}

function fileEditorHtml(name, fileRead) {
  if (!openFileEditor || openFileEditor.name !== name) return "";
  const content = fileRead && fileRead.path === openFileEditor.path
    ? atob(fileRead.content_b64) : "";
  return `
    <div class="file-editor">
      <p>Edition de <code>${esc(openFileEditor.root)}/${esc(openFileEditor.path)}</code></p>
      <textarea id="file-editor-textarea" rows="16">${esc(content)}</textarea>
      <div class="file-editor-actions">
        <button onclick="previewFileDiff('${esc(name)}')">Aperçu</button>
        <button onclick="saveFile('${esc(name)}', false)">Enregistrer</button>
        <button onclick="saveFile('${esc(name)}', true)">Enregistrer et redémarrer</button>
      </div>
      <div id="file-diff-preview"></div>
    </div>`;
}

function renderFilesSection(s) {
  // Admin only (revue finale 18/07) : un fichier .ini legitime peut contenir un
  // secret (AdminPassword Palworld) -- meme regle que renderDetailConfig, jamais
  // expose a un "user" meme sur son propre serveur assigne.
  if (!s || !currentUser || currentUser.role !== "admin") return "";
  return `
    <div class="detail-section files-section">
      <h3>Fichiers de config</h3>
      ${fileTreeHtml(s.name, s.files_listing)}
      ${fileEditorHtml(s.name, s.file_read)}
    </div>`;
}

function renderDetailFiles(s) {
  detailFiles.innerHTML = renderFilesSection(s);
}

async function listFiles(name, root) {
  try {
    const res = await apiCall(`/api/servers/${name}/files/list`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root }),
    });
    if (!res.ok) showError(`Erreur listing (${res.status}).`);
    await loadServerDetail(name);
  } catch (e) { showError(String(e.message || e)); }
}

async function openFile(name, root, path) {
  openFileEditor = { name, root, path, sha256: null };
  try {
    const res = await apiCall(`/api/servers/${name}/files/read`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, path }),
    });
    if (!res.ok) showError(`Erreur lecture (${res.status}).`);
    await loadServerDetail(name);
  } catch (e) { showError(String(e.message || e)); }
}

function previewFileDiff(name) {
  if (!openFileEditor) return;
  const textarea = document.getElementById("file-editor-textarea");
  const s = latestServers.find(x => x.name === name);
  const original = s && s.file_read && s.file_read.path === openFileEditor.path
    ? atob(s.file_read.content_b64) : "";
  const diff = simpleLineDiff(original, textarea.value);
  const el = document.getElementById("file-diff-preview");
  if (!el) return;
  el.innerHTML = diff.map(l => {
    const cls = l.type === "add" ? "diff-add" : l.type === "del" ? "diff-del" : "diff-same";
    const prefix = l.type === "add" ? "+ " : l.type === "del" ? "- " : "  ";
    return `<div class="${cls}">${esc(prefix + l.text)}</div>`;
  }).join("");
}

async function saveFile(name, andRestart) {
  if (!openFileEditor) return;
  const s = latestServers.find(x => x.name === name);
  const expectedSha256 = s && s.file_read && s.file_read.path === openFileEditor.path
    ? s.file_read.sha256 : null;
  if (!expectedSha256) { showError("Relisez le fichier avant d'enregistrer."); return; }
  const content = document.getElementById("file-editor-textarea").value;
  try {
    const res = await apiCall(`/api/servers/${name}/files/write`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        root: openFileEditor.root, path: openFileEditor.path,
        content_b64: btoa(content), expected_sha256: expectedSha256,
      }),
    });
    if (res.status === 409 || res.status === 400) {
      showError("Le fichier a changé depuis sa lecture -- votre modification n'a pas été enregistrée. Contenu rechargé.");
      await openFile(name, openFileEditor.root, openFileEditor.path);
      return;
    }
    if (!res.ok) { showError(`Erreur enregistrement (${res.status}).`); return; }
    showError("");
    if (andRestart) await apiCall(`/api/servers/${name}/restart`, { method: "POST" });
    await loadServerDetail(name);
  } catch (e) { showError(String(e.message || e)); }
}

function renderDetailFromLatest() {
  // Rejoue l'en-tete et la colonne mods de l'overlay depuis le dernier poll, en
  // preservant le brouillon du champ d'ajout et le navigateur Workshop ouvert
  // (meme classe de bug que restoreOpenPanels, 15/07).
  if (!detailServerName) return;
  const s = latestServers.find(x => x.name === detailServerName);
  if (!s) return;
  const input = document.getElementById(`mod-input-${detailServerName}`);
  const draft = input ? input.value : "";
  const fileTextarea = document.getElementById("file-editor-textarea");
  const fileDraft = fileTextarea ? fileTextarea.value : null;
  renderDetailChrome(s);
  renderDetailMods(s);
  if (lastFilesData && lastFilesData.name === detailServerName) renderDetailFiles(lastFilesData);
  const newInput = document.getElementById(`mod-input-${detailServerName}`);
  if (newInput && draft) newInput.value = draft;
  const newFileTextarea = document.getElementById("file-editor-textarea");
  if (newFileTextarea && fileDraft !== null && openFileEditor) newFileTextarea.value = fileDraft;
  reopenWorkshopBrowser(detailServerName);
}

detailClose.addEventListener("click", closeServerDetail);
detailRefresh.addEventListener("click", () => { if (detailServerName) loadServerDetail(detailServerName); });
detailOverlay.addEventListener("click", (e) => { if (e.target === detailOverlay) closeServerDetail(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && detailOverlay.classList.contains("open")) closeServerDetail();
});

async function sendOrder(name, type) {
  const labels = { update: "mettre à jour", restart: "redémarrer", start: "démarrer", stop: "arrêter" };
  const label = labels[type];
  const confirmed = await confirmDialog(`Confirmer : ${label} ${name} ?`);
  if (!confirmed) return;
  try {
    const res = await apiCall(`/api/servers/${name}/${type}`, { method: "POST" });
    if (res.status === 409) {
      showError(`Un ordre ${label} est déjà en cours pour ${name}.`);
    } else if (!res.ok) {
      showError(`Erreur lors de l'envoi de l'ordre (${res.status}).`);
    } else {
      showError("");
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

async function cancelOrder(name, orderId) {
  const confirmed = await confirmDialog(`Annuler cet ordre en attente pour ${name} ?`);
  if (!confirmed) return;
  try {
    const res = await apiCall(`/api/servers/${name}/orders/${orderId}`, { method: "DELETE" });
    if (res.status === 409) {
      showError("L'agent a déjà commencé à exécuter cet ordre, annulation impossible.");
    } else if (!res.ok && res.status !== 404) {
      showError(`Erreur lors de l'annulation (${res.status}).`);
    } else {
      showError("");
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

async function addMod(name) {
  const input = document.getElementById(`mod-input-${name}`);
  const ref = input.value.trim();
  if (!ref) return;

  let preview;
  try {
    const res = await apiCall(`/api/servers/${name}/mods/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workshop_ref: ref }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showError(`Mod invalide : ${body.detail || res.status}`);
      return;
    }
    preview = await res.json();
  } catch (e) {
    showError(String(e.message || e));
    return;
  }

  const html = `
    <div class="mod-preview-box">
      ${preview.thumbnail_url ? `<img src="${esc(preview.thumbnail_url)}" alt="">` : ""}
      <div>
        <strong>${esc(preview.title)}</strong>
        <p>${esc((preview.description || "").slice(0, 200))}</p>
      </div>
    </div>
  `;
  const confirmed = await confirmDialog(html, true);
  if (!confirmed) return;

  try {
    const res = await apiCall(`/api/servers/${name}/mods`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workshop_id: preview.workshop_id, title: preview.title, thumbnail_url: preview.thumbnail_url,
      }),
    });
    if (res.status === 409) {
      showError("Une installation est déjà en attente pour ce mod.");
    } else if (!res.ok) {
      showError(`Erreur lors de l'installation (${res.status}).`);
    } else {
      showError("");
      input.value = "";
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

async function removeMod(name, workshopId) {
  const confirmed = await confirmDialog(`Confirmer : retirer le mod ${workshopId} de ${name} ?`);
  if (!confirmed) return;

  try {
    const res = await apiCall(`/api/servers/${name}/mods/${workshopId}`, { method: "DELETE" });
    if (res.status === 409) {
      showError("Une suppression est déjà en attente pour ce mod.");
    } else if (!res.ok) {
      showError(`Erreur lors de la suppression (${res.status}).`);
    } else {
      showError("");
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

async function updateMod(name, workshopId) {
  const server = (latestServers || []).find(s => s.name === name);
  const mod = server && (server.mods || []).find(m => m.workshop_id === workshopId);
  const title = mod ? mod.title : workshopId;
  const confirmed = await confirmDialog(
    `Confirmer : mettre à jour le mod « ${title} » vers la dernière version Workshop ? (redémarrage du serveur requis ensuite)`);
  if (!confirmed) return;

  try {
    // Réutilise l'ordre install_mod : idempotent côté agent (re-télécharge la
    // dernière version Workshop et remplace le dossier). Le backend revalide
    // l'ID auprès de Steam, title/thumbnail client ne sont pas de confiance.
    const res = await apiCall(`/api/servers/${name}/mods`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workshop_id: workshopId,
        title: title,
        thumbnail_url: (mod && mod.thumbnail_url) || "",
      }),
    });
    if (res.status === 409) {
      showError("Une installation est déjà en attente pour ce mod.");
    } else if (!res.ok) {
      showError(`Erreur lors de la mise à jour (${res.status}).`);
    } else {
      showError("");
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

async function updateAllMods(name) {
  const confirmed = await confirmDialog(
    `Confirmer : mettre à jour tous les mods de ${name} en retard (ou sans date de référence), puis redémarrer le serveur ?`);
  if (!confirmed) return;

  try {
    const res = await apiCall(`/api/servers/${name}/mods/update-all`, { method: "POST" });
    if (!res.ok) {
      showError(`Erreur lors de la mise à jour groupée (${res.status}).`);
    } else {
      const data = await res.json();
      showError(data.orders_created === 0 ? "Aucun mod à mettre à jour." : "");
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

const workshopBrowserState = {};

function toggleWorkshopBrowser(name) {
  const el = document.getElementById(`workshop-browser-${name}`);
  if (!el) return;
  const isOpen = el.classList.contains("open");
  if (isOpen) {
    el.classList.remove("open");
    el.innerHTML = "";
    delete workshopBrowserState[name];
    return;
  }
  el.classList.add("open");
  workshopBrowserState[name] = { query: "", sort: "trend", page: 1, selected: null };
  renderWorkshopBrowser(name);
}

function reopenWorkshopBrowser(name) {
  const state = workshopBrowserState[name];
  const el = document.getElementById(`workshop-browser-${name}`);
  if (!state || !el) return;
  el.classList.add("open");
  renderWorkshopBrowserShell(name);
  if (state.results) {
    renderWorkshopResultsGrid(name, state.results);
    if (state.selected) selectWorkshopResult(name, state.selected.workshop_id);
  } else {
    fetchWorkshopResults(name);
  }
}

function renderWorkshopBrowserShell(name) {
  const el = document.getElementById(`workshop-browser-${name}`);
  if (!el) return;
  const state = workshopBrowserState[name];

  el.innerHTML = `
    <div class="workshop-search-row">
      <input type="text" id="workshop-query-${name}" placeholder="rechercher..." value="${esc(state.query)}">
      <select id="workshop-sort-${name}">
        <option value="trend" ${state.sort === "trend" ? "selected" : ""}>tendance</option>
        <option value="recent" ${state.sort === "recent" ? "selected" : ""}>recents</option>
        <option value="text" ${state.sort === "text" ? "selected" : ""}>recherche texte</option>
      </select>
    </div>
    <div class="workshop-split">
      <div>
        <div class="workshop-grid" id="workshop-grid-${name}"><div class="workshop-empty">chargement...</div></div>
        <div class="workshop-pager">
          <button id="workshop-prev-${name}" ${state.page <= 1 ? "disabled" : ""}>precedent</button>
          <button id="workshop-next-${name}">suivant</button>
        </div>
      </div>
      <div class="workshop-detail" id="workshop-detail-${name}">
        <div class="workshop-empty">clique un mod pour voir le detail</div>
      </div>
    </div>
  `;

  document.getElementById(`workshop-query-${name}`).addEventListener("change", (e) => {
    state.query = e.target.value;
    state.sort = state.query ? "text" : "trend";
    state.page = 1;
    fetchWorkshopResults(name);
  });
  document.getElementById(`workshop-sort-${name}`).addEventListener("change", (e) => {
    state.sort = e.target.value;
    state.page = 1;
    fetchWorkshopResults(name);
  });
  document.getElementById(`workshop-prev-${name}`).addEventListener("click", () => {
    if (state.page > 1) { state.page -= 1; fetchWorkshopResults(name); }
  });
  document.getElementById(`workshop-next-${name}`).addEventListener("click", () => {
    state.page += 1; fetchWorkshopResults(name);
  });
}

function renderWorkshopBrowser(name) {
  renderWorkshopBrowserShell(name);
  return fetchWorkshopResults(name);
}

function renderWorkshopResultsGrid(name, results) {
  const grid = document.getElementById(`workshop-grid-${name}`);
  const nextBtn = document.getElementById(`workshop-next-${name}`);
  const prevBtn = document.getElementById(`workshop-prev-${name}`);
  if (!grid) return;
  const state = workshopBrowserState[name];

  if (results.length === 0) {
    grid.innerHTML = `<div class="workshop-empty">aucun mod trouve</div>`;
    if (prevBtn) prevBtn.disabled = state.page <= 1;
    if (nextBtn) nextBtn.disabled = true;
    return;
  }
  grid.innerHTML = results.map(r => `
    <div class="workshop-thumb" data-wid="${esc(r.workshop_id)}" onclick="selectWorkshopResult('${esc(name)}', '${esc(r.workshop_id)}')">
      ${r.thumbnail_url ? `<img src="${esc(r.thumbnail_url)}" alt="">` : ""}
      <div class="wt-info">
        <div class="wt-name">${esc(r.title)}</div>
        <div class="wt-subs">${esc(r.subscriptions)} abonnes</div>
      </div>
    </div>
  `).join("");
  if (prevBtn) prevBtn.disabled = state.page <= 1;
  if (nextBtn) nextBtn.disabled = results.length < 20;
}

async function fetchWorkshopResults(name) {
  const state = workshopBrowserState[name];
  const grid = document.getElementById(`workshop-grid-${name}`);
  if (!grid) return;

  grid.innerHTML = `<div class="workshop-empty">chargement...</div>`;

  const params = new URLSearchParams({ sort: state.sort, page: String(state.page) });
  if (state.query) params.set("q", state.query);

  try {
    const res = await apiCall(`/api/servers/${name}/mods/search?${params.toString()}`);
    if (res.status === 503) {
      grid.innerHTML = `<div class="workshop-empty">recherche Workshop non disponible (cle API non configuree)</div>`;
      return;
    }
    if (!res.ok) {
      grid.innerHTML = `<div class="workshop-empty">erreur de recherche, reessayer</div>`;
      return;
    }
    const data = await res.json();
    state.results = data.results;
    renderWorkshopResultsGrid(name, data.results);
  } catch (e) {
    grid.innerHTML = `<div class="workshop-empty">erreur de recherche, reessayer</div>`;
  }
}

function selectWorkshopResult(name, workshopId) {
  const state = workshopBrowserState[name];
  const result = (state.results || []).find(r => r.workshop_id === workshopId);
  if (!result) return;
  state.selected = result;

  document.querySelectorAll(`#workshop-grid-${name} .workshop-thumb`).forEach(el => {
    el.classList.toggle("active", el.dataset.wid === workshopId);
  });

  const detail = document.getElementById(`workshop-detail-${name}`);
  detail.innerHTML = `
    ${result.thumbnail_url ? `<img src="${esc(result.thumbnail_url)}" alt="">` : ""}
    <h4>${esc(result.title)}</h4>
    <div class="wd-subs">${esc(result.subscriptions)} abonnes</div>
    <p>${esc(result.description)}</p>
    <button onclick="installWorkshopResult('${esc(name)}', '${esc(workshopId)}')">Installer</button>
  `;
}

async function installWorkshopResult(name, workshopId) {
  const state = workshopBrowserState[name];
  const result = (state.results || []).find(r => r.workshop_id === workshopId);
  if (!result) return;

  const html = `
    <div class="mod-preview-box">
      ${result.thumbnail_url ? `<img src="${esc(result.thumbnail_url)}" alt="">` : ""}
      <div>
        <strong>${esc(result.title)}</strong>
        <p>${esc((result.description || "").slice(0, 200))}</p>
      </div>
    </div>
  `;
  const confirmed = await confirmDialog(html, true);
  if (!confirmed) return;

  try {
    const res = await apiCall(`/api/servers/${name}/mods`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workshop_id: result.workshop_id, title: result.title, thumbnail_url: result.thumbnail_url }),
    });
    if (res.status === 409) {
      showError("Une installation est deja en attente pour ce mod.");
    } else if (!res.ok) {
      showError(`Erreur lors de l'installation (${res.status}).`);
    } else {
      showError("");
    }
    await fetchServers();
  } catch (e) {
    showError(String(e.message || e));
  }
}

function renderGrid() {
  const query = searchBox.value.trim().toLowerCase();
  const filtered = latestServers.filter(s => s.display_name.toLowerCase().includes(query));
  const sorted = [...filtered].sort((a, b) => {
    const aUp = a.state && a.state.process_up === true;
    const bUp = b.state && b.state.process_up === true;
    if (aUp !== bUp) return aUp ? -1 : 1;
    return a.display_name.localeCompare(b.display_name);
  });
  grid.innerHTML = "";
  for (const s of sorted) grid.appendChild(renderCard(s));
  restoreOpenPanels();
  renderDetailFromLatest();
  const discoveredEl = document.getElementById("discovered");
  if (discoveredEl) discoveredEl.innerHTML = renderDiscoveredGames(discoveredGames);
}

function restoreOpenPanels() {
  for (const name of openPlayersDetail) restorePlayersDetail(name);
}

async function fetchServers() {
  try {
    const res = await apiCall("/api/servers");
    if (!res.ok) {
      showError(`Erreur serveur (${res.status}).`);
      return;
    }
    showError("");
    const data = await res.json();
    latestServers = data.servers;
    discoveredGames = data.discovered_games || [];
    agentVersion = data.agent_version || null;
    renderGrid();
  } catch (e) {
    showError(String(e.message || e));
  }
}

async function logout() {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login";
}

async function backupNow(name) {
  try {
    const res = await apiCall(`/api/servers/${name}/saves/backup`, { method: "POST" });
    if (res.status === 409) showError("Un backup est déjà en attente.");
    else if (!res.ok) showError(`Erreur backup (${res.status}).`);
    else showError("");
    await fetchServers();
  } catch (e) { showError(String(e.message || e)); }
}

async function restoreSave(name, file) {
  const confirmed = await confirmDialog(
    `⚠️ Restaurer « ${file} » sur ${name} ? La sauvegarde ACTUELLE sera remplacée (copie de sûreté prise avant), et le serveur sera arrêté puis redémarré.`);
  if (!confirmed) return;
  try {
    const res = await apiCall(`/api/servers/${name}/saves/restore`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file }),
    });
    if (res.status === 409) showError("Une restauration est déjà en attente.");
    else if (res.status === 403) showError("Restauration réservée aux administrateurs.");
    else if (!res.ok) showError(`Erreur restauration (${res.status}).`);
    else showError("");
    await fetchServers();
  } catch (e) { showError(String(e.message || e)); }
}

// --- identite + gestion des comptes (section admin) ---

let currentUser = null;
let accountsCache = [];
let accountEditorFor = null;    // username dont l'editeur d'acces est ouvert
let accountPasswordFor = null;  // username dont l'editeur de mot de passe est ouvert

async function fetchMe() {
  try {
    const res = await apiCall("/api/me");
    if (!res.ok) return;
    currentUser = await res.json();
    const nameEl = document.getElementById("userName");
    if (nameEl) nameEl.textContent = currentUser.username;
    if (currentUser.role === "admin") {
      const btn = document.getElementById("accountsBtn");
      if (btn) btn.style.display = "";
      const dbtn = document.getElementById("deployBtn");
      if (dbtn) dbtn.style.display = "";
    }
  } catch (e) { /* topbar minimale si /api/me echoue, le dashboard reste utilisable */ }
}

function serverCheckboxesHtml(prefix, checked) {
  return (latestServers || []).map(s => `
    <label><input type="checkbox" class="${prefix}-server" value="${esc(s.name)}"
      ${checked && checked.includes(s.name) ? "checked" : ""}> ${esc(s.display_name || s.name)}</label>`).join("");
}

function renderAccountFormServers() {
  const el = document.getElementById("accServers");
  if (!el) return;
  const role = document.getElementById("accRole").value;
  el.innerHTML = role === "admin" ? "" : serverCheckboxesHtml("acc", []);
}

function renderAccountsList() {
  const el = document.getElementById("accountsList");
  if (!el) return;
  el.innerHTML = accountsCache.map(u => {
    const servers = u.role === "admin" ? "tous les serveurs" : (u.servers.join(", ") || "aucun serveur");
    const isSelf = currentUser && u.username === currentUser.username;
    const editor = accountEditorFor === u.username ? `
      <div class="account-editor">
        <select id="edit-role-${esc(u.username)}">
          <option value="user" ${u.role === "user" ? "selected" : ""}>accès limité</option>
          <option value="admin" ${u.role === "admin" ? "selected" : ""}>administrateur</option>
        </select>
        ${serverCheckboxesHtml("edit", u.servers)}
        <button onclick="saveAccountAccess('${esc(u.username)}')">enregistrer</button>
      </div>` : "";
    const pwEditor = accountPasswordFor === u.username ? `
      <div class="account-editor">
        <input type="text" id="pw-input-${esc(u.username)}" placeholder="nouveau mot de passe (12 caractères min)" maxlength="128" autocomplete="off">
        <button onclick="saveAccountPassword('${esc(u.username)}')">enregistrer</button>
      </div>` : "";
    return `
    <div class="account-row">
      <span class="acc-name">${esc(u.username)}</span>
      <span class="account-role ${esc(u.role)}">${u.role === "admin" ? "admin" : "limité"}</span>
      <span class="account-servers-label">${esc(servers)}</span>
      <button onclick="toggleAccountEditor('${esc(u.username)}')">accès</button>
      <button onclick="togglePasswordEditor('${esc(u.username)}')">mdp</button>
      ${isSelf ? "" : `<button class="acc-delete" onclick="deleteAccount('${esc(u.username)}')">supprimer</button>`}
      ${editor}
      ${pwEditor}
    </div>`;
  }).join("") || `<div class="detail-empty">aucun compte</div>`;
}

function accountsError(msg) {
  const el = document.getElementById("accountsError");
  if (el) el.textContent = msg || "";
}

async function refreshAccounts() {
  const res = await apiCall("/api/users");
  if (!res.ok) { accountsError(`Erreur (${res.status}).`); return; }
  accountsCache = (await res.json()).users;
  renderAccountsList();
}

async function openAccounts() {
  accountEditorFor = null;
  accountsError("");
  renderAccountFormServers();
  document.getElementById("accountsOverlay").classList.add("open");
  await refreshAccounts();
}

function closeAccounts() {
  document.getElementById("accountsOverlay").classList.remove("open");
}

function toggleAccountEditor(username) {
  accountEditorFor = accountEditorFor === username ? null : username;
  accountPasswordFor = null;
  renderAccountsList();
}

function togglePasswordEditor(username) {
  accountPasswordFor = accountPasswordFor === username ? null : username;
  accountEditorFor = null;
  renderAccountsList();
}

function checkedServers(prefix) {
  return Array.from(document.querySelectorAll(`.${prefix}-server:checked`)).map(cb => cb.value);
}

async function createAccount() {
  const username = document.getElementById("accUsername").value.trim();
  const password = document.getElementById("accPassword").value;
  const role = document.getElementById("accRole").value;
  const servers = role === "admin" ? [] : checkedServers("acc");
  const res = await apiCall("/api/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role, servers }),
  });
  if (res.status === 409) { accountsError("Ce compte existe déjà."); return; }
  if (res.status === 422) { accountsError("Identifiant (2-32 car., lettres/chiffres/-_) ou mot de passe (12 car. min) invalide."); return; }
  if (!res.ok) { accountsError(`Erreur lors de la création (${res.status}).`); return; }
  accountsError("");
  document.getElementById("accUsername").value = "";
  document.getElementById("accPassword").value = "";
  await refreshAccounts();
}

async function deleteAccount(username) {
  const confirmed = await confirmDialog(`Confirmer : supprimer le compte ${username} ? Ses sessions seront révoquées immédiatement.`);
  if (!confirmed) return;
  const res = await apiCall(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
  if (!res.ok) { accountsError(`Suppression impossible (${res.status}).`); return; }
  accountsError("");
  await refreshAccounts();
}

async function saveAccountPassword(username) {
  const input = document.getElementById(`pw-input-${username}`);
  const password = input ? input.value : "";
  if (password.length < 12) { accountsError("Mot de passe trop court (12 caractères min)."); return; }
  const res = await apiCall(`/api/users/${encodeURIComponent(username)}/password`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) { accountsError(`Erreur (${res.status}).`); return; }
  accountsError("");
  accountPasswordFor = null;
  renderAccountsList();
}

async function saveAccountAccess(username) {
  const role = document.getElementById(`edit-role-${username}`).value;
  const servers = role === "admin" ? [] : checkedServers("edit");
  const res = await apiCall(`/api/users/${encodeURIComponent(username)}/access`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, servers }),
  });
  if (!res.ok) { accountsError(`Modification impossible (${res.status}).`); return; }
  accountsError("");
  accountEditorFor = null;
  await refreshAccounts();
}

searchBox.addEventListener("input", renderGrid);
fetchServers();
fetchMe();
setInterval(fetchServers, 10000);
