"""
Module : position_utils.py
But : Fournir une fonction pour synchroniser la position actuelle sur Binance
      avec l'état local du bot (state).
"""

from core.state import state
from core.binance_client import check_position_open
from core.config import symbol
from core.telegram_controller import send_telegram
from threading import Lock

# Lock pour éviter les conflits d'accès concurrentiels
position_lock = Lock()

def sync_position():
    """
    Synchronise l'état local avec la position réelle sur Binance.
    Met à jour state.position_open en fonction de l'info Binance.
    Gère les erreurs réseau/API.
    """
    try:
        with position_lock:
            pos_open = check_position_open(symbol=symbol)
            state.position_open = pos_open
    except Exception as e:
        err_msg = f"⚠️ Erreur lors de la synchronisation de position : {e}"
        print(err_msg)
        send_telegram(err_msg)
        state.position_open = False
