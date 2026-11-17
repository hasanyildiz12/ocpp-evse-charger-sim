"""Energy register for the charge point.

The pilot board in the schematic has no energy meter, so energy is
*estimated* from the current offered on the control pilot:

    P = offered_current × nominal_voltage × phases   (only while the EV
                                                       is in state C)

The EV may draw less than it is offered, so this is an upper bound, not a
billing-grade measurement. Replace :class:`EstimatedMeter` with a real
meter driver (implementing :class:`IMeterReader`) before charging money.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

LogFn = Callable[[str, str], None]


class IMeterReader(ABC):
    @abstractmethod
    def read_wh(self) -> Optional[int]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def configure_logger(self, log_fn: LogFn) -> None:
        pass


class MockMeter(IMeterReader):
    """Deterministic meter for tests: +increment_wh on every read."""

    def __init__(self, initial_wh: int = 0, increment_wh: int = 500):
        self.initial_wh = initial_wh
        self.increment_wh = increment_wh
        self.call_count = 0
        self.log = lambda l, m: None

    def configure_logger(self, log_fn: LogFn) -> None:
        self.log = log_fn

    def read_wh(self) -> Optional[int]:
        val = self.initial_wh + (self.call_count * self.increment_wh)
        self.call_count += 1
        return val

    def close(self) -> None:
        pass


class EstimatedMeter(IMeterReader):
    """Energy register that integrates ``power_fn()`` (W) over time.

    ``power_fn`` is wired to :meth:`ChargePoint.estimated_power_w`, which
    returns offered current × voltage × phases while the EV charges and 0
    otherwise. The register is integrated on every read, so reads must
    happen regularly (the meter loop does this).
    """

    def __init__(self, power_fn: Optional[Callable[[], float]] = None,
                 initial_wh: float = 0.0, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._wh = float(initial_wh)
        self._last = clock()
        self.power_fn = power_fn or (lambda: 0.0)
        self.log: LogFn = lambda l, m: None

    def configure_logger(self, log_fn: LogFn) -> None:
        self.log = log_fn

    def read_wh(self) -> Optional[int]:
        now = self._clock()
        power = max(0.0, float(self.power_fn()))
        self._wh += power * (now - self._last) / 3600.0
        self._last = now
        return int(round(self._wh))

    def close(self) -> None:
        pass
