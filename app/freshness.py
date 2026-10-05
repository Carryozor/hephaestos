"""Seuils de fraicheur de l'etat rapporte par l'agent (servers[name]["last_seen"]),
definis a UN seul endroit : sonde Kuma, /metrics et auto-enqueue en dependent.

L'agent poste son etat au moins a chaque cycle (2 min).
"""
from datetime import UTC, datetime, timedelta

# Au-dela, l'etat n'est plus digne de confiance pour AFFICHER (sonde publique, /metrics) :
# 10 min = 5 cycles rates.
STATE_STALE_SECONDS = 600

# Au-dela, l'etat n'est plus digne de confiance pour AGIR (ordres automatiques) : un
# players==0 ou process_up=False lu dans un rapport vieux de plus de 5 min peut etre faux
# (joueur connecte depuis, process relance a la main). Plus strict que l'affichage.
STATE_FRESH_SECONDS = 300


def state_age(state: dict | None) -> timedelta | None:
    """Age du rapport d'etat, ou None si last_seen est absent/illisible."""
    if not state:
        return None
    try:
        last_seen = datetime.fromisoformat(state["last_seen"])
    except (KeyError, ValueError, TypeError):
        return None
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return datetime.now(UTC) - last_seen


def is_state_fresh(state: dict | None, max_seconds: int = STATE_FRESH_SECONDS) -> bool:
    """Vrai si le rapport est assez recent pour fonder une decision automatique.
    last_seen absent/illisible = non frais."""
    age = state_age(state)
    return age is not None and age <= timedelta(seconds=max_seconds)
