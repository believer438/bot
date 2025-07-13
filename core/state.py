import threading

class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._position_open = False
        self._current_direction = None
        self._current_entry_price = None
        self._current_quantity = None
        self._current_position_id = None  # Pour futur tracking si nécessaire

    # === Propriété : position ouverte ===
    @property
    def position_open(self):
        with self._lock:
            return self._position_open

    @position_open.setter
    def position_open(self, value: bool):
        with self._lock:
            self._position_open = value

    # === Propriété : direction actuelle ===
    @property
    def current_direction(self):
        with self._lock:
            return self._current_direction

    @current_direction.setter
    def current_direction(self, value):
        with self._lock:
            self._current_direction = value

    # === Propriété : prix d'entrée actuel ===
    @property
    def current_entry_price(self):
        with self._lock:
            return self._current_entry_price

    @current_entry_price.setter
    def current_entry_price(self, value):
        with self._lock:
            self._current_entry_price = value

    # === Propriété : quantité actuelle ===
    @property
    def current_quantity(self):
        with self._lock:
            return self._current_quantity

    @current_quantity.setter
    def current_quantity(self, value):
        with self._lock:
            self._current_quantity = value

    # === Propriété : ID de position actuel ===
    @property
    def current_position_id(self):
        with self._lock:
            return self._current_position_id

    @current_position_id.setter
    def current_position_id(self, value):
        with self._lock:
            self._current_position_id = value

    # === Réinitialisation de tout l’état (utile après fermeture de position) ===
    def reset_all(self):
        with self._lock:
            self._position_open = False
            self._current_direction = None
            self._current_entry_price = None
            self._current_quantity = None
            self._current_position_id = None

    # === Export sous forme de dictionnaire (pour logs ou debug) ===
    def get_state(self):
        with self._lock:
            return {
                "position_open": self._position_open,
                "direction": self._current_direction,
                "entry_price": self._current_entry_price,
                "quantity": self._current_quantity,
                "position_id": self._current_position_id
            }

# ✅ Instance globale unique utilisée dans tout le bot
state = State()
