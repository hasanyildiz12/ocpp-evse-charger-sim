#!/usr/bin/env python3
"""OCPP 1.6J charge point — entry point.

    python main.py               # run the charge point
    python main.py pilot-test    # bench-test the pilot board (no CSMS needed)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ocpp_charger.config import ChargePointConfig
from ocpp_charger.logger import Logger
from ocpp_charger.charge_point import ChargePoint
from ocpp_charger.protocol.client import OcppClient
from ocpp_charger.hardware.display import IDisplayDriver, NextionDisplay, NullDisplay
from ocpp_charger.hardware.meter import IMeterReader, ModbusMeter, SimulatedMeter
from ocpp_charger.hardware.cp_signal import Ads1115CpReader, ICpSignalReader, MockCpReader
from ocpp_charger.hardware.pilot import IPilotPwm, MockPilotPwm, RpiHardwarePwm
from ocpp_charger.hardware.nfc import INfcReader, MockNfcReader, Pn532Reader


@dataclass
class Hardware:
    display: IDisplayDriver
    meter: IMeterReader
    cp_reader: Optional[ICpSignalReader]
    pwm: IPilotPwm
    nfc: Optional[INfcReader]
    simulated: list


def _looks_like_pi() -> bool:
    return sys.platform.startswith("linux") and os.path.exists("/dev/i2c-1")


def build_hardware(config: ChargePointConfig, logger: Logger) -> Hardware:
    mode = config.hardware_mode
    if mode == "auto" and not _looks_like_pi():
        logger("INFO", "Raspberry Pi algılanmadı → simülasyon modu")
        mode = "sim"
    strict = mode == "pi"
    simulated = []

    def fallback(name: str, err: Exception):
        if strict:
            raise RuntimeError(f"{name} başlatılamadı (HARDWARE_MODE=pi): {err}") from err
        logger("WARN", f"{name} kullanılamıyor ({err}) → SİMÜLE ediliyor")
        simulated.append(name)

    # PWM
    pwm: IPilotPwm = MockPilotPwm()
    if mode != "sim":
        try:
            real = RpiHardwarePwm(config.pwm_chip, config.pwm_channel, config.pwm_frequency_hz)
            real.open()
            pwm = real
            logger("INFO", f"Pilot PWM hazır (chip {config.pwm_chip} ch {config.pwm_channel}, {config.pwm_frequency_hz} Hz)")
        except Exception as e:
            fallback("Pilot PWM (GPIO12)", e)
    if isinstance(pwm, MockPilotPwm):
        pwm.open()

    # CP / PP reader
    cp_reader: Optional[ICpSignalReader] = None
    if config.cp_adc_enabled:
        if mode == "sim":
            cp_reader = _mock_cp(config, pwm)
        else:
            try:
                reader = Ads1115CpReader(config.cp_adc_channel,
                                         config.pp_adc_channel if config.pp_enabled else None,
                                         config.cp_adc_gain)
                reader.read_cp_adc()
                cp_reader = reader
                logger("INFO", "ADS1115 hazır (A0=CP, A1=PP)")
            except Exception as e:
                fallback("ADS1115", e)
                cp_reader = _mock_cp(config, pwm)

    # NFC
    nfc: Optional[INfcReader] = None
    if config.nfc_enabled:
        if mode == "sim":
            nfc = MockNfcReader()
        else:
            try:
                reader = Pn532Reader()
                reader.init()
                nfc = reader
                logger("INFO", "PN532 NFC hazır")
            except Exception as e:
                fallback("PN532 NFC", e)
                nfc = MockNfcReader()

    # Display
    display: IDisplayDriver
    if mode == "sim":
        display = NullDisplay()
    else:
        display = NextionDisplay(config.nextion_port, config.nextion_baudrate, logger,
                                 config.pic_car_connected, config.pic_car_disconnected)
    display.open()

    # Meter
    meter: IMeterReader
    if mode == "sim":
        meter = SimulatedMeter()
    else:
        modbus = ModbusMeter(log=logger)
        if modbus.read_wh() is not None:
            meter = modbus
        else:
            modbus.close()
            fallback("Modbus sayaç", RuntimeError("okuma yapılamadı"))
            meter = SimulatedMeter()
    meter.configure_logger(logger)

    if mode == "sim":
        simulated = ["tümü"]
    return Hardware(display, meter, cp_reader, pwm, nfc, simulated)


def _mock_cp(config: ChargePointConfig, pwm: IPilotPwm) -> MockCpReader:
    return MockCpReader(pwm=pwm, v_low=config.cp_low_voltage, r_top=config.cp_r_top,
                        r_bottom=config.cp_r_bottom, r_pullup=config.cp_r_pullup,
                        vref=config.adc_vref, pp_pullup=config.pp_pullup_ohm)


def run_charge_point(config: ChargePointConfig) -> None:
    logger = Logger()
    hw = build_hardware(config, logger)
    if hw.simulated:
        logger("WARN", f"Simüle edilen birimler: {', '.join(hw.simulated)}")

    client = OcppClient(
        csms_url=config.csms_url,
        subprotocol=config.subprotocol,
        auth_user=config.basic_auth_user,
        auth_password=config.basic_auth_password,
        ping_interval=config.ping_interval,
        logger=logger,
        call_timeout=config.call_timeout,
    )
    cp = ChargePoint(config=config, client=client, display=hw.display, meter=hw.meter,
                     cp_reader=hw.cp_reader, logger=logger, pwm=hw.pwm, nfc=hw.nfc)

    if isinstance(hw.meter, SimulatedMeter):
        hw.meter.is_drawing = lambda: cp.transaction_active and cp.pilot_state == "C"

    try:
        asyncio.run(cp.run())
    except KeyboardInterrupt:
        logger("INFO", "Ctrl+C — çıkılıyor")
    finally:
        hw.pwm.close()
        hw.display.close()
        hw.meter.close()
        if hw.nfc is not None:
            hw.nfc.close()


def _safe_stdout() -> None:
    """Never crash on characters the console can't encode (Windows cp125x)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv: Optional[list] = None) -> None:
    _safe_stdout()
    parser = argparse.ArgumentParser(description="OCPP 1.6J EVSE controller")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "pilot-test"],
                        help="run: şarj ünitesi (varsayılan) · pilot-test: kart testi")
    args = parser.parse_args(argv)

    if args.command == "pilot-test":
        from ocpp_charger.tools.pilot_test import main as pilot_main
        pilot_main()
        return

    try:
        config = ChargePointConfig.from_env()
    except ValueError as e:
        print(f"Yapılandırma hatası: {e}")
        sys.exit(2)
    run_charge_point(config)


if __name__ == "__main__":
    main()
