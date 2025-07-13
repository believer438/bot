import time
import traceback
from core.binance_client import client, check_position_open, change_leverage
from core.state import state
from core.config import symbol, default_leverage, default_quantity_usdt, stop_loss_pct, take_profit_pct
from core.trading_utils import (
    calculate_quantity,
    log_trade,
    get_leverage_from_file,
    get_quantity_from_file,
    retry_order,
)
from core.telegram_controller import send_telegram
from core.position_utils import sync_position
from core.trailing import update_trailing_sl_and_tp
import threading

# Initialisation des threads globaux
trailing_thread = None
tp_thread = None

position_lock = threading.Lock()
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

def safe_round(value, decimals=4):
    try:
        return round(float(value), decimals)
    except Exception:
        return value

# Ajout exceptions Binance
try:
    from binance.exceptions import BinanceAPIException, BinanceOrderException
except ImportError:
    BinanceAPIException = Exception
    BinanceOrderException = Exception

def log_error(e):
    try:
        with open("logs/errors.txt", "a") as f:
            f.write(f"{time.ctime()} - Erreur : {traceback.format_exc()}\n")
    except Exception:
        pass

def round_quantity(symbol, qty):
    # Pour ALGOUSDT, 1 décimale
    if symbol.upper() == "ALGOUSDT":
        return round(qty, 1)
    # Ajoute ici d'autres symboles si besoin
    return qty

def get_price_with_retry(symbol, retries=3, delay=2):
    """
    Récupère le prix du symbole avec plusieurs tentatives en cas d'échec réseau.
    """
    last_exception = None
    for i in range(retries):
        try:
            price_data = client.get_symbol_ticker(symbol=symbol)
            if "price" not in price_data:
                raise Exception("Prix non trouvé dans la réponse de Binance")
            return float(price_data["price"])
        except Exception as e:
            last_exception = e
            if i < retries - 1:
                time.sleep(delay)
    raise last_exception

def retry_order_creation(order_fn, max_retries=3, delay=2):
    last_exception = None
    for i in range(max_retries):
        try:
            return order_fn()
        except Exception as e:
            last_exception = e
            if i < max_retries - 1:
                time.sleep(delay)
    raise last_exception

def get_mode():
    # À adapter selon ta logique, ici on retourne toujours "trade"
    return "trade"

