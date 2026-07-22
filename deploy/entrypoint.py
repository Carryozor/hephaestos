import json
import os
from pathlib import Path

import uvicorn

from app.config import Settings
from app.main import create_app


def load_settings() -> Settings:
    agent_token = os.environ.get("HEPHAESTOS_AGENT_TOKEN")
    if not agent_token:
        raise SystemExit("HEPHAESTOS_AGENT_TOKEN manquant")

    data_dir = Path(os.environ.get("HEPHAESTOS_DATA_DIR", "/data"))
    servers_file = Path(os.environ.get("HEPHAESTOS_SERVERS_FILE", "/data/servers.json"))
    if not servers_file.exists():
        raise SystemExit(f"fichier de config serveurs introuvable: {servers_file}")
    try:
        servers = json.loads(servers_file.read_text())
    except json.JSONDecodeError as e:
        # Sans ce garde-fou, un servers.json edite a la main et mal forme fait
        # planter le process avec une traceback brute -- et comme restart:unless-stopped
        # relance le conteneur a l'identique, ca boucle indefiniment sur la meme erreur.
        raise SystemExit(f"servers.json invalide ({servers_file}): {e}") from e
    steam_api_key = os.environ.get("STEAM_API_KEY")

    return Settings(agent_token=agent_token, data_dir=data_dir, servers=servers, steam_api_key=steam_api_key,
                    alert_webhook=os.environ.get("HEPHAESTOS_ALERT_WEBHOOK"))


if __name__ == "__main__":
    app = create_app(load_settings())
    # MONO-PROCESS OBLIGATOIRE : caches TTL module-level (app/mods.py) et
    # verrou asyncio du Store (state.json) ne sont pas partages entre workers.
    # Ne jamais ajouter workers=N ici sans refondre ces deux mecanismes.
    uvicorn.run(app, host="0.0.0.0", port=8710)
