"""Control Pilot (CP) and Proximity Pilot (PP) sensing via ADS1115.

Schematic (ocpp-evse-hasan.kicad_sch):

  PILOT ──R7 200k──┬── PILOTREAD → ADS1115 A0
                   ├── R6 56k ── GND
                   └── R5 47k ── +3.3 V

  This is an *offset* divider, so the pilot's ±12 V swing maps into 0..3.3 V:
      V_adc = (V_cp/R7 + Vref/R5) / (1/R7 + 1/R6 + 1/R5)
  i.e. +12 V → 2.95 V, +9 V → 2.61 V, +6 V → 2.27 V, +3 V → 1.93 V,
        0 V → 1.59 V, -12 V → 0.23 V.  (A plain ×4.03 multiplier is wrong.)

  PP ── R2 330R ── +3.3 V, C1 to GND, → ADS1115 A1.
  The Type 2 cable's Rc (PP→PE) forms a divider with R2.

While the pilot is a 1 kHz PWM, the ADS1115 (delta-sigma, ≤860 SPS) returns
the *average* over the conversion, not the high level. ``pilot_high_from_average``
recovers the high level from the known duty and the measured negative level.
"""

from __future__ import annotations

import site as _site
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

try:
    user_site = _site.getusersitepackages()
    if user_site and user_site not in sys.path:
        _site.addsitedir(user_site)
except Exception:
    pass


# ─── Pure conversion helpers ─────────────────────────────────────────────────

def cp_voltage_from_adc(v_adc: float, r_top: float, r_bottom: float, r_pullup: float, vref: float) -> float:
    """Invert the offset divider: ADC voltage at PILOTREAD → pilot voltage."""
    return r_top * (v_adc / r_bottom + (v_adc - vref) / r_pullup + v_adc / r_top)


def adc_from_cp_voltage(v_cp: float, r_top: float, r_bottom: float, r_pullup: float, vref: float) -> float:
    g = 1.0 / r_top + 1.0 / r_bottom + 1.0 / r_pullup
    return (v_cp / r_top + vref / r_pullup) / g


def pilot_high_from_average(v_avg: float, duty: float, v_low: float) -> float:
    """Recover the positive pilot level from a duty-weighted average.

    avg = D·V_high + (1-D)·V_low  →  V_high = (avg - (1-D)·V_low) / D
    """
    if duty >= 1.0:
        return v_avg
    if duty <= 0.0:
        return v_avg  # pilot held low; nothing to recover
    return (v_avg - (1.0 - duty) * v_low) / duty


@dataclass(frozen=True)
class CpThresholds:
    a: float = 10.5
    b: float = 7.5
    c: float = 4.5
    d: float = 1.5


def pilot_state_from_voltage(v_high: float, th: CpThresholds = CpThresholds()) -> str:
    """IEC 61851-1 state letter from the positive pilot level.

    A 12 V no EV · B 9 V connected · C 6 V charging · D 3 V ventilation · E ≤0 V fault
    """
    if v_high >= th.a:
        return "A"
    if v_high >= th.b:
        return "B"
    if v_high >= th.c:
        return "C"
    if v_high >= th.d:
        return "D"
    return "E"


# Legacy helper kept for callers that want a human label.
STATE_LABELS = {"A": "Available", "B": "Connected", "C": "Charging", "D": "Ventilation", "E": "Fault"}


def status_from_cp_voltage(cp_voltage: float, th: CpThresholds = CpThresholds()) -> str:
    return STATE_LABELS[pilot_state_from_voltage(cp_voltage, th)]


# IEC 61851-1 Table B.2: cable Rc (PP–PE) ranges → cable current rating
PP_RANGES = (
    (1100.0, 2460.0, 13.0),   # nominal 1.5 kΩ
    (400.0, 936.0, 20.0),     # nominal 680 Ω
    (164.0, 308.0, 32.0),     # nominal 220 Ω
    (75.0, 140.0, 63.0),      # nominal 100 Ω
)


def pp_resistance_from_adc(v_adc: float, pullup_ohm: float, vref: float) -> Optional[float]:
    """Rc from the R2 divider. None = open (no cable)."""
    if v_adc >= vref * 0.97:
        return None
    if v_adc <= 0:
        return 0.0
    return pullup_ohm * v_adc / (vref - v_adc)


