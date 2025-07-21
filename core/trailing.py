import time
import threading
import traceback
from binance.enums import SIDE_BUY, SIDE_SELL
from core.binance_client import client, check_position_open
from core.telegram_controller import send_telegram
from core.trading_utils import update_trade_status
from core.config import symbol, take_profit_pct  # <-- Import centralisé
from core.state import state  # <-- Import de l'état global si besoin

order_lock = threading.Lock()

# === Calcul du SL dynamique selon le gain atteint ===
def get_trailing_sl(entry_price, current_price, direction):
    gain_pct = (current_price - entry_price) / entry_price if direction == "bullish" else (entry_price - current_price) / entry_price
    sl_level = None
    levels = [
        (0.005, 0.002),
        (0.006, 0.003),
        (0.010, 0.005),
        (0.012, 0.006),
        (0.015, 0.010),
    ]
    for threshold, sl in levels:
        if gain_pct >= threshold:
            sl_level = sl
    if gain_pct >= 0.015:
        extra_gain = gain_pct - 0.015
        steps = int(extra_gain / 0.005)
        sl_level = 0.010 + steps * 0.005

    if sl_level:
        if direction == "bullish":
            new_sl = round(entry_price * (1 + sl_level), 4)
        else:
            new_sl = round(entry_price * (1 - sl_level), 4)

        # ✅ Distance minimale : au moins 0.1% entre prix actuel et SL
        min_distance = entry_price * 0.001
        if abs(current_price - new_sl) < min_distance:
            return None
        return new_sl
    return None

# === Calcul du TP dynamique selon le gain ===
def get_trailing_tp(entry_price, current_price, direction, current_tp_pct):
    gain_pct = (current_price - entry_price) / entry_price if direction == "bullish" else (entry_price - current_price) / entry_price
    tp_levels = [
        (0.012, 0.02),
        (0.018, 0.025),
    ]
    new_tp_pct = take_profit_pct
    for threshold, new_tp in tp_levels:
        if gain_pct >= threshold:
            new_tp_pct = new_tp
    if gain_pct > 0.018:
        extra_gain = gain_pct - 0.018
        steps = int(extra_gain / 0.005)
        new_tp_pct = 0.025 + steps * 0.005
    if new_tp_pct > current_tp_pct:
        return new_tp_pct
    return None