def start_thread(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t

def get_real_leverage(symbol):
    """
    Récupère le levier réellement appliqué sur le symbole donné.
    """
    try:
        info = client.futures_position_information(symbol=symbol)
        for pos in info:
            if float(pos["positionAmt"]) != 0:
                return int(float(pos["leverage"]))
        # Si aucune position ouverte, retourne le levier configuré
        return int(client.futures_leverage_bracket(symbol=symbol)[0]['initialLeverage'])
    except Exception as e:
        log_error(e)
        return None

# === OUVERTURE DE POSITION ==
def open_trade(direction, quantity=None, leverage=None):
    """
    Ouvre une position sur Binance Futures en utilisant EXACTEMENT 1$ de marge USDT,
    avec effet de levier personnalisé. Quantité d’ALGO calculée automatiquement.
    """
    try:
        sync_position()
        if state.position_open or check_position_open(symbol=symbol):
            send_telegram("⚠️ Une position est déjà ouverte. Fermeture avant nouvelle ouverture.")
            close_position()
            time.sleep(1)
            sync_position()
            if state.position_open or check_position_open(symbol=symbol):
                send_telegram("❌ Impossible de fermer la position précédente.")
                return

        # ✅ Lecture paramètres dynamiques
        usdt_margin = float(quantity) if quantity is not None else float(get_quantity_from_file())
        lev = int(leverage) if leverage is not None else int(get_leverage_from_file())

        # 🎯 Applique le levier
        try:
            client.futures_change_leverage(symbol=symbol, leverage=lev)
        except Exception as e:
            send_telegram(f"❌ Erreur levier : {e}")
            log_error(e)
            return

        # 📈 Récupère le prix du marché
        try:
            price = get_price_with_retry(symbol, retries=3, delay=3)
        except Exception as e:
            send_telegram(f"❌ Erreur prix : {e}")
            log_error(e)
            return

        # 📊 Calcul de la quantité
        position_value = usdt_margin * lev
        qty = round_quantity(symbol, position_value / price)

        # ✅ Vérifie minQty & minNotional
        exchange_info = client.futures_exchange_info()
        symbol_info = next(s for s in exchange_info['symbols'] if s['symbol'] == symbol)
        filters = {f['filterType']: f for f in symbol_info['filters']}
        min_qty = float(filters['LOT_SIZE']['minQty'])
        min_notional = float(filters['MIN_NOTIONAL']['notional'])
        step_size = float(filters['LOT_SIZE']['stepSize'])

        if qty < min_qty:
            qty = min_qty
            send_telegram(f"⚠️ Quantité ajustée à {qty} (minQty)")

        if qty * price < min_notional:
            qty = round((min_notional / price) / step_size) * step_size
            qty = round_quantity(symbol, qty)
            send_telegram(f"⚠️ Quantité ajustée pour respecter minNotional : {qty}")

        # 🏦 Vérifie le solde (au moins 1$ dispo)
        balance = client.futures_account_balance()
        usdt_balance = float(next(b for b in balance if b['asset'] == 'USDT')['availableBalance'])
        if usdt_balance < usdt_margin:
            send_telegram(f"❌ Solde insuffisant. Requis : {usdt_margin}$, dispo : {usdt_balance:.2f}$")
            return

        # 📤 Place l’ordre
        side = "BUY" if direction == "bullish" else "SELL"
        try:
            order = retry_order_creation(lambda: client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty
            ), max_retries=3, delay=3)
        except Exception as e:
            send_telegram(f"❌ Erreur création ordre : {e}")
            log_error(e)
            return

        # 🎯 Post-trade
        entry_price = float(order.get("avgFillPrice", price))
        if not check_position_open(symbol=symbol):
            send_telegram("❌ Aucune position détectée après l’ordre.")
            return

        # 🧠 State
        state.position_open = True
        state.current_direction = direction
        state.current_entry_price = entry_price
        state.current_quantity = qty

        send_telegram(
            f"✅ Position {'HAUSSE' if direction == 'bullish' else 'BAISSE'} ouverte à {entry_price}$\n"
            f"💰 Montant : {usdt_margin}$ ... Quantité: {qty} ALGO |\n⚙️ Levier: x{lev}\n"
        )


        # SL/TP et trailing
        set_initial_sl_tp(direction, entry_price, qty)

        global trailing_thread
        try:
            if trailing_thread and trailing_thread.is_alive():
                trailing_thread.do_run = False
                trailing_thread.join()
        except Exception as e:
            log_error(e)

        trailing_thread = threading.Thread(
            target=update_trailing_sl_and_tp,
            args=(direction, entry_price),
            daemon=True
        )
        trailing_thread.start()

        log_trade(
            direction,
            entry_price,
            entry_price * (1 - stop_loss_pct if direction == "bullish" else 1 + stop_loss_pct),
            entry_price * (1 + take_profit_pct if direction == "bullish" else 1 - take_profit_pct),
            "AUTO",
            status="OUVERT"
        )

    except Exception as e:
        send_telegram(f"❌ Erreur open_trade : {e}")
        log_error(e)

        