def cable_current_from_pp(rc: Optional[float]) -> Optional[float]:
    """Cable rating in A, or None if no cable / out-of-spec value."""
    if rc is None:
        return None
    for lo, hi, amps in PP_RANGES:
        if lo <= rc <= hi:
            return amps
    return None


# ─── Readers ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CpReading:
    adc_voltage: float          # raw ADC volts at PILOTREAD
    cp_voltage: float           # pilot voltage (average if PWM is active)


class ICpSignalReader(ABC):
    @abstractmethod
    def read_cp_adc(self) -> float:
        """Raw ADC volts at PILOTREAD (A0)."""

    def read_pp_adc(self) -> Optional[float]:
        """Raw ADC volts at PP (A1); None if not wired."""
        return None


class Ads1115CpReader(ICpSignalReader):
    def __init__(self, cp_channel: int = 0, pp_channel: Optional[int] = 1, gain: int = 1, address: int = 0x48):
        self.cp_channel = cp_channel
        self.pp_channel = pp_channel
        self.gain = gain
        self.address = address
        self._ads = None
        self._chans = {}
        self._lock = threading.Lock()

    def _pin(self, ADS, channel: int):
        pin = getattr(ADS, f"P{channel}", None)
        if pin is not None:
            return pin
        try:  # adafruit-circuitpython-ads1x15 >= 3.0
            from adafruit_ads1x15.ads1x15 import Pin  # type: ignore
            return getattr(Pin, f"A{channel}")
        except Exception:
            return channel

    def connect(self) -> None:
        if self._ads is not None:
            return
        import board  # type: ignore
        import busio  # type: ignore
        import adafruit_ads1x15.ads1115 as ADS  # type: ignore
        from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore

        i2c = busio.I2C(board.SCL, board.SDA)
        ads = ADS.ADS1115(i2c, address=self.address)
        ads.gain = self.gain
        ads.data_rate = 128  # ~7.8 ms conversion ≈ 8 PWM periods → stable average
        self._chans[self.cp_channel] = AnalogIn(ads, self._pin(ADS, self.cp_channel))
        if self.pp_channel is not None:
            self._chans[self.pp_channel] = AnalogIn(ads, self._pin(ADS, self.pp_channel))
        self._ads = ads

    def _read(self, channel: int) -> float:
        with self._lock:
            self.connect()
            return float(self._chans[channel].voltage)

    def read_cp_adc(self) -> float:
        return self._read(self.cp_channel)

    def read_pp_adc(self) -> Optional[float]:
        if self.pp_channel is None:
            return None
        return self._read(self.pp_channel)


class MockCpReader(ICpSignalReader):
    """Simulated EV + cable for desktop runs and tests.

    ``state`` is the EV side (A–E). With ``auto_ev`` a plugged EV moves from
    B to C when it sees PWM, like a real car. PWM averaging is simulated so
    the same compensation path as on hardware is exercised.
    """

    LEVELS = {"A": 12.0, "B": 9.0, "C": 6.0, "D": 3.0, "E": 0.0}

    def __init__(self, state: str = "A", pwm=None, v_low: float = -12.0,
                 r_top: float = 200_000.0, r_bottom: float = 56_000.0, r_pullup: float = 47_000.0,
                 vref: float = 3.3, pp_ohm: Optional[float] = None, pp_pullup: float = 330.0,
                 auto_ev: bool = True):
        self.state = state
        self.pwm = pwm
        self.v_low = v_low
        self.r = (r_top, r_bottom, r_pullup, vref)
        self.pp_ohm = pp_ohm
        self.pp_pullup = pp_pullup
        self.auto_ev = auto_ev

    def _effective_state(self) -> str:
        offering = self.pwm is not None and 0.0 < self.pwm.duty < 1.0
        if self.auto_ev and self.state == "B" and offering:
            return "C"
        return self.state

    def read_cp_adc(self) -> float:
        high = self.LEVELS[self._effective_state()]
        duty = self.pwm.duty if self.pwm is not None else 1.0
        if duty <= 0.0:
            v = self.v_low
        elif duty >= 1.0 or high == 0.0:
            v = high
        else:
            v = duty * high + (1.0 - duty) * self.v_low
        return adc_from_cp_voltage(v, *self.r)

    def read_pp_adc(self) -> Optional[float]:
        vref = self.r[3]
        if self.pp_ohm is None:
            return vref
        return vref * self.pp_ohm / (self.pp_ohm + self.pp_pullup)
