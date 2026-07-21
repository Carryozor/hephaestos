from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    agent_token: str
    data_dir: Path
    servers: dict = field(default_factory=dict)  # name -> {display_name, server_appid}
    steam_api_key: str | None = None
    alert_webhook: str | None = None  # webhook Discord-compatible notifie sur ordre failed
