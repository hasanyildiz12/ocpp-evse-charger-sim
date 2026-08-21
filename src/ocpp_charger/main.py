#!/usr/bin/env python3
"""OCPP 1.6J Charge Point Simulator — Entry Point.

This module wires together all components via dependency injection
and runs the simulator.
"""

import asyncio
import sys
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