# === Suivi dynamique du SL et TP ===
def update_trailing_sl_and_tp(direction, entry_price):
    t = threading.current_thread()
    current_sl = None
    current_tp_pct = take_profit_pct
    max_gain_pct_notified = 0
    trailing_sl_order_id = None
    trailing_tp_order_id = None

    try:
        while getattr(t, "do_run", True):
            try:
                current_price = float(client.futures_mark_price(symbol=symbol)["markPrice"])
            except Exception as e:
                send_telegram(f"❌ Erreur récupération prix : {e}")
                traceback.print_exc()
                time.sleep(15)
                continue

            gain_pct = (current_price - entry_price) / entry_price * 100 if direction == "bullish" else (entry_price - current_price) / entry_price * 100

            # ⛔ Vérifie si position toujours ouverte (local ET Binance si besoin)
            if not check_position_open(symbol=symbol) or not state.position_open:
                send_telegram(" 💎 Fin du suivi dynamique SL/TP. 🙌")
                break

            # 📢 Notifie à chaque +1%
            next_threshold = max_gain_pct_notified + 1
            if gain_pct >= next_threshold:
                max_gain_pct_notified = next_threshold
                send_telegram(f"📊 Gain +{next_threshold:.0f}% atteint ({direction.upper()} - {current_price}$ 🤗)")

            # 🛡 Mise à jour SL (n'annule que son propre ordre)
            new_sl = get_trailing_sl(entry_price, current_price, direction)
            if new_sl and (current_sl is None or
                (direction == "bullish" and new_sl > current_sl) or
                (direction == "bearish" and new_sl < current_sl)):

                with order_lock:
                    try:
                        # Annule uniquement l'ordre SL posé par le trailing (si existe)
                        if trailing_sl_order_id:
                            try:
                                client.futures_cancel_order(symbol=symbol, orderId=trailing_sl_order_id)
                            except Exception as e:
                                if "code=-2011" in str(e):
                                    print(f"Ordre SL trailing déjà annulé ou exécuté (id: {trailing_sl_order_id})")
                                else:
                                    raise
                        sl_order = client.futures_create_order(
                            symbol=symbol,
                            side="SELL" if direction == "bullish" else "BUY",
                            type="STOP_MARKET",
                            stopPrice=new_sl,
                            closePosition=True,
                            timeInForce="GTC"
                        )
                        trailing_sl_order_id = sl_order["orderId"]
                        current_sl = new_sl
                        print(f"🔵 SL trailing mis à jour à {new_sl}$ (orderId: {trailing_sl_order_id})")
                        send_telegram(f"🔵 Stop Loss dynamique mis à jour à {new_sl}$🎉...🥳")
                    except Exception as e:
                        send_telegram(f"❌ Erreur création SL dynamique : {e}")
                        traceback.print_exc()

            # 🎯 Mise à jour TP (n'annule que son propre ordre)
            new_tp_pct = get_trailing_tp(entry_price, current_price, direction, current_tp_pct)
            if new_tp_pct:
                new_tp_price = round(entry_price * (1 + new_tp_pct), 4) if direction == "bullish" else round(entry_price * (1 - new_tp_pct), 4)

                with order_lock:
                    try:
                        # Annule TOUS les TP existants (pour éviter qu'un TP plus bas soit exécuté avant)
                        orders = client.futures_get_open_orders(symbol=symbol)
                        for o in orders:
                            if o["type"] == "TAKE_PROFIT_MARKET" and o.get("closePosition", False):
                                try:
                                    client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                                except Exception as e:
                                    if "code=-2011" in str(e):
                                        print(f"Ordre TP déjà annulé ou exécuté (id: {o['orderId']})")
                                    else:
                                        raise

                        tp_order = client.futures_create_order(
                            symbol=symbol,
                            side="SELL" if direction == "bullish" else "BUY",
                            type="TAKE_PROFIT_MARKET",
                            stopPrice=new_tp_price,
                            closePosition=True,
                            timeInForce="GTC"
                        )
                        trailing_tp_order_id = tp_order["orderId"]
                        current_tp_pct = new_tp_pct
                        print(f"🎯 TP trailing mis à jour à {new_tp_price}$ (orderId: {trailing_tp_order_id})")
                        send_telegram(f"🎯 Take Profit dynamique mis à jour à {new_tp_price}$ 🥂💰")
                    except Exception as e:
                        send_telegram(f"❌ Erreur création TP dynamique : {e}")
                        traceback.print_exc()

            time.sleep(15)

    except Exception as e:
        send_telegram(f"❌ Erreur générale trailing : {e}")
        traceback.print_exc()

# === Vérifie si TP atteint (autre thread) ===
def wait_for_tp_or_exit(direction, entry_price, tp):
    from core.config import symbol  # <-- Import centralisé (utile si ce fichier est utilisé ailleurs)
    from core.state import state    # <-- Import de l'état global si besoin (déjà importé en haut normalement)
    t = threading.current_thread()
    try:
        while getattr(t, "do_run", True):
            try:
                price = float(client.futures_mark_price(symbol=symbol)["markPrice"])
            except Exception as e:
                print(f"❌ Erreur récupération prix dans wait_for_tp_or_exit : {e}")
                traceback.print_exc()
                time.sleep(10)
                continue

            # On peut ici aussi vérifier l'état local si besoin :
            if not state.position_open:
                send_telegram("⛔ Position fermée localement. Arrêt du suivi TP.")
                break

            if (direction == "bullish" and price >= tp) or (direction == "bearish" and price <= tp):
                update_trade_status(entry_price, "FERMÉ - TP")
                send_telegram(f"✅ Take Profit atteint à {price}$")
                break
            time.sleep(15)
    except Exception as e:
        print("❌ Erreur dans wait_for_tp_or_exit :", e)
        traceback.print_exc()