# === FERMETURE DE POSITION ===
def close_position():
    """
    Ferme la position ouverte s'il y en a une.
    Annule tous les ordres SL/TP restants après la fermeture.
    """
    try:
        sync_position()
        if not state.position_open and not check_position_open(symbol=symbol):
            send_telegram("⚠️ Aucune position ouverte à fermer.")
            return

        # Détermination du sens de clôture
        positions = client.futures_position_information(symbol=symbol)
        pos = next((p for p in positions if float(p["positionAmt"]) != 0), None)
        if not pos:
            send_telegram("⚠️ Aucune position détectée sur Binance.")
            return

        amt = float(pos["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)

        # Fermeture de la position
        try:
            client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty,
                reduceOnly=True
            )
        except BinanceOrderException as e:
            send_telegram(f"❌ Erreur d'ordre Binance : {e}")
            log_error(e)
            return
        except BinanceAPIException as e:
            send_telegram(f"❌ Erreur API Binance : {e}")
            log_error(e)
            return
        except Exception as e:
            send_telegram(f"❌ Erreur inconnue : {e}")
            log_error(e)
            return
        
        position_info = client.futures_position_information(symbol=symbol)
        qty = abs(float(pos['positionAmt']))
        entry_price = float(pos['entryPrice'])
        exit_price = float(pos['markPrice'])
        position_info = client.futures_position_information(symbol=symbol)
        try:
            lev = int(pos.get("leverage", 1))
        except (KeyError, ValueError):
            lev = "inconnu"
        position_value = qty * entry_price
        sens = "HAUSSE" if state.current_direction == "bullish" else "BAISSE"
        send_telegram(
            f"✅ La Position {sens} fermée à {exit_price:.4f}$\n"
            f"Quantité: {qty:.2f} | Prix d'Entrée: {entry_price:.4f}$\nLevier: x{lev}"
            f"... 💰 Montant : {position_value:.2f} USDT"
        )


        # Nettoyage des ordres SL/TP restants
        cancel_all_open_orders_if_no_position()

        # Mise à jour de l'état local
        state.reset_all()

        # Vérification de clôture effective
        time.sleep(1)
        if check_position_open(symbol=symbol):
            send_telegram("⚠️ La position semble toujours ouverte après la clôture. Vérifie manuellement.")

    except Exception as e:
        send_telegram(f"❌ Erreur close_position : {e}")
        log_error(e)

# === POSE SL/TP DE SÉCURITÉ SI ABSENT ===
def set_initial_sl_tp(direction, entry_price, qty):
    """
    Pose un SL et un TP si aucun n'est présent.
    """
    try:
        side_close = "SELL" if direction == "bullish" else "BUY"
        orders = client.futures_get_open_orders(symbol=symbol)
        sl_orders = [o for o in orders if o['type'] == "STOP_MARKET" and o['side'] == side_close and o.get('closePosition', False)]
        tp_orders = [o for o in orders if o['type'] == "TAKE_PROFIT_MARKET" and o['side'] == side_close and o.get('closePosition', False)]

        has_sl = len(sl_orders) > 0
        has_tp = len(tp_orders) > 0

        stop_price = entry_price * (1 - stop_loss_pct) if direction == "bullish" else entry_price * (1 + stop_loss_pct)
        take_profit = entry_price * (1 + take_profit_pct) if direction == "bullish" else entry_price * (1 - take_profit_pct)

        if not has_sl:
            retry_order(lambda: client.futures_create_order(
                symbol=symbol,
                side=side_close,
                type="STOP_MARKET",
                stopPrice=round(stop_price, 4),
                closePosition=True,
                timeInForce="GTC"
            ))
            send_telegram(f"🛡 Stop loss automatique à {round(stop_price, 4)}$")

        if not has_tp:
            retry_order(lambda: client.futures_create_order(
                symbol=symbol,
                side=side_close,
                type="TAKE_PROFIT_MARKET",
                stopPrice=round(take_profit, 4),
                closePosition=True,
                timeInForce="GTC"
            ))
            send_telegram(f"🎯 Take profit automatique à {round(take_profit, 4)}$")

        # Vérification création SL/TP
        orders = client.futures_get_open_orders(symbol=symbol)
        has_sl = any(o['type'] == "STOP_MARKET" for o in orders)
        has_tp = any(o['type'] == "TAKE_PROFIT_MARKET" for o in orders)
        if not (has_sl and has_tp):
            send_telegram("⚠️ SL/TP pas créés correctement. Vérifie manuellement.")

    except Exception as e:
        send_telegram(f"❌ Erreur pose SL/TP initial : {e}")
        log_error(e)

# === NETTOYAGE DES ORDRES SL/TP ORPHELINS ===
def cancel_all_open_orders_if_no_position():
    """
    Annule tous les ordres SL/TP restants UNIQUEMENT s'il n'y a plus de position ouverte.
    N'envoie un message Telegram que si au moins un ordre a été annulé.
    """
    try:
        positions = client.futures_position_information(symbol=symbol)
        has_position = any(float(p["positionAmt"]) != 0 for p in positions)
        if has_position:
            # Il y a une position ouverte, on ne touche à rien
            return
        # Sinon, on annule les ordres SL/TP restants
        open_orders = client.futures_get_open_orders(symbol=symbol)
        cancelled = 0
        for order in open_orders:
            if order['type'] in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
                try:
                    client.futures_cancel_order(symbol=symbol, orderId=order['orderId'])
                    cancelled += 1
                except Exception as e:
                    if "code=-2011" in str(e):
                        print(f"Ordre déjà annulé ou exécuté (id: {order['orderId']})")
                    else:
                        log_error(e)
                        raise
        if cancelled > 0:
            send_telegram(f"✅ {cancelled} ordre(s) SL/TP orphelin(s) ont été annulés car il n'y a plus de position ouverte.")
    except Exception as e:
        send_telegram(f"⚠️ Erreur lors de l'annulation des ordres sans position : {e}")
        log_error(e)

# === SYNCHRONISATION DE POSITION (exposé pour d'autres modules) ===
def sync_and_check_position():
    sync_position()
    return state.position_open or check_position_open(symbol=symbol)

def sltp_watchdog_loop():
    while True:
        try:
            cancel_all_open_orders_if_no_position()
        except Exception as e:
            log_error(e)
        time.sleep(10)

# Lance la surveillance au démarrage du module
watchdog_thread = threading.Thread(target=sltp_watchdog_loop, daemon=True)
watchdog_thread.start()