"""Notification d'alerte vers un webhook Discord-compatible (pont TeamSpeak).

Best-effort strict : jamais d'exception propagee (une panne du webhook ne doit
jamais faire echouer le rapport d'ordre de l'agent), timeout court.
"""
import logging

from fastapi import FastAPI

logger = logging.getLogger(__name__)

ALERT_TIMEOUT_SECONDS = 5


async def send_alert(app: FastAPI, text: str) -> None:
    url = app.state.settings.alert_webhook
    if not url:
        return
    try:
        resp = await app.state.http_client.post(url, json={"content": text},
                                                timeout=ALERT_TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            logger.warning("alerte webhook refusee (%s): %s", resp.status_code, text)
    except Exception:
        logger.warning("alerte webhook non envoyee: %s", text, exc_info=True)
