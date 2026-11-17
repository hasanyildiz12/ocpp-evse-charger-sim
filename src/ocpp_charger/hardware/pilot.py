"""Control Pilot PWM output.

Schematic: GPIO12 (PWM0) → LM358 IC1A +IN, -IN held at 1.65 V by R8/R9.
The op-amp works as a comparator powered from +12 V / TC1044S (-12 V), so
GPIO high → pilot ≈ +12 V, GPIO low → pilot ≈ -12 V, through R1 (1 kΩ).

  duty = 1.0  → constant +12 V  (state A/B: "EVSE ready, no charging allowed")
  0 < duty < 1 → 1 kHz ±12 V PWM, duty encodes max current (IEC 61851-1 Annex A)
  duty = 0.0  → constant -12 V  (state F: EVSE not available)
"""

from __future__ import annotations

from abc import ABC, abstractmethod


def duty_from_current(amps: float) -> float:
    """IEC 61851-1 Annex A: 6–51 A → D% = A / 0.6 ; 51–80 A → D% = A / 2.5 + 64."""
    if amps < 6.0:
        raise ValueError("IEC 61851-1 minimum is 6 A")
    if amps <= 51.0:
        pct = amps / 0.6
    else:
        pct = min(amps, 80.0) / 2.5 + 64.0
    return round(pct / 100.0, 4)


def current_from_duty(duty: float) -> float:
    pct = duty * 100.0
    if 10.0 <= pct <= 85.0:
        return pct * 0.6
    if 85.0 < pct <= 96.0:
        return (pct - 64.0) * 2.5
    return 0.0


class IPilotPwm(ABC):
    """Drives the pilot comparator input."""

    def __init__(self) -> None:
        self.duty: float = 0.0

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def _apply(self, duty: float) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def set_duty(self, duty: float) -> None:
        duty = max(0.0, min(1.0, duty))
        if duty != self.duty:
            self._apply(duty)
            self.duty = duty

    # Convenience
    def standby(self) -> None:
        """+12 V DC: EV may connect, must not draw current."""
        self.set_duty(1.0)

    def offer_current(self, amps: float) -> None:
        self.set_duty(duty_from_current(amps))

    def disable(self) -> None:
        """-12 V DC (state F)."""
        self.set_duty(0.0)

    @property
    def offering(self) -> bool:
        return 0.0 < self.duty < 1.0


class RpiHardwarePwm(IPilotPwm):
    """Kernel hardware PWM via sysfs (package ``rpi-hardware-pwm``).

    /boot/firmware/config.txt needs: ``dtoverlay=pwm,pin=12,func=4``
    (Pi Zero 2 W / 3 / 4: chip 0, channel 0. Pi 5: chip 2.)
    """

    def __init__(self, chip: int = 0, channel: int = 0, frequency_hz: int = 1000):
        super().__init__()
        self.chip = chip
        self.channel = channel
        self.frequency_hz = frequency_hz
        self._pwm = None

    def open(self) -> None:
        from rpi_hardware_pwm import HardwarePWM  # type: ignore

        self._pwm = HardwarePWM(pwm_channel=self.channel, hz=self.frequency_hz, chip=self.chip)
        # Start at +12 V (standby) so a plugged EV sees state B, not F.
        self._pwm.start(100)
        self.duty = 1.0

    def _apply(self, duty: float) -> None:
        if self._pwm is None:
            raise RuntimeError("PWM not opened")
        self._pwm.change_duty_cycle(duty * 100.0)

    def close(self) -> None:
        if self._pwm is not None:
            try:
                self._pwm.change_duty_cycle(0)
                self._pwm.stop()
            except Exception:
                pass
            self._pwm = None
        self.duty = 0.0


class MockPilotPwm(IPilotPwm):
    def open(self) -> None:
        self.duty = 1.0

    def _apply(self, duty: float) -> None:
        pass

    def close(self) -> None:
        self.duty = 0.0
