"""Bench test for the pilot board (no CSMS, no OCPP).

    python main.py pilot-test

Drives GPIO12 and prints what the ADS1115 sees on A0 (PILOTREAD) and A1 (PP),
converted with the schematic's resistor values, so the perfboard can be
checked against the expected table in docs/donanim-test-plani.md.
"""

from __future__ import annotations

import time

from ocpp_charger.config import ChargePointConfig
from ocpp_charger.hardware.cp_signal import (
    Ads1115CpReader,
    CpThresholds,
    adc_from_cp_voltage,
    cable_current_from_pp,
    cp_voltage_from_adc,
    pilot_high_from_average,
    pilot_state_from_voltage,
    pp_resistance_from_adc,
)
from ocpp_charger.hardware.pilot import RpiHardwarePwm, duty_from_current

HELP = """Komutlar:
  high            GPIO12 sürekli HIGH → pilot +12 V (standby)
  low             GPIO12 sürekli LOW  → pilot -12 V
  pwm <A>         1 kHz PWM, akıma göre duty (ör. pwm 16 → %26.7)
  duty <yüzde>    1 kHz PWM, doğrudan duty (ör. duty 50)
  read            tek ölçüm
  watch           0.5 s'de bir ölçüm (Ctrl+C ile dur)
  table           beklenen ADC gerilimleri tablosu
  q               çık (pilot -12 V'a çekilir)"""


def _expected_table(c: ChargePointConfig) -> None:
    print(" Pilot V | ADC A0 (beklenen)")
    for v in (12, 9, 6, 3, 0, -12):
        a = adc_from_cp_voltage(v, c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup, c.adc_vref)
        print(f"  {v:+5.1f}  | {a:.3f} V")
    print(" PP Rc   | ADC A1 (beklenen)")
    for rc, amps in ((None, "kablo yok"), (1500, "13 A"), (680, "20 A"), (220, "32 A"), (100, "63 A")):
        a = c.adc_vref if rc is None else c.adc_vref * rc / (rc + c.pp_pullup_ohm)
        print(f"  {str(rc or '∞'):>5} Ω | {a:.3f} V  ({amps})")


def main() -> None:
    c = ChargePointConfig.from_env(require_csms=False)
    th = CpThresholds(c.cp_threshold_a, c.cp_threshold_b, c.cp_threshold_c, c.cp_threshold_d)
    reader = Ads1115CpReader(c.cp_adc_channel, c.pp_adc_channel, c.cp_adc_gain)
    pwm = None
    try:
        pwm = RpiHardwarePwm(c.pwm_chip, c.pwm_channel, c.pwm_frequency_hz)
        pwm.open()
        print("PWM açıldı → pilot +12 V (high)")
    except Exception as e:
        print(f"UYARI: PWM açılamadı ({e}). config.txt'de 'dtoverlay=pwm,pin=12,func=4' var mı? "
              "pip install rpi-hardware-pwm ? Yalnızca okuma yapılacak.")

    def measure() -> None:
        duty = pwm.duty if pwm else 1.0
        a0 = reader.read_cp_adc()
        v_cp = cp_voltage_from_adc(a0, c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup, c.adc_vref)
        v_high = pilot_high_from_average(v_cp, duty, c.cp_low_voltage)
        state = "?" if duty <= 0 else ("E" if v_high > 13.5 else pilot_state_from_voltage(v_high, th))
        line = f"duty={duty * 100:5.1f}%  A0={a0:.3f} V  CP(ort)={v_cp:+6.2f} V  CP(high)={v_high:+6.2f} V  durum={state}"
        pp = reader.read_pp_adc()
        if pp is not None:
            rc = pp_resistance_from_adc(pp, c.pp_pullup_ohm, c.adc_vref)
            line += f"  |  A1={pp:.3f} V  Rc={'∞' if rc is None else f'{rc:.0f} Ω'}  kablo={cable_current_from_pp(rc)} A"
        print(line)

    print(HELP)
    try:
        while True:
            parts = input("\npilot> ").strip().split()
            if not parts:
                continue
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd in ("q", "quit", "exit"):
                    break
                elif cmd == "table":
                    _expected_table(c)
                elif cmd == "read":
                    measure()
                elif cmd == "watch":
                    try:
                        while True:
                            measure()
                            time.sleep(0.5)
                    except KeyboardInterrupt:
                        print()
                elif pwm is None:
                    print("PWM yok — yalnızca read/watch/table kullanılabilir")
                elif cmd == "high":
                    pwm.standby()
                elif cmd == "low":
                    pwm.disable()
                elif cmd == "pwm" and args:
                    d = duty_from_current(float(args[0]))
                    pwm.set_duty(d)
                    print(f"{args[0]} A → duty %{d * 100:.1f}")
                elif cmd == "duty" and args:
                    pwm.set_duty(float(args[0]) / 100.0)
                else:
                    print(HELP)
            except Exception as e:
                print(f"Hata: {e}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        if pwm is not None:
            pwm.close()
        print("Pilot kapatıldı (-12 V).")


if __name__ == "__main__":
    main()
