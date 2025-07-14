def open_trade(direction, quantity=None, leverage=None):
    """
    Ouvre une position sur Binance Futures avec effet de levier appliqué correctement.
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

        # 1. Lecture des paramètres utilisateur
        qty_usdt = float(quantity) if quantity is not None else float(get_quantity_from_file())
        lev = int(leverage) if leverage is not None else int(get_leverage_from_file())

        # 2. Applique le levier côté Binance AVANT la prise de position
        try:
            client.futures_change_leverage(symbol=symbol, leverage=lev)
        except Exception as e:
            send_telegram(f"❌ Erreur réglage levier : {e}")
            log_error(e)
            return

        # 3. Récupération du prix du marché
        try:
            price = get_price_with_retry(symbol, retries=3, delay=3)
        except Exception as e:
            send_telegram(f"❌ Erreur récupération prix : {e}")
            log_error(e)
            return

        # 4. Calcul de la quantité AVEC effet de levier
        qty = (qty_usdt * lev) / price
        qty = round_quantity(symbol, qty)

        if qty < 0.01:
            send_telegram("❌ Quantité trop faible : min 0.01")
            return

        # 5. Création de l'ordre
        side = "BUY" if direction == "bullish" else "SELL"
        try:
            order = retry_order_creation(lambda: client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty
            ), max_retries=3, delay=3)
        except Exception as e:
            send_telegram(f"❌ Erreur ouverture ordre : {e}")
            log_error(e)
            return

        # 6. Vérification post-ordre
        entry_price = float(order.get("avgFillPrice", price))
        if not check_position_open(symbol=symbol):
            send_telegram("❌ Trade non détecté après l’ordre.")
            return

        # 7. Mise à jour de l'état et notifications
        state.position_open = True
        state.current_direction = direction
        state.current_entry_price = entry_price
        state.current_quantity = qty

        send_telegram(
            f"✅ Position ouverte à {entry_price}$\n"
            f"Quantité: {qty} | Montant: {round(qty * price, 2)} USDT"
        )

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
        log_error(e)  voila ma fonction change ce qui dois etre changer et ajoute pour applique la logique que tu viens de m'envoyer 