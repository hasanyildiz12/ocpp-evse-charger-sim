"""OCPP 1.6J charge point — one connector, IEC 61851-1 pilot state machine.

Session flow (card per session):

    card tap ──Authorize──▶ Accepted ─┐
                                      ├─▶ StartTransaction ─▶ PWM on ─▶ EV C ─▶ Charging
    EV plugged (pilot B) ─────────────┘
    same card tap / RemoteStop / unplug / fault ─▶ PWM off ─▶ StopTransaction

Order of operations is chosen so energy is never offered without an open
transaction: StartTransaction is confirmed *before* PWM starts, and PWM is
withdrawn *before* StopTransaction is sent.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from .config import ChargePointConfig
from .hardware.cp_signal import (
    CpReading,
    CpThresholds,
    ICpSignalReader,
    MockCpReader,
    cable_current_from_pp,
    cp_voltage_from_adc,
    pilot_high_from_average,
    pilot_state_from_voltage,
    pp_resistance_from_adc,
)
from .hardware.display import TOUCH_COMP_USER_ID_TAG, IDisplayDriver
from .hardware.meter import IMeterReader
from .hardware.nfc import INfcReader
from .hardware.pilot import IPilotPwm, MockPilotPwm
from .protocol.client import NotConnectedError, OcppClient
from .protocol.messages import (
    Action,
    CallErrorCode,
    CallResult,
    ChargePointStatus,
    ErrorCode,
    OcppCallError,
    StopReason,
    id_tag_accepted,
    id_tag_status,
)
from .session import MeterSession

CONNECTOR_ID = 1
# Positive pilot level above this cannot be produced by a healthy circuit:
# while PWM runs it means the negative half isn't at -12 V (missing EV diode
# or pilot shorted to PE).
CP_HIGH_IMPOSSIBLE_V = 13.5


@dataclass
class Authorization:
    id_tag: str
    source: str          # "Local" (card) | "Remote" (RemoteStartTransaction)
    expires_at: float    # time.monotonic()


class ChargePoint:
    def __init__(
        self,
        config: ChargePointConfig,
        client: OcppClient,
        display: IDisplayDriver,
        meter: IMeterReader,
        cp_reader: Optional[ICpSignalReader],
        logger: Callable[[str, str], None],
        pwm: Optional[IPilotPwm] = None,
        nfc: Optional[INfcReader] = None,
    ):
        self.config = config
        self.client = client
        self.display = display
        self.meter = meter
        self.cp_reader = cp_reader
        self.pwm: IPilotPwm = pwm if pwm is not None else MockPilotPwm()
        self.nfc = nfc
        self.log = logger
        self.thresholds = CpThresholds(
            a=config.cp_threshold_a, b=config.cp_threshold_b,
            c=config.cp_threshold_c, d=config.cp_threshold_d,
        )

        # Transaction
        self.transaction_id: Optional[int] = None
        self.session: Optional[MeterSession] = None
        self.active_id_tag: Optional[str] = None
        self.charging_active: bool = False
        self.charge_start_time: Optional[float] = None
        self.transaction_end_time: Optional[float] = None
        self.pending_auth: Optional[Authorization] = None
        self.finishing: bool = False

        # Pilot / proximity
        self.pilot_state: Optional[str] = None       # "A".."E"
        self._pilot_candidate: Optional[str] = None
        self._pilot_count: int = 0
        self.last_cp_reading: Optional[CpReading] = None
        self.last_pilot_high: Optional[float] = None
        self.cable_max_current: Optional[float] = None

        # Availability (ChangeAvailability)
        self.availability: str = "Operative"
        self._pending_availability: Optional[str] = None

        # OCPP link
        self.registered: bool = False                # BootNotification accepted
        self._last_sent_status: Optional[Tuple[str, str]] = None
        self.connector_status: str = ChargePointStatus.AVAILABLE.value
        self.connector_error: str = ErrorCode.NO_ERROR.value
        self._offline_queue: List[Tuple[str, dict]] = []
        self._reset_requested: bool = False

        # Runtime
        self.current_status: str = "NOT CONNECTED"   # text shown on Nextion
        self._page: Optional[str] = None
        self._lock_obj: Optional[asyncio.Lock] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._bg: Set[asyncio.Task] = set()
        self._last_warn: Dict[str, float] = {}

    # ─── Helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def iso_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    @property
    def online(self) -> bool:
        return self.client.is_connected and self.registered

    @property
    def transaction_active(self) -> bool:
        return self.transaction_id is not None

    def _warn_rl(self, key: str, msg: str, every: float = 5.0) -> None:
        now = time.monotonic()
        if now - self._last_warn.get(key, 0.0) >= every:
            self._last_warn[key] = now
            self.log("WARN", msg)

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _call(self, action: Action, payload: dict, queue_if_offline: bool = False) -> Optional[dict]:
        """CALL that never raises; returns None on failure."""
        if not self.online:
            if queue_if_offline:
                self._offline_queue.append((action.value, payload))
                self.log("WARN", f"CSMS çevrimdışı — {action.value} kuyruğa alındı")
            else:
                self.log("WARN", f"CSMS çevrimdışı — {action.value} gönderilmedi")
            return None
        try:
            return await self.client.call(action.value, payload)
        except OcppCallError as e:
            self.log("ERR", f"{action.value} CALLERROR: {e}")
        except (NotConnectedError, TimeoutError, ConnectionError, OSError) as e:
            self.log("ERR", f"{action.value} başarısız: {e}")
            if queue_if_offline:
                self._offline_queue.append((action.value, payload))
                self.log("WARN", f"{action.value} kuyruğa alındı (bağlanınca gönderilecek)")
        return None

    # ─── Meter ───────────────────────────────────────────────────────────────
    async def _read_meter_async(self) -> Optional[int]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self.meter.read_wh)
        except Exception as e:
            self.log("ERR", f"Sayaç okuma hatası: {e}")
            return None

    async def _read_actual_meter(self, context: str) -> Optional[int]:
        reading_wh = await self._read_meter_async()
        if reading_wh is None:
            self.log("ERR", f"{context}: sayac okunamadi")
            return None
        if self.session is None:
            return reading_wh
        reported = self.session.update(reading_wh)
        self.log("INFO", f"{context}: sayac={reading_wh} Wh, kullanim={self.session.energy_kwh:.3f} kWh")
        return reported

    def offered_current(self) -> float:
        amps = self.config.max_current_a
        if self.cable_max_current is not None:
            amps = min(amps, self.cable_max_current)
        return amps

    # ─── OCPP CP→CSMS messages ───────────────────────────────────────────────
    async def authorize(self, id_tag: str) -> bool:
        resp = await self._call(Action.AUTHORIZE, {"idTag": id_tag})
        ok = id_tag_accepted(resp)
        self.log("INFO" if ok else "WARN", f"Authorize {id_tag}: {id_tag_status(resp)}")
        return ok

    async def _send_status(self, status: str, error_code: str, info: Optional[str] = None) -> None:
        payload = {
            "connectorId": CONNECTOR_ID,
            "status": status,
            "errorCode": error_code,
            "timestamp": self.iso_now(),
        }
        if info:
            payload["info"] = info[:50]
        if await self._call(Action.STATUS_NOTIFICATION, payload) is not None:
            self._last_sent_status = (status, error_code)

    async def status_notification(self, connector_id: int, status: str, error_code: str = "NoError") -> None:
        """Direct StatusNotification (kept for TriggerMessage / external use)."""
        await self._call(Action.STATUS_NOTIFICATION, {
            "connectorId": connector_id, "status": status,
            "errorCode": error_code, "timestamp": self.iso_now(),
        })

    async def meter_values(self) -> None:
        async with self._lock():
            await self._meter_values_locked()

    async def _meter_values_locked(self) -> None:
        reading_wh = await self._read_actual_meter("MeterValues")
        if reading_wh is None:
            return
        power_w = self.session.measured_power_w if (self.session and self.transaction_active) else 0
        sampled = [
            {"value": str(reading_wh), "context": "Sample.Periodic",
             "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
            {"value": str(power_w), "context": "Sample.Periodic",
             "measurand": "Power.Active.Import", "unit": "W"},
            {"value": f"{(self.offered_current() if self.pwm.offering else 0):.1f}", "context": "Sample.Periodic",
             "measurand": "Current.Offered", "unit": "A"},
        ]
        payload = {"connectorId": CONNECTOR_ID,
                   "meterValue": [{"timestamp": self.iso_now(), "sampledValue": sampled}]}
        if self.transaction_id is not None:
            payload["transactionId"] = self.transaction_id
        await self._call(Action.METER_VALUES, payload)

    # ─── Transaction control (call with lock held) ───────────────────────────
    async def _start_transaction_locked(self) -> None:
        auth = self.pending_auth
        if auth is None or self.transaction_active:
            return
        if self.config.pp_enabled and self.config.pp_required and self.cable_max_current is None:
            self._warn_rl("pp", "Kablo (PP) algılanmadı — şarj başlatılmıyor")
            return

        start_wh = await self._read_meter_async()
        if start_wh is None:
            self.log("ERR", "StartTransaction iptal: sayaç okunamadı")
            self.pending_auth = None
            return

        resp = await self._call(Action.START_TRANSACTION, {
            "connectorId": CONNECTOR_ID,
            "idTag": auth.id_tag,
            "meterStart": int(start_wh),
            "timestamp": self.iso_now(),
        })
        self.pending_auth = None
        if not resp or "transactionId" not in resp:
            self.log("ERR", "StartTransaction yanıtı alınamadı — oturum açılmadı")
            return

        self.transaction_id = resp["transactionId"]
        self.active_id_tag = auth.id_tag
        self.session = MeterSession(int(start_wh), self.log)
        self.charge_start_time = time.time()
        self.transaction_end_time = None
        self.charging_active = True
        self.finishing = False
        self.log("INFO", f"Transaction ID={self.transaction_id} idTag={auth.id_tag} meterStart={start_wh} Wh")

        if not id_tag_accepted(resp):
            self.log("WARN", f"StartTransaction idTagInfo={id_tag_status(resp)}")
            if self.config.stop_transaction_on_invalid_id:
                await self._stop_transaction_locked(StopReason.DE_AUTHORIZED)

    async def _stop_transaction_locked(self, reason: StopReason) -> None:
        if self.transaction_id is None:
            return
        self.pwm.standby()  # withdraw energy first

        stop_wh = await self._read_actual_meter("StopTransaction")
        if stop_wh is None:
            stop_wh = self.session.meter_wh if self.session else 0
            self.log("WARN", f"StopTransaction: son geçerli sayaç değeri kullanılıyor: {stop_wh} Wh")

        payload = {
            "transactionId": self.transaction_id,
            "idTag": self.active_id_tag or "",
            "meterStop": int(stop_wh),
            "timestamp": self.iso_now(),
            "reason": reason.value,
        }
        tx = self.transaction_id
        self.transaction_id = None
        self.charging_active = False
        self.transaction_end_time = time.time()
        self.finishing = self.pilot_state in ("B", "C")

        await self._call(Action.STOP_TRANSACTION, payload, queue_if_offline=True)
        self.log("INFO", f"Şarj durduruldu: tx={tx} reason={reason.value} meterStop={stop_wh} Wh")

    async def stop_transaction(self, reason: StopReason = StopReason.LOCAL) -> None:
        async with self._lock():
            if self.transaction_id is None:
                self.log("WARN", "Aktif işlem yok!")
                return
            await self._stop_transaction_locked(reason)
            await self._evaluate_locked()

    # ─── State evaluation ────────────────────────────────────────────────────
    def compute_status(self) -> Tuple[str, str, Optional[str]]:
        p = self.pilot_state
        S, E = ChargePointStatus, ErrorCode
        if p == "E":
            return S.FAULTED.value, E.EV_COMMUNICATION_ERROR.value, "Pilot fault (short / no diode)"
        if p == "D":
            return S.FAULTED.value, E.OTHER_ERROR.value, "Ventilation not supported"
        if self.transaction_active:
            return (S.CHARGING.value if p == "C" else S.SUSPENDED_EV.value), E.NO_ERROR.value, None
        if self.availability == "Inoperative":
            return S.UNAVAILABLE.value, E.NO_ERROR.value, None
        if p in ("B", "C"):
            return (S.FINISHING.value if self.finishing else S.PREPARING.value), E.NO_ERROR.value, None
        return (S.PREPARING.value if self.pending_auth else S.AVAILABLE.value), E.NO_ERROR.value, None

    async def _evaluate_locked(self) -> None:
        p = self.pilot_state

        if self.pending_auth and not self.transaction_active and time.monotonic() > self.pending_auth.expires_at:
            self.log("WARN", f"ConnectionTimeOut: {self.pending_auth.id_tag} için araç takılmadı, yetki düştü")
            self.pending_auth = None

        # Vehicle-side events
        if self.transaction_active:
            if p == "A":
                await self._stop_transaction_locked(StopReason.EV_DISCONNECTED)
            elif p in ("D", "E"):
                await self._stop_transaction_locked(StopReason.OTHER)
        elif (p in ("B", "C") and self.pending_auth is not None
              and self.availability == "Operative"):
            await self._start_transaction_locked()

        if p == "A":
            self.finishing = False

        if self._pending_availability and not self.transaction_active:
            self.availability = self._pending_availability
            self._pending_availability = None
            self.log("INFO", f"Availability → {self.availability}")

        # Pilot output
        if self.transaction_active and p in ("B", "C"):
            self.pwm.offer_current(self.offered_current())
        else:
            self.pwm.standby()

        # Status
        status, err, info = self.compute_status()
        self.connector_status, self.connector_error = status, err
        if (status, err) != self._last_sent_status and self.online:
            await self._send_status(status, err, info)
        self._refresh_display(status)

    def _refresh_display(self, status: Optional[str] = None) -> None:
        status = status or self.connector_status
        if not self.client.is_connected and not self.transaction_active:
            text = "NOT CONNECTED"
        else:
            text = status.upper()
        self.current_status = text
        self.display.set_status(text)

        busy = self.pending_auth or self.transaction_active or self.finishing \
            or status in (ChargePointStatus.FAULTED.value, ChargePointStatus.UNAVAILABLE.value)
        page = "home" if busy else "rfid_scan"
        if page != self._page:
            self._page = page
            self.display.show_page(page)

    async def evaluate(self) -> None:
        async with self._lock():
            await self._evaluate_locked()

    # ─── Inputs: pilot ───────────────────────────────────────────────────────
    def classify_adc(self, v_adc: float) -> Tuple[Optional[str], float, float]:
        c = self.config
        v_cp = cp_voltage_from_adc(v_adc, c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup, c.adc_vref)
        duty = self.pwm.duty
        if duty <= 0.0:
            return None, v_cp, v_cp  # we're holding -12 V; nothing to classify
        v_high = pilot_high_from_average(v_cp, duty, c.cp_low_voltage)
        if v_high > CP_HIGH_IMPOSSIBLE_V:
            return "E", v_cp, v_high
        return pilot_state_from_voltage(v_high, self.thresholds), v_cp, v_high

    async def process_pilot(self, state: Optional[str], debounce: bool = True) -> None:
        if state is None:
            return
        # Safety first: drop PWM immediately, without waiting for the lock.
        if state in ("A", "D", "E") and self.pwm.offering:
            self.pwm.standby()

        if debounce:
            if state == self._pilot_candidate:
                self._pilot_count += 1
            else:
                self._pilot_candidate, self._pilot_count = state, 1
            if self._pilot_count < 2:
                return

        if state != self.pilot_state:
            v = f" ({self.last_pilot_high:.2f} V)" if self.last_pilot_high is not None else ""
            self.log("INFO", f"CP durum: {self.pilot_state or '-'} → {state}{v}")
            self.pilot_state = state
        await self.evaluate()

    def _update_pp(self, v_adc: Optional[float]) -> None:
        if v_adc is None:
            return
        rc = pp_resistance_from_adc(v_adc, self.config.pp_pullup_ohm, self.config.adc_vref)
        amps = cable_current_from_pp(rc)
        if amps != self.cable_max_current:
            desc = "kablo yok" if rc is None else f"Rc={rc:.0f} Ω → {amps or 'geçersiz'} A"
            self.log("INFO", f"PP: {desc}")
            self.cable_max_current = amps

    async def pilot_loop(self) -> None:
        if self.cp_reader is None or not self.config.cp_adc_enabled:
            self.log("WARN", "CP okuyucu yok — pilot durumu yalnızca konsoldan değiştirilebilir")
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                v_adc = await loop.run_in_executor(None, self.cp_reader.read_cp_adc)
                state, v_cp, v_high = self.classify_adc(v_adc)
                self.last_cp_reading = CpReading(adc_voltage=v_adc, cp_voltage=v_cp)
                self.last_pilot_high = v_high
                if self.config.pp_enabled:
                    self._update_pp(await loop.run_in_executor(None, self.cp_reader.read_pp_adc))
                await self.process_pilot(state)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._warn_rl("cp", f"CP okuma hatası: {e}")
            await asyncio.sleep(self.config.cp_status_interval)

    # ─── Inputs: card ────────────────────────────────────────────────────────
    async def on_card(self, uid_hex: str) -> None:
        id_tag = self.config.id_tag_for_uid(uid_hex)
        self.log("INFO", f"Kart okundu: UID={uid_hex.upper()} → idTag={id_tag}")
        async with self._lock():
            if self.transaction_active:
                if id_tag == self.active_id_tag:
                    await self._stop_transaction_locked(StopReason.LOCAL)
                else:
                    self.log("WARN", "Aktif oturum başka bir karta ait — yok sayıldı")
            elif self.pending_auth and self.pending_auth.id_tag == id_tag:
                self.log("INFO", "Aynı kart tekrar okutuldu — yetkilendirme iptal edildi")
                self.pending_auth = None
            elif self.availability != "Operative":
                self.log("WARN", "Konnektör kullanım dışı (Inoperative) — kart reddedildi")
            elif self.pilot_state in ("D", "E"):
                self.log("WARN", "Pilot hatası varken şarj başlatılamaz")
            elif not self.online:
                self.log("WARN", "CSMS bağlantısı yok — kart doğrulanamıyor")
            elif await self.authorize(id_tag):
                self.pending_auth = Authorization(id_tag, "Local", time.monotonic() + self.config.connection_timeout)
                self.finishing = False
                if self.pilot_state not in ("B", "C"):
                    self.log("INFO", f"Yetki alındı — {self.config.connection_timeout}s içinde aracı takın")
            await self._evaluate_locked()

    async def nfc_loop(self) -> None:
        if self.nfc is None:
            return
        loop = asyncio.get_running_loop()
        last_uid, last_ts = None, 0.0
        while True:
            try:
                uid = await loop.run_in_executor(None, self.nfc.read_uid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._warn_rl("nfc", f"NFC okuma hatası: {e}")
                await asyncio.sleep(1.0)
                continue
            if uid:
                h = uid.hex().upper()
                now = time.monotonic()
                same = h == last_uid and now - last_ts < 3.0
                last_uid, last_ts = h, now
                if not same:  # card held on the reader doesn't toggle the session
                    await self.on_card(h)
            await asyncio.sleep(self.config.nfc_poll_interval)

    # ─── Inputs: CSMS (server CALLs) ─────────────────────────────────────────
    # Handlers must not await CALLs or the state lock (that would block the
    # receive loop). They decide from current state and return a follow-up.
    async def handle_server_request(self, action: str, payload: dict):
        handlers = {
            Action.REMOTE_START_TRANSACTION.value: self._on_remote_start,
            Action.REMOTE_STOP_TRANSACTION.value: self._on_remote_stop,
            Action.RESET.value: self._on_reset,
            Action.CHANGE_AVAILABILITY.value: self._on_change_availability,
            Action.GET_CONFIGURATION.value: self._on_get_configuration,
            Action.CHANGE_CONFIGURATION.value: self._on_change_configuration,
            Action.TRIGGER_MESSAGE.value: self._on_trigger_message,
            Action.UNLOCK_CONNECTOR.value: lambda p: {"status": "NotSupported"},
            Action.CLEAR_CACHE.value: lambda p: {"status": "Accepted"},
            Action.DATA_TRANSFER.value: lambda p: {"status": "UnknownVendorId"},
        }
        handler = handlers.get(str(action))
        if handler is None:
            raise OcppCallError(CallErrorCode.NOT_IMPLEMENTED, f"{action} is not supported")
        return handler(payload)

    def _on_remote_start(self, p: dict):
        id_tag = p.get("idTag")
        if not isinstance(id_tag, str) or not id_tag:
            raise OcppCallError(CallErrorCode.FORMATION_VIOLATION, "idTag is required")
        connector = p.get("connectorId", CONNECTOR_ID)
        busy = (self.transaction_active or self.pending_auth is not None
                or self.availability != "Operative" or self.pilot_state in ("D", "E"))
        if connector != CONNECTOR_ID or busy:
            return {"status": "Rejected"}

        self.pending_auth = Authorization(id_tag, "Remote", time.monotonic() + self.config.connection_timeout)
        self.finishing = False

        async def followup():
            async with self._lock():
                if self.config.authorize_remote_tx_requests and self.pending_auth \
                        and self.pending_auth.source == "Remote":
                    if not await self.authorize(id_tag):
                        self.pending_auth = None
                await self._evaluate_locked()

        return CallResult({"status": "Accepted"}, followup)

    def _on_remote_stop(self, p: dict):
        tid = p.get("transactionId")
        if self.transaction_id is None or tid != self.transaction_id:
            return {"status": "Rejected"}

        async def followup():
            async with self._lock():
                if self.transaction_id == tid:
                    await self._stop_transaction_locked(StopReason.REMOTE)
                await self._evaluate_locked()

        return CallResult({"status": "Accepted"}, followup)

    def _on_reset(self, p: dict):
        kind = p.get("type", "Soft")
        if kind not in ("Soft", "Hard"):
            raise OcppCallError(CallErrorCode.PROPERTY_CONSTRAINT_VIOLATION, "type must be Soft or Hard")
        reason = StopReason.HARD_RESET if kind == "Hard" else StopReason.SOFT_RESET

        async def followup():
            async with self._lock():
                await self._stop_transaction_locked(reason)
                self.pending_auth = None
                await self._evaluate_locked()
            self.log("INFO", f"{kind} reset — yeniden bağlanıp BootNotification gönderilecek")
            self._reset_requested = True
            await self.client.disconnect()

        return CallResult({"status": "Accepted"}, followup)

    def _on_change_availability(self, p: dict):
        connector = p.get("connectorId")
        kind = p.get("type")
        if kind not in ("Operative", "Inoperative"):
            raise OcppCallError(CallErrorCode.PROPERTY_CONSTRAINT_VIOLATION, "type must be Operative or Inoperative")
        if connector not in (0, CONNECTOR_ID):
            return {"status": "Rejected"}
        if kind == "Inoperative" and self.transaction_active:
            self._pending_availability = kind
            status = "Scheduled"
        else:
            self.availability = kind
            self._pending_availability = None
            if kind == "Inoperative":
                self.pending_auth = None
            status = "Accepted"
        return CallResult({"status": status}, self.evaluate)

    # OCPP configuration keys: name → (getter, setter-or-None)
    def _config_keys(self) -> Dict[str, Tuple[Callable[[], str], Optional[Callable[[str], None]]]]:
        c = self.config

        def int_setter(attr: str, minimum: int = 1):
            def _set(v: str):
                n = int(v)
                if n < minimum:
                    raise ValueError
                setattr(c, attr, n)
            return _set

        def bool_setter(attr: str):
            def _set(v: str):
                if v.lower() not in ("true", "false"):
                    raise ValueError
                setattr(c, attr, v.lower() == "true")
            return _set

        b = lambda x: "true" if x else "false"  # noqa: E731
        return {
            "HeartbeatInterval": (lambda: str(c.heartbeat_interval), int_setter("heartbeat_interval")),
            "MeterValueSampleInterval": (lambda: str(c.meter_value_sample_interval), int_setter("meter_value_sample_interval")),
            "ConnectionTimeOut": (lambda: str(c.connection_timeout), int_setter("connection_timeout")),
            "AuthorizeRemoteTxRequests": (lambda: b(c.authorize_remote_tx_requests), bool_setter("authorize_remote_tx_requests")),
            "StopTransactionOnInvalidId": (lambda: b(c.stop_transaction_on_invalid_id), bool_setter("stop_transaction_on_invalid_id")),
            "NumberOfConnectors": (lambda: "1", None),
            "StopTransactionOnEVSideDisconnect": (lambda: "true", None),
            "UnlockConnectorOnEVSideDisconnect": (lambda: "false", None),
            "LocalPreAuthorize": (lambda: "false", None),
            "LocalAuthorizeOffline": (lambda: "false", None),
            "MeterValuesSampledData": (lambda: "Energy.Active.Import.Register,Power.Active.Import,Current.Offered", None),
            "SupportedFeatureProfiles": (lambda: "Core,RemoteTrigger", None),
            "WebSocketPingInterval": (lambda: str(c.ping_interval or 0), None),
        }

    def _on_get_configuration(self, p: dict):
        keys = self._config_keys()
        requested = p.get("key") or list(keys.keys())
        known, unknown = [], []
        for k in requested:
            if k in keys:
                getter, setter = keys[k]
                known.append({"key": k, "readonly": setter is None, "value": getter()})
            else:
                unknown.append(k)
        result = {"configurationKey": known}
        if unknown:
            result["unknownKey"] = unknown
        return result

    def _on_change_configuration(self, p: dict):
        key, value = p.get("key"), p.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            raise OcppCallError(CallErrorCode.FORMATION_VIOLATION, "key and value must be strings")
        keys = self._config_keys()
        if key not in keys:
            return {"status": "NotSupported"}
        setter = keys[key][1]
        if setter is None:
            return {"status": "Rejected"}
        try:
            setter(value)
        except (ValueError, TypeError):
            return {"status": "Rejected"}
        self.log("INFO", f"ChangeConfiguration {key}={value}")

        if key == "HeartbeatInterval":
            async def followup():
                if self.client.is_connected:
                    await self.client.start_heartbeat(self.config.heartbeat_interval)
            return CallResult({"status": "Accepted"}, followup)
        return {"status": "Accepted"}

    def _on_trigger_message(self, p: dict):
        req = p.get("requestedMessage")
        connector = p.get("connectorId")
        if connector is not None and connector not in (0, CONNECTOR_ID):
            return {"status": "Rejected"}

        async def resend_status():
            status, err, info = self.compute_status()
            await self._send_status(status, err, info)

        async def heartbeat():
            await self._call(Action.HEARTBEAT, {})

        async def boot():
            resp = await self._call(Action.BOOT_NOTIFICATION, self._boot_payload())
            if resp and resp.get("status") == "Accepted" and resp.get("interval"):
                self.config.heartbeat_interval = int(resp["interval"])
                await self.client.start_heartbeat(self.config.heartbeat_interval)

        actions = {
            "StatusNotification": resend_status,
            "Heartbeat": heartbeat,
            "BootNotification": boot,
            "MeterValues": self.meter_values,
        }
        if req not in actions:
            return {"status": "NotImplemented"}
        return CallResult({"status": "Accepted"}, actions[req])

    # ─── Connection lifecycle ────────────────────────────────────────────────
    def _boot_payload(self) -> dict:
        return {
            "chargePointVendor": self.config.vendor,
            "chargePointModel": self.config.model,
            "chargePointSerialNumber": self.config.serial,
            "firmwareVersion": self.config.firmware,
        }

    async def boot_notification(self) -> dict:
        return await self.client.call(Action.BOOT_NOTIFICATION.value, self._boot_payload())

    async def _boot(self) -> None:
        """Send BootNotification until Accepted (Pending/Rejected → wait interval)."""
        self.registered = False
        while True:
            resp = await self.boot_notification()
            status = resp.get("status")
            interval = int(resp.get("interval") or 0)
            self.log("INFO", f"BootNotification: status={status}, interval={interval}s")
            if status == "Accepted":
                if interval > 0:
                    self.config.heartbeat_interval = interval
                self.registered = True
                return
            await asyncio.sleep(interval if interval > 0 else 30)

    async def _flush_offline_queue(self) -> None:
        while self._offline_queue and self.online:
            action, payload = self._offline_queue[0]
            try:
                await self.client.call(action, payload)
            except OcppCallError as e:
                self.log("ERR", f"Kuyruktaki {action} reddedildi: {e}")
            except Exception as e:
                self.log("WARN", f"Kuyruk gönderimi durdu: {e}")
                return
            self._offline_queue.pop(0)

    async def _connection_loop(self) -> None:
        while True:
            await self.client.connect_with_retry()
            recv_task = asyncio.ensure_future(self.client.recv_loop())
            try:
                if not self.registered or self._reset_requested:
                    self._reset_requested = False
                    await self._boot()
                await self.client.start_heartbeat(self.config.heartbeat_interval)
                await self._flush_offline_queue()
                async with self._lock():
                    self._last_sent_status = None   # resend current status
                    await self._evaluate_locked()
                await recv_task
            except asyncio.CancelledError:
                recv_task.cancel()
                raise
            except Exception as e:
                self.log("ERR", f"Bağlantı hatası: {e}")
            finally:
                self.client.stop_heartbeat()
                if not recv_task.done():
                    recv_task.cancel()
                await self.client.disconnect()
            self._refresh_display()
            self.log("WARN", "CSMS bağlantısı koptu — yeniden bağlanılıyor")
            await asyncio.sleep(1)

    # ─── UI loops ────────────────────────────────────────────────────────────
    def _update_display_status(self) -> None:
        if self.charge_start_time is not None and self.session is not None:
            if self.charging_active:
                elapsed = time.time() - self.charge_start_time
            elif self.transaction_end_time:
                elapsed = self.transaction_end_time - self.charge_start_time
            else:
                elapsed = 0
            energy_kwh = self.session.energy_kwh
            cost = energy_kwh * self.config.cost_per_kwh
            power_kw = self.session.measured_power_w / 1000.0 if self.charging_active else 0.0
            self.display.update_charging_info(energy_kwh, power_kw, elapsed, cost, self.config.currency)
        else:
            self.display.update_charging_info(0.0, 0.0, -1, 0.0, self.config.currency)

    def _display_id_tag(self) -> str:
        if self.transaction_active and self.active_id_tag:
            return self.active_id_tag
        if self.pending_auth:
            return self.pending_auth.id_tag
        return self.active_id_tag or "-"

    async def clock_loop(self) -> None:
        while True:
            self.display.set_time()
            self.display.set_status(self.current_status)
            self.display.set_user_id(self._display_id_tag())
            self._update_display_status()
            await asyncio.sleep(1)

    async def meter_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.meter_value_sample_interval)
            if self.transaction_active:
                try:
                    await self.meter_values()
                except Exception as e:
                    self.log("ERR", f"MeterValues hatası: {e}")

    async def auth_timeout_loop(self) -> None:
        # Ensures ConnectionTimeOut fires even without a pilot reader.
        while True:
            await asyncio.sleep(1)
            if self.pending_auth and time.monotonic() > self.pending_auth.expires_at:
                await self.evaluate()

    async def nextion_touch_loop(self) -> None:
        async for event in self.display.read_touch_events():
            if event.event_type == 0x01:
                self.log("INFO", f"Nextion touch → page={event.page_id} comp={event.component_id}")
                if event.component_id == TOUCH_COMP_USER_ID_TAG:
                    self.display.set_user_id(self._display_id_tag())

    # ─── Console ─────────────────────────────────────────────────────────────
    HELP = """Komutlar:
  card <UID>        kart okutmayı simüle et (ör. card 97350E07)
  plug / unplug     (sim) aracı tak / çıkar
  ev <A|B|C|D|E>    (sim) pilot durumunu elle ayarla
  pp <13|20|32|63|none>  (sim) kablo kapasitesi
  stop              aktif şarjı yerelden durdur
  status            durumu göster
  q                 çıkış"""

    def print_info_banner(self) -> None:
        sim = isinstance(self.cp_reader, MockCpReader)
        print(f"""
\033[1m\033[36m╔═══════════════════════════════════════════════════╗\033[0m
\033[1m\033[36m║  OCPP 1.6J · EVSE Kontrolcü                       ║\033[0m
\033[1m\033[36m╚═══════════════════════════════════════════════════╝\033[0m
  CSMS : {self.config.csms_url}
  Mod  : {'SİMÜLASYON (sanal araç)' if sim else 'DONANIM (ADS1115 + PWM)'}
  Akım : {self.config.max_current_a:.0f} A
{self.HELP}
""")

    def status_text(self) -> str:
        r = self.last_cp_reading
        cp = f"adc={r.adc_voltage:.3f}V cp_avg={r.cp_voltage:.2f}V high={self.last_pilot_high:.2f}V" if r else "okuma yok"
        return (f"CSMS={'bağlı' if self.online else 'yok'} status={self.connector_status} pilot={self.pilot_state} "
                f"pwm={self.pwm.duty * 100:.1f}% tx={self.transaction_id} idTag={self._display_id_tag()} "
                f"kablo={self.cable_max_current} {cp}")

    async def console_input(self) -> None:
        loop = asyncio.get_running_loop()
        self.print_info_banner()
        sim = self.cp_reader if isinstance(self.cp_reader, MockCpReader) else None
        while True:
            try:
                line = await loop.run_in_executor(None, input, "\n\033[1m>\033[0m ")
            except (EOFError, KeyboardInterrupt):
                return  # no interactive console (e.g. systemd) — keep running
            parts = line.strip().split()
            if not parts:
                continue
            cmd, args = parts[0].lower(), parts[1:]
            try:
                if cmd in ("q", "quit", "exit"):
                    if self._stop_event:
                        self._stop_event.set()
                    return
                elif cmd == "card" and args:
                    await self.on_card(args[0])
                elif cmd == "stop":
                    await self.stop_transaction(StopReason.LOCAL)
                elif cmd == "status":
                    self.log("INFO", self.status_text())
                elif cmd in ("plug", "unplug", "ev", "pp") and sim is None:
                    self.log("WARN", "Bu komut yalnızca simülasyon modunda çalışır")
                elif cmd == "plug":
                    sim.state, sim.auto_ev = "B", True
                elif cmd == "unplug":
                    sim.state = "A"
                elif cmd == "ev" and args and args[0].upper() in "ABCDE":
                    sim.state, sim.auto_ev = args[0].upper(), False
                elif cmd == "pp" and args:
                    ohms = {"13": 1500.0, "20": 680.0, "32": 220.0, "63": 100.0}
                    sim.pp_ohm = ohms.get(args[0])
                else:
                    print(self.HELP)
            except Exception as e:
                self.log("ERR", f"Komut hatası: {e}")

    # ─── Main ────────────────────────────────────────────────────────────────
    async def run(self, interactive: Optional[bool] = None) -> None:
        self._stop_event = asyncio.Event()
        if interactive is None:
            interactive = sys.stdin is not None and sys.stdin.isatty()

        self.pwm.standby()
        self._refresh_display()
        self.display.set_time()
        self.client.set_server_request_handler(self.handle_server_request)

        loops = [self.pilot_loop(), self.nfc_loop(), self.clock_loop(), self.meter_loop(),
                 self.auth_timeout_loop(), self.nextion_touch_loop()]
        if interactive:
            loops.append(self.console_input())
        tasks = [asyncio.ensure_future(c) for c in loops]
        conn = asyncio.ensure_future(self._connection_loop())
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait([conn, stop], return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.log("INFO", "Kapatılıyor...")
            try:
                async with self._lock():
                    await self._stop_transaction_locked(StopReason.LOCAL)
                if self.online:
                    await self._send_status(ChargePointStatus.UNAVAILABLE.value, ErrorCode.NO_ERROR.value)
            except Exception as e:
                self.log("WARN", f"Kapanışta hata: {e}")
            self.pwm.standby()
            for t in tasks + [conn, stop]:
                t.cancel()
            await self.client.disconnect()
            self.display.set_status("NOT CONNECTED")
            self.display.show_page("rfid_scan")
