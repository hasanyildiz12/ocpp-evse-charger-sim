from __future__ import annotations
import time
from typing import Callable, Optional

class MeterSession:
    """Tracks energy consumption for a single charging session."""
    
    def __init__(self, start_wh: int, log: Callable[[str, str], None]):
        self.meter_start_wh: int = start_wh
        self.meter_wh: int = start_wh
        self.last_meter_read_wh: int = start_wh
        self.last_meter_read_ts: float = time.time()
        self.measured_power_w: int = 0
        self._log = log
    
    def update(self, reading_wh: int) -> int:
        """Update meter state with new reading. Returns the reported Wh value.
        
        Same logic as original _update_meter_state():
        - Calculates delta and elapsed time
        - Handles negative delta (meter value decreased) by keeping last valid value
        - Calculates measured_power_w from delta and elapsed time
        """
        now = time.time()
        reported_wh = reading_wh
        
        elapsed = now - self.last_meter_read_ts
        delta_wh = reading_wh - self.last_meter_read_wh
        
        if delta_wh < 0:
            self._log("WARN", f"Sayac degeri azaldi: onceki={self.last_meter_read_wh} Wh, yeni={reading_wh} Wh; son gecerli deger korunuyor")
            delta_wh = 0
            reported_wh = self.last_meter_read_wh
        
        if elapsed > 0:
            self.measured_power_w = int(round((delta_wh * 3600) / elapsed))
        
        self.meter_wh = reported_wh
        self.last_meter_read_wh = reported_wh
        self.last_meter_read_ts = now
        return reported_wh
    
    @property
    def energy_kwh(self) -> float:
        """Session energy consumption in kWh."""
        return max(0, self.meter_wh - self.meter_start_wh) / 1000.0
