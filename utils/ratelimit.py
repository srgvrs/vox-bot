"""Límite de uso por usuario (por ejemplo, N descargas por hora)."""
import logging
from datetime import date

logger = logging.getLogger(__name__)


class DailyUserLimiter:
    """Contador en memoria de acciones por usuario y día (UTC).

    No persiste entre reinicios; suficiente para limitar abuso básico.
    """

    def __init__(self, max_per_day: int) -> None:
        self.max_per_day = max_per_day
        self._counts: dict[tuple[int, date], int] = {}

    @staticmethod
    def _today() -> date:
        return date.today()

    def _prune(self, today: date) -> None:
        """Elimina entradas de días anteriores para no crecer sin límite."""
        stale = [key for key in self._counts if key[1] != today]
        for key in stale:
            del self._counts[key]

    def remaining(self, user_id: int) -> int:
        """Cuántas acciones le quedan al usuario hoy."""
        today = self._today()
        used = self._counts.get((user_id, today), 0)
        return max(self.max_per_day - used, 0)

    def is_allowed(self, user_id: int) -> bool:
        """Indica si el usuario puede realizar otra acción hoy."""
        return self.remaining(user_id) > 0

    def consume(self, user_id: int) -> bool:
        """Registra una acción si hay cupo. Devuelve True si se permitió."""
        today = self._today()
        self._prune(today)
        key = (user_id, today)
        used = self._counts.get(key, 0)
        if used >= self.max_per_day:
            return False
        self._counts[key] = used + 1
        return True
