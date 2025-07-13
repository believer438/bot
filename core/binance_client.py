import os
import time
from binance.client import Client
from dotenv import load_dotenv
import traceback
from core.notifier import send_telegram
from core.config import symbol  # <-- Import du symbole centralisé

# === Chargement des variables d’environnement (.env) ===
load_dotenv()

# === Gestion singleton client Binance ===
_client = None
def get_client():
    global _client
    if _client is None:
        API_KEY = os.getenv("BINANCE_API_KEY")
        API_SECRET = os.getenv("BINANCE_API_SECRET")
        if not API_KEY or not API_SECRET:
            err = "❌ Clés API Binance manquantes dans .env"
            print(err)
            send_telegram(err)
            raise ValueError(err)
        _client = Client(API_KEY, API_SECRET)
    return _client

client = get_client() 

def retry(func, max_retries: int = 3, delay: int = 3, verbose: bool = False):
    """
    Fonction utilitaire pour réessayer une fonction en cas d'exception.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            msg = f"❌ Tentative {attempt} échouée : {e}"
            print(msg)
            if verbose:
                send_telegram(msg)
            if attempt < max_retries:
                print(f"⏳ Nouvelle tentative dans {delay} secondes...")
                time.sleep(delay)
            else:
                print("🚫 Échec définitif après plusieurs tentatives.")
                if verbose:
                    send_telegram("🚫 Échec définitif après plusieurs tentatives.")
                raise

def check_futures_permissions() -> None:
    try:
        account_info = get_client().futures_account()
        if "canTrade" not in account_info or not account_info["canTrade"]:
            raise Exception("⚠ Les Futures ne sont pas activés sur ce compte Binance.")
        print("✅ Futures activés sur ce compte Binance.")
    except Exception as e:
        err_msg = f"❌ Erreur de permission Futures : {e}"
        print(err_msg)
        send_telegram(err_msg)
        raise

def sync_time() -> None:
    """
    Compare l'heure locale avec celle du serveur Binance (timestamps en secondes).
    Affiche un avertissement si l'écart est trop important.
    """
    try:
        server_time_ms = get_client().get_server_time()["serverTime"]
        server_time = server_time_ms // 1000  # Converti en secondes
        local_time = int(time.time())
        delta = server_time - local_time
        if abs(delta) > 2:
            warn = f"⚠️ Décalage horaire détecté : {delta} secondes (Synchronisez l'horloge système !)"
            print(warn)
            send_telegram(warn)
        else:
            print("⏰ Heure locale synchronisée avec Binance.")
    except Exception as e:
        err_msg = f"❌ Erreur lors de la synchronisation de l'heure : {e}"
        print(err_msg)
        send_telegram(err_msg)

def check_position_open(symbol: str = symbol) -> bool:  # <-- Utilisation du symbole centralisé par défaut
    try:
        positions = get_client().futures_position_information(symbol=symbol)
        for pos in positions:
            if float(pos["positionAmt"]) != 0:
                return True
        return False
    except Exception as e:
        err_msg = f"❌ Erreur check_position_open : {e}"
        print(err_msg)
        send_telegram(err_msg)
        return False

def change_leverage(symbol: str, leverage: int) -> bool:
    """
    Change le levier sur un symbole avec retries.
    Retourne True si succès, False sinon.
    """
    def try_change():
        get_client().futures_change_leverage(symbol=symbol, leverage=leverage)
        print(f"🔧 Levier mis à jour : x{leverage} sur {symbol}")

    try:
        retry(try_change, verbose=True)
        return True
    except Exception as e:
        err_msg = f"❌ Erreur changement levier : {e}"
        print(err_msg)
        send_telegram(err_msg)
        return False
    return False

# === Vérifie si un symbole est valide (optionnel) ===
def is_symbol_valid(symbol: str) -> bool:
    try:
        info = get_client().futures_exchange_info()
        return any(s['symbol'] == symbol for s in info['symbols'])
    except Exception as e:
        print(f"❌ Erreur vérification du symbole : {e}")
        return False