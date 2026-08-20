import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from ocpp_charger.config import ChargePointConfig
    from ocpp_charger.logger import Logger
    from ocpp_charger.session import MeterSession
    from ocpp_charger.protocol.client import OcppClient
    from ocpp_charger.protocol.messages import Action, SERVER_ACTION_RESPONSES, ChargePointStatus
    from ocpp_charger.hardware.display import IDisplayDriver, TOUCH_COMP_USER_ID_TAG
    from ocpp_charger.hardware.meter import IMeterReader
    from ocpp_charger.hardware.cp_signal import ICpSignalReader
except ImportError:
    from .config import ChargePointConfig
    from .logger import Logger
    from .session import MeterSession
    from .protocol.client import OcppClient
    from .protocol.messages import Action, SERVER_ACTION_RESPONSES, ChargePointStatus
    from .hardware.display import IDisplayDriver, TOUCH_COMP_USER_ID_TAG
    from .hardware.meter import IMeterReader
    from .hardware.cp_signal import ICpSignalReader

class ChargePoint:
    """OCPP 1.6J Charge Point — state machine with all runtime state as instance attributes."""
    
    def __init__(
        self,
        config: ChargePointConfig,
        client: OcppClient,
        display: IDisplayDriver,
        meter: IMeterReader,
        cp_reader: Optional[ICpSignalReader],
        logger: Logger,
    ):
        self.config = config
        self.client = client
        self.display = display
        self.meter = meter
        self.cp_reader = cp_reader
        self.log = logger
        
        # Transaction state
        self.transaction_id: Optional[int] = None
        self.session: Optional[MeterSession] = None
        
        # Charging state
        self.charging_active: bool = False
        self.charge_start_time: Optional[float] = None
        self.transaction_end_time: Optional[float] = None
        
        # Connection state  
        self.current_status: str = "NOT CONNECTED"
        
        # CP ADC state
        self.cp_status_source_active: bool = False
        self.last_cp_status: Optional[str] = None
        self.last_cp_gap_log_ts: float = 0.0
        self.cp_last_csms_state: Optional[str] = None

    @staticmethod
    def iso_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    async def boot_notification(self):
        return await self.client.call(Action.BOOT_NOTIFICATION, {
            "chargePointVendor":       self.config.vendor,
            "chargePointModel":        self.config.model,
            "chargePointSerialNumber": self.config.serial,
            "firmwareVersion":         self.config.firmware,
        })

    async def heartbeat(self):
        await self.client.send(Action.HEARTBEAT, {})

    async def status_notification(self, connector_id: int, status: str, error_code: str = "NoError"):
        await self.client.send(Action.STATUS_NOTIFICATION, {
            "connectorId": connector_id,
            "status":      status,
            "errorCode":   error_code,
            "timestamp":   self.iso_now(),
        })
        if not self.cp_status_source_active:
            self.display.set_status(status.upper())
            self.current_status = status.upper()

    async def authorize(self, id_tag: Optional[str] = None):
        if id_tag is None:
            id_tag = self.config.user_id_tag
        await self.client.send(Action.AUTHORIZE, {"idTag": id_tag})

    async def _read_meter_async(self) -> Optional[int]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.meter.read_wh)

    async def _read_actual_meter(self, context: str) -> Optional[int]:
        reading_wh = await self._read_meter_async()
        if reading_wh is None:
            self.log("ERR", f"{context}: sayac okunamadi")
            return None

        if self.session is None:
            return reading_wh
            
        reported_wh = self.session.update(reading_wh)
        if reported_wh != reading_wh:
            self.log("INFO", f"{context}: sayac={reading_wh} Wh, gonderilecek={reported_wh} Wh, kullanim={self.session.energy_kwh:.3f} kWh")
        else:
            self.log("INFO", f"{context}: sayac={reading_wh} Wh, kullanim={self.session.energy_kwh:.3f} kWh")
        return reported_wh

    def _update_display_status(self):
        if self.charge_start_time is not None and self.session is not None:
            if self.charging_active:
                elapsed = time.time() - self.charge_start_time
            else:
                if self.transaction_end_time:
                    elapsed = self.transaction_end_time - self.charge_start_time
                else:
                    elapsed = 0
            
            energy_kwh = self.session.energy_kwh
            cost = energy_kwh * self.config.cost_per_kwh
            power_kw = self.session.measured_power_w / 1000.0 if self.charging_active else 0.0
            
            self.display.update_charging_info(energy_kwh, power_kw, elapsed, cost)
        else:
            self.display.update_charging_info(0.0, 0.0, -1, 0.0)

    async def start_transaction(self, id_tag: Optional[str] = None):
        if id_tag is None:
            id_tag = self.config.user_id_tag
            
        start_wh = await self._read_meter_async()
        if start_wh is None:
            self.log("ERR", "StartTransaction iptal: sayac okunamadi, simule enerji gonderilmeyecek")
            if not self.cp_status_source_active:
                self.display.set_status("AVAILABLE")
                self.current_status = "AVAILABLE"
            self._update_display_status()
            return
            
        self.session = MeterSession(start_wh, self.log)
        self.log("INFO", f"StartTransaction: sayac baslangic={start_wh} Wh")
        self.charge_start_time = time.time()
        self.transaction_end_time = None
        self.charging_active = True
        
        if not self.cp_status_source_active:
            self.display.set_status("CHARGING")
            self.current_status = "CHARGING"
        self._update_display_status()
        
        try:
            response = await self.client.call(Action.START_TRANSACTION, {
                "connectorId": 1,
                "idTag":       id_tag,
                "meterStart":  self.session.meter_wh,
                "timestamp":   self.iso_now(),
            })
            if "transactionId" in response:
                self.transaction_id = response["transactionId"]
                self.log("INFO", f"Transaction ID={self.transaction_id}")
        except Exception as e:
            self.log("ERR", f"StartTransaction call failed: {e}")

    async def stop_transaction(self):
        if self.transaction_id is None:
            self.log("WARN", "Aktif işlem yok!")
            return

        stop_wh = await self._read_actual_meter("StopTransaction")
        if stop_wh is None:
            if self.session and self.session.meter_wh > 0:
                stop_wh = self.session.meter_wh
                self.log("WARN", f"StopTransaction: son basarili sayac degeri kullaniliyor: {stop_wh} Wh")
            else:
                self.log("ERR", "StopTransaction iptal: sayac okunamadi ve onceki deger yok")
                return

        self.charging_active = False
        self.transaction_end_time = time.time()
        if not self.cp_status_source_active:
            self.display.set_status("AVAILABLE")
            self.current_status = "AVAILABLE"

        await self.client.send(Action.STOP_TRANSACTION, {
            "transactionId": self.transaction_id,
            "idTag":         self.config.user_id_tag,
            "meterStop":     stop_wh,
            "timestamp":     self.iso_now(),
            "reason":        "Local",
        })
        self._update_display_status()
        self.log("INFO", f"Sarj durduruldu - Gonderilen Meter: {stop_wh} Wh")
        self.transaction_id = None

    async def meter_values(self):
        if not self.charging_active or not self.charge_start_time:
            self.log("WARN", "Şarj aktif değil — MeterValues gönderilmedi")
            return

        reading_wh = await self._read_actual_meter("MeterValues")
        if reading_wh is None or self.session is None:
            self.log("ERR", "MeterValues gonderilmedi: sayac okunamadi")
            return

        payload = {
            "connectorId": 1,
            "meterValue": [{
                "timestamp": self.iso_now(),
                "sampledValue": [
                    {"value": str(reading_wh),       "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                    {"value": str(self.config.default_voltage),  "measurand": "Voltage",        "unit": "V"},
                    {"value": str(self.config.default_current),  "measurand": "Current.Import", "unit": "A"},
                    {"value": str(self.session.measured_power_w), "measurand": "Power.Active.Import", "unit": "W"}
                ]
            }]
        }
        if self.transaction_id:
            payload["transactionId"] = self.transaction_id

        await self.client.send(Action.METER_VALUES, payload)
        self._update_display_status()
        self.log("INFO", f"MeterValues Gonderildi: {reading_wh} Wh")

    def init_cp_status_source(self) -> bool:
        if not self.config.cp_adc_enabled:
            self.log("INFO", "CP ADC disabled; Nextion status uses simulator events")
            return False

        if self.cp_reader is None:
            self.log("WARN", "CP ADC unavailable; Nextion status uses simulator events")
            return False

        try:
            reading = self.cp_reader.read()
        except Exception as e:
            self.cp_reader = None
            self.cp_status_source_active = False
            self.log("WARN", f"CP ADC unavailable: {e}; Nextion status uses simulator events")
            return False

        self.cp_status_source_active = True
        self.log("INFO", f"CP ADC ready: adc={reading.adc_voltage:.2f}V cp={reading.cp_voltage:.2f}V status={reading.status or 'hold'}")

        if reading.status:
            self.display.set_status(reading.status)
            self.current_status = reading.status.upper()

        return True

    async def _handle_cp_state_transition(self, old_state: Optional[str], new_state: str):
        self.log("INFO", f"CP State Machine: {old_state or 'None'} → {new_state}")

        if new_state == "Available":
            if self.charging_active and self.transaction_id is not None:
                self.log("INFO", "CP → Available: Aktif şarj durduruluyor (StopTransaction)")
                await self.stop_transaction()
            await self.status_notification(1, "Available")

        elif new_state == "Connected":
            await self.status_notification(1, "Preparing")
            await self.authorize()

        elif new_state == "Charging":
            if not self.charging_active:
                self.log("INFO", "CP → Charging: Şarj başlatılıyor (StartTransaction)")
                await self.status_notification(1, "Charging")
                await self.start_transaction()
            else:
                await self.status_notification(1, "Charging")

        elif new_state == "Ventilation":
            await self.status_notification(1, "SuspendedEV")

        elif new_state == "Fault":
            if self.charging_active and self.transaction_id is not None:
                self.log("INFO", "CP → Fault: Aktif şarj durduruluyor (StopTransaction)")
                await self.stop_transaction()
            await self.status_notification(1, "Faulted", error_code="GroundFailure")

    async def cp_state_machine_loop(self):
        if not self.cp_status_source_active or self.cp_reader is None:
            return

        loop = asyncio.get_running_loop()
        while True:
            try:
                reading = await loop.run_in_executor(None, self.cp_reader.read)
                if reading.status:
                    self.display.set_status(reading.status)
                    self.current_status = reading.status.upper()

                    if reading.status != self.last_cp_status:
                        self.log("INFO", f"CP status -> {reading.status} ({reading.cp_voltage:.2f} V)")
                        old_state = self.cp_last_csms_state
                        new_state = reading.status
                        self.last_cp_status = reading.status

                        await self._handle_cp_state_transition(old_state, new_state)
                        self.cp_last_csms_state = new_state
                else:
                    now = time.time()
                    if now - self.last_cp_gap_log_ts >= 5:
                        self.log("WARN", f"CP voltage outside status bands: {reading.cp_voltage:.2f} V; keeping {self.current_status}")
                        self.last_cp_gap_log_ts = now
            except Exception as e:
                self.log("WARN", f"CP state machine error: {e}")

            await asyncio.sleep(self.config.cp_status_interval)

    async def handle_server_request(self, action: str, payload: dict) -> dict:
        if action == Action.REMOTE_START_TRANSACTION:
            id_tag = payload.get("idTag", self.config.user_id_tag)
            asyncio.create_task(self.start_transaction(id_tag))
        elif action == Action.REMOTE_STOP_TRANSACTION:
            asyncio.create_task(self.stop_transaction())
            
        return SERVER_ACTION_RESPONSES.get(action, {})

    async def clock_loop(self):
        while True:
            self.display.set_time()
            self.display.set_status(self.current_status)
            self.display.set_user_id(self.config.user_id_tag)
            await asyncio.sleep(1)

    async def status_update_loop(self):
        while True:
            self._update_display_status()
            await asyncio.sleep(1)

    async def auto_meter_values_loop(self, interval: int = 10):
        while True:
            await asyncio.sleep(interval)
            if self.charging_active and self.charge_start_time:
                try:
                    await self.meter_values()
                except Exception as e:
                    self.log("ERR", f"MeterValues otomatik gönderme hatası: {e}")

    async def nextion_touch_loop(self):
        async for event in self.display.read_touch_events():
            if event.event_type == 0x01:
                self.log("INFO", f"Nextion touch → page={event.page_id} comp={event.component_id}")
                if event.component_id == TOUCH_COMP_USER_ID_TAG:
                    self.log("INFO", f"useridtag butonu → id yazılıyor: {self.config.user_id_tag}")
                    self.display.set_user_id(self.config.user_id_tag)

    def print_info_banner(self):
        print(f"""
\033[1m\033[36m╔═══════════════════════════════════════════════════╗\033[0m
\033[1m\033[36m║  OCPP 1.6J · CP Voltaj Tabanlı State Machine      ║\033[0m
\033[1m\033[36m╠═══════════════════════════════════════════════════╣\033[0m
\033[1m\033[36m║\033[0m  Mod  : ADS1115 CP Voltaj Okuma (Otomatik)        \033[1m\033[36m║\033[0m
\033[1m\033[36m║\033[0m  CSMS : {self.config.csms_url:<42s}\033[1m\033[36m║\033[0m
\033[1m\033[36m╠═══════════════════════════════════════════════════╣\033[0m
\033[1m\033[36m║\033[0m  Voltaj → Durum eşleştirmesi:                     \033[1m\033[36m║\033[0m
\033[1m\033[36m║\033[0m    \033[32m≥11.1V → Available\033[0m     \033[36m7-11V → Connected\033[0m        \033[1m\033[36m║\033[0m
\033[1m\033[36m║\033[0m    \033[32m5-7V   → Charging\033[0m      \033[33m2-4V  → Ventilation\033[0m     \033[1m\033[36m║\033[0m
\033[1m\033[36m║\033[0m    \033[31m<1V    → Fault\033[0m                                 \033[1m\033[36m║\033[0m
\033[1m\033[36m╠═══════════════════════════════════════════════════╣\033[0m
\033[1m\033[36m║\033[0m  \033[1mq\033[0m / quit → Çıkış                                 \033[1m\033[36m║\033[0m
\033[1m\033[36m╚═══════════════════════════════════════════════════╝\033[0m
""")

    async def console_input(self):
        loop = asyncio.get_running_loop()
        self.print_info_banner()
        while True:
            try:
                choice = await loop.run_in_executor(None, input, "\n\033[1m>\033[0m ")
                choice = choice.strip().lower()

                if choice in ("q", "quit", "exit"):
                    self.log("INFO", "Çıkılıyor...")
                    if self.charging_active and self.transaction_id is not None:
                        await self.stop_transaction()
                    await self.status_notification(1, "Unavailable")
                    self.display.set_status("NOT CONNECTED")
                    self.current_status = "NOT CONNECTED"
                    self.display.show_page("rfid_scan")
                    await asyncio.sleep(0.5)
                    import sys
                    sys.exit(0)
                else:
                    self.log("INFO", "Voltaj tabanlı otomatik mod aktif. Çıkmak için 'q' yazın.")

            except (EOFError, KeyboardInterrupt):
                break

    async def run(self):
        self.display.set_status("NOT CONNECTED")
        self.current_status = "NOT CONNECTED"
        self.display.set_time()
        self.display.set_user_id(self.config.user_id_tag)
        
        self.init_cp_status_source()

        self.client.set_server_request_handler(self.handle_server_request)

        try:
            await self.client.connect_with_retry()
            
            if not self.cp_status_source_active:
                self.display.set_status("CONNECTED")
                self.current_status = "CONNECTED"
            self.display.set_user_id(self.config.user_id_tag)

            response = await self.boot_notification()
            
            interval = response.get("interval", self.config.heartbeat_interval)
            status = response.get("status")
            self.log("INFO", f"BootNotification: status={status}, interval={interval}s")
            
            if status == "Accepted":
                await self.client.start_heartbeat(interval)

            await self.status_notification(1, "Available")

            tasks = [
                asyncio.create_task(self.client.recv_loop()),
                asyncio.create_task(self.console_input()),
                asyncio.create_task(self.clock_loop()),
                asyncio.create_task(self.status_update_loop()),
                asyncio.create_task(self.nextion_touch_loop()),
                asyncio.create_task(self.auto_meter_values_loop(interval=10))
            ]

            if self.cp_status_source_active:
                tasks.append(asyncio.create_task(self.cp_state_machine_loop()))

            await asyncio.gather(*tasks)

        except OSError as e:
            self.log("ERR", f"Bağlantı başarısız: {e}")
            self.log("INFO", "CSMS çalışıyor mu? URL doğru mu?")
            if not self.cp_status_source_active:
                self.display.set_status("NOT CONNECTED")
                self.current_status = "NOT CONNECTED"
        except Exception as e:
            self.log("ERR", f"Beklenmeyen hata: {e}")
            if not self.cp_status_source_active:
                self.display.set_status("NOT CONNECTED")
                self.current_status = "NOT CONNECTED"
        finally:
            await self.client.disconnect()
