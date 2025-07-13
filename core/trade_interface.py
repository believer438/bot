from core.trade_executor import open_trade as real_open_trade, close_position as real_close_position
from core.state import state
from core.binance_client import check_position_open
import threading
from core.telegram_controller import send_telegram
from core.position_utils import sync_position
from core.config import symbol

position_lock = threading.Lock()  # Verrou pour accès thread-safe

MAX_RETRIES = 3
RETRY_DELAY = 2

def open_trade(direction, quantity=None, leverage=None):
    """
    Ouvre une position dans la direction donnée, avec quantité et levier personnalisés si fournis.
    - Ferme la position existante si besoin (logique EMA cross).
    - Vérifie la cohérence locale/Binance (point 1, 2, 6).
    - Retry sur erreur temporaire (point 3, 7, 8).
    - Notifie tout problème sur Telegram (point 5).
    """
    try:
        sync_position()
        with position_lock:
            # Vérification croisée local/Binance
            local_open = state.position_open
            real_open = check_position_open(symbol=symbol)
            if local_open or real_open:
                msg = "⚠️ Une position est déjà ouverte (local ou Binance). Tentative de fermeture avant ouverture."
                print(msg)
                send_telegram(msg)
                # Tentative de fermeture propre
                for attempt in range(MAX_RETRIES):
                    try:
                        real_close_position()
                        state.position_open = False
                        send_telegram("✅ Position précédente fermée automatiquement avant ouverture.")
                        break
                    except Exception as close_e:
                        send_telegram(f"❌ Erreur fermeture auto (tentative {attempt+1}): {close_e}")
                        if attempt < MAX_RETRIES - 1:
                            import time; time.sleep(RETRY_DELAY)
                        else:
                            send_telegram("❌ Impossible de fermer la position précédente. Ouverture annulée.")
                            return

            # Tentative d'ouverture avec retry
            for attempt in range(MAX_RETRIES):
                try:
                    # Passe quantity et leverage à real_open_trade
                    real_open_trade(direction, quantity=quantity, leverage=leverage)
                    state.position_open = True
                    return
                except Exception as open_e:
                    send_telegram(f"❌ Erreur ouverture trade (tentative {attempt+1}): {open_e}")
                    if attempt < MAX_RETRIES - 1:
                        import time; time.sleep(RETRY_DELAY)
                    else:
                        send_telegram("❌ Toutes les tentatives d'ouverture ont échoué.")
                        return

    except Exception as e:
        err_msg = f"❌ Erreur générale open_trade : {e}"
        print(err_msg)
        send_telegram(err_msg)

def close_position():
    """
    Ferme la position ouverte s'il y en a une.
    - Vérifie la cohérence local/Binance (point 1, 2, 6).
    - Retry sur erreur temporaire (point 3, 7, 8).
    - Notifie tout problème sur Telegram (point 5).
    """
    try:
        sync_position()
        with position_lock:
            local_open = state.position_open
            real_open = check_position_open(symbol=symbol)
            if not local_open and not real_open:
                msg = "⚠️ Aucune position ouverte à fermer (local ou Binance)."
                print(msg)
                send_telegram(msg)
                return

            for attempt in range(MAX_RETRIES):
                try:
                    real_close_position()
                    state.position_open = False
                    print("✅ Position fermée proprement")
                    return
                except Exception as close_e:
                    send_telegram(f"❌ Erreur fermeture trade (tentative {attempt+1}): {close_e}")
                    if attempt < MAX_RETRIES - 1:
                        import time; time.sleep(RETRY_DELAY)
                    else:
                        send_telegram("❌ Toutes les tentatives de fermeture ont échoué.")
                        return
    except Exception as e:
        err_msg = f"❌ Erreur générale close_position : {e}"
        print(err_msg)
        send_telegram(err_msg)