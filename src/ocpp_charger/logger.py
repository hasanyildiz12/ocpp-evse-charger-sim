"""Logger module for the OCPP Simulator."""

from datetime import datetime
import sys


class Logger:
    """ANSI-colored OCPP simulator logger.

    Produces the same visual output as the original sim1.py ``log()``
    function::

        HH:MM:SS.mmm  SEND  [BootNotification] {...}
    """

    # ANSI codes
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    CYAN   = "\033[36m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    DIM    = "\033[2m"

    DIRECTION_COLORS = {
        "SEND": CYAN,
        "RECV": GREEN,
        "INFO": DIM,
        "WARN": YELLOW,
        "ERR":  RED,
    }

    def _ts(self) -> str:
        """Timestamp matching original format: ``HH:MM:SS.mmm``."""
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def log(self, direction: str, msg: str) -> None:
        ts = self._ts()
        color = self.DIRECTION_COLORS.get(direction, self.RESET)
        print(
            f"{self.DIM}{ts}{self.RESET}  "
            f"{color}{self.BOLD}{direction:<4}{self.RESET}  "
            f"{msg}"
        )
        sys.stdout.flush()

    def __call__(self, direction: str, msg: str) -> None:
        """Shortcut so the logger can be passed as a plain callable."""
        self.log(direction, msg)
