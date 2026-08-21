#!/usr/bin/env python3
"""OCPP 1.6J Charge Point Simulator — Entry Point.

This module wires together all components via dependency injection
and runs the simulator.
"""

import asyncio
import signal
import sys
import time
from pathlib import Path

# Ensure src/ is in sys.path when executed directly
SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ocpp_charger.config import ChargePointConfig
from ocpp_charger.logger import Logger
from ocpp_charger.charge_point import ChargePoint
from ocpp_charger.protocol.client import OcppClient
from ocpp_charger.hardware.display import NextionDisplay, NullDisplay, TOUCH_COMP_USER_ID_TAG, IDisplayDriver
from ocpp_charger.hardware.meter import ModbusMeter, IMeterReader
from ocpp_charger.hardware.cp_signal import Ads1115CpReader, ICpSignalReader
from ocpp_charger.hardware.nfc import Pn532Reader, INfcReader



def wait_for_nfc_auth(config: ChargePointConfig, display: IDisplayDriver, logger: Logger) -> None:
    """Block until valid NFC card is presented."""
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    RESET = "\033[0m"
    DIM = "\033[2m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    RED = "\033[31m"

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║     NFC Kimlik Doğrulama             ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════╝{RESET}")
    print(f"{DIM}İzin verilen kart UID : {config.nfc_allowed_id}{RESET}")
    print(f"{YELLOW}Kartı okuyucuya yaklaştırın...{RESET}\n")

    try:
        nfc_reader = Pn532Reader()
        nfc_reader.init()
    except Exception as e:
        logger("WARN", f"NFC reader not available: {e}. Bypassing NFC auth.")
        return

    display.show_page("rfid_scan")

    while True:
        try:
            uid = nfc_reader.read_uid()
            if uid:
                uid_str = uid.hex().upper()
                if uid_str == config.nfc_allowed_id:
                    print(f"{GREEN}{BOLD}✓ Kart onaylandı  : {uid_str}{RESET}")
                    print(f"{GREEN}  Simülasyon başlıyor...{RESET}\n")
                    display.show_page("home")
                    return
                else:
                    print(f"{RED}✗ Geçersiz kart   : {uid_str}  —  Tekrar deneyin{RESET}")
            time.sleep(0.1)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Çıkış yapılıyor...{RESET}")
            display.show_page("rfid_scan")
            time.sleep(0.5)
            sys.exit(0)
        except Exception as e:
            logger("ERR", f"NFC read error: {e}")
            time.sleep(1)


def main() -> None:
    # 1. Load config
    config = ChargePointConfig.from_env()
    
    # 2. Create logger
    logger = Logger()
    
    # 3. Create display
    display: IDisplayDriver
    try:
        import serial
        display = NextionDisplay(
            port=config.nextion_port,
            baudrate=config.nextion_baudrate,
            logger=logger,
            pic_car_connected=config.pic_car_connected,
            pic_car_disconnected=config.pic_car_disconnected
        )
    except ImportError:
        logger("WARN", "pyserial not found, using NullDisplay")
        display = NullDisplay()
    
    # 4. Open display
    display.open()
    
    # 5. Run NFC auth flow
    wait_for_nfc_auth(config, display, logger)
    
    # 6. Create ModbusMeter
    meter: IMeterReader
    try:
        from ocpp_charger.hardware.meter import ModbusMeter
        meter = ModbusMeter()
    except Exception as e:
        logger("WARN", f"Could not init ModbusMeter: {e}. Using MockMeter.")
        class MockMeter(IMeterReader):
            def read_wh(self): return 1000
            def close(self): pass
            def configure_logger(self, log_fn): pass
        meter = MockMeter()
        
    meter.configure_logger(logger)
    
    # 7. Create CpSignalReader
    cp_reader: ICpSignalReader = None
    if config.cp_adc_enabled:
        try:
            cp_reader = Ads1115CpReader(
                channel=config.cp_adc_channel,
                gain=config.cp_adc_gain,
                voltage_multiplier=config.cp_voltage_divider
            )
        except Exception as e:
            logger("WARN", f"Could not init Ads1115CpReader: {e}.")
            
    # 8. Create OcppClient
    client = OcppClient(
        csms_url=config.csms_url,
        subprotocol=config.subprotocol,
        auth_user=config.basic_auth_user,
        auth_password=config.basic_auth_password,
        ping_interval=config.ping_interval,
        logger=logger
    )
    
    # 9. Create ChargePoint
    charge_point = ChargePoint(
        config=config,
        client=client,
        display=display,
        meter=meter,
        cp_reader=cp_reader,
        logger=logger
    )
    
    # 10. Setup SIGINT handler (already handled implicitly by asyncio in most cases, 
    # but we can set a flag if needed. For now, ChargePoint handles KeyboardInterrupt in its run loop).

    try:
        # 11. Run
        asyncio.run(charge_point.run())
    except KeyboardInterrupt:
        logger("INFO", "Exiting due to KeyboardInterrupt")
    finally:
        display.close()
        meter.close()


if __name__ == "__main__":
    main()
