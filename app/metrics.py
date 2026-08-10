"""Endpoint /metrics au format d'exposition Prometheus (texte 0.0.4) : les
metriques par serveur deja collectees par l'agent (process_up, cpu/mem, joueurs,
buildid, disjoncteur crash, file d'ordres) etaient jusqu'ici visibles seulement en
instantane via /api/servers -- aucun historique, aucune tendance. Reutilise le
Prometheus/Grafana deja deployes sur Vidar, pas de nouveau service a exploiter.

Public (comme /api/public/health/{name} dans app/main.py) : le port n'est expose
que sur le tailnet, et Prometheus scrape sans gerer de cookie de session -- meme
modele de confiance que la sonde Kuma existante. Aucune donnee sensible exposee
(noms de serveur et compteurs seulement, jamais rcon/mots de passe/contenu de
fichier).
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response

router = APIRouter()

Sample = tuple[dict[str, str], float]


def _render(name: str, help_text: str, samples: list[Sample]) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")
    return "\n".join(lines)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    store, steam = request.app.state.store, request.app.state.steam
    snap = await store.snapshot()
    pending = await store.pending_orders()
    registry = await store.registry.all()
    crash_recovery = snap.get("crash_recovery", {})

    up: list[Sample] = []
    players: list[Sample] = []
    cpu: list[Sample] = []
    mem: list[Sample] = []
    uptime: list[Sample] = []
    update_available: list[Sample] = []
    breaker: list[Sample] = []
    orders_by_server: list[Sample] = []

    for name, cfg in registry.items():
        labels = {"server": name}
        pending_count = sum(1 for o in pending if o["server"] == name)
        orders_by_server.append((labels, pending_count))

        breaker_tripped = bool(crash_recovery.get(name, {}).get("breaker_tripped_at"))
        breaker.append((labels, 1 if breaker_tripped else 0))

        state = snap["servers"].get(name)
        if state is None:
            continue
        if state.get("process_up") is not None:
            up.append((labels, 1 if state["process_up"] else 0))
        if state.get("players") is not None:
            players.append((labels, state["players"]))
        if state.get("process_cpu_percent") is not None:
            cpu.append((labels, state["process_cpu_percent"]))
        if state.get("process_mem_mb") is not None:
            mem.append((labels, state["process_mem_mb"]))

        started = state.get("process_started_at")
        if started and state.get("process_up"):
            started_dt = datetime.fromisoformat(started)
            uptime.append((labels, int((datetime.now(UTC) - started_dt).total_seconds())))

        local = state.get("buildid")
        public = await steam.public_buildid(cfg["server_appid"])
        if local and public:
            update_available.append((labels, 0 if local == public else 1))

    blocks = [
        _render("hephaestos_server_up",
                "Process reporte up (1) ou down (0) par l'agent.", up),
        _render("hephaestos_server_players",
                "Nombre de joueurs connectes.", players),
        _render("hephaestos_server_cpu_percent",
                "CPU du process, pourcentage rapporte par l'agent.", cpu),
        _render("hephaestos_server_mem_mb",
                "Memoire du process, en Mo.", mem),
        _render("hephaestos_server_uptime_seconds",
                "Duree depuis le dernier demarrage confirme.", uptime),
        _render("hephaestos_server_update_available",
                "1 si une MAJ Steam est disponible (buildid local != public).", update_available),
        _render("hephaestos_server_crash_breaker_tripped",
                "1 si le disjoncteur auto-reboot sur crash est declenche.", breaker),
        _render("hephaestos_server_orders_pending",
                "Ordres en attente ou en cours pour ce serveur.", orders_by_server),
        _render("hephaestos_orders_pending_total",
                "Ordres en attente ou en cours, tous serveurs confondus.",
                [({}, len(pending))]),
    ]

    meta = await store.get_agent_meta()
    reported_at = meta.get("reported_at")
    if reported_at:
        age = (datetime.now(UTC) - datetime.fromisoformat(reported_at)).total_seconds()
        blocks.append(_render(
            "hephaestos_agent_last_seen_seconds",
            "Anciennete du dernier rapport agent, tous serveurs confondus.",
            [({}, round(age, 1))]))

    body = "\n".join(blocks) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
