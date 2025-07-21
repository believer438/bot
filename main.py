import threading
import signal
import sys
from core.bot import launch_bot, stop_bot
from core.telegram_controller import start_bot, stop_telegram_bot

def main():
    print("🚀 Lancement du bot de trading et du contrôleur Telegram...")

    # Démarre le bot de trading dans un thread daemon
    bot_thread = threading.Thread(target=launch_bot, daemon=True)
    bot_thread.start()
    print("✅ Bot de trading lancé.")
    
    # Fonction pour gérer l'arrêt propre sur Ctrl+C
    def signal_handler(sig, frame):
        print("\n🔴 Arrêt demandé. Fermeture en cours...")
        stop_bot()
        stop_telegram_bot()
        sys.exit(0)

    # Liaison du signal SIGINT (Ctrl+C) à la fonction d'arrêt
    signal.signal(signal.SIGINT, signal_handler)

    # Le bot Telegram tourne dans le thread principal
    start_bot()  # <-- ici, plus dans un thread !

if __name__ == "__main__":
    main()
