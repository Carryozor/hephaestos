"""Liste statique des serveurs dedies Steam connus, installables en anonyme (recherche
par nom dans le wizard de deploiement). Extraite le 19/07/2026 de la page Valve
Dedicated_Servers_List (fournie par l'utilisateur -- page protegee anti-bot, pas
d'acces automatise possible). A enrichir a la main si un nouveau jeu manque : la
page source n'a pas d'API, mise a jour manuelle du JSON uniquement.

Chemin via variable d'env, meme convention que HEPHAESTOS_SERVERS_FILE (deploy/entrypoint.py) :
les fichiers de config ne sont jamais copies dans l'image Docker, ils vivent dans /data
(volume monte, editable sur l'hote sans rebuild -- deploy/known-dedicated-servers.json
dans le repo est la source versionnee, copiee une fois vers /data/ au deploiement).
"""
import json
import os
from pathlib import Path

_cache: list[dict] | None = None


def _data_path() -> Path:
    return Path(os.environ.get("HEPHAESTOS_KNOWN_SERVERS_FILE", "/data/known-dedicated-servers.json"))


def _load() -> list[dict]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_data_path().read_text())
            if not isinstance(data, list):
                raise ValueError("known-dedicated-servers.json doit contenir une liste")
            _cache = data
        except (FileNotFoundError, ValueError):
            # Fichier absent/mal forme (deploiement pas encore fait, variable d'env mal
            # configuree, ou edition manuelle fautive) : degrade en liste vide plutot
            # que de faire planter la recherche entiere -- le repli boutique Steam
            # reste disponible.
            _cache = []
    return _cache


def search_known(term: str) -> list[dict]:
    needle = term.lower()
    results = []
    for e in _load():
        try:
            if needle in e["name"].lower():
                results.append(e)
        except (TypeError, KeyError, AttributeError):
            continue  # entree malformee (edition manuelle fautive) : ignoree, pas fatale
    return results
