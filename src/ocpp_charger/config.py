"""Configuration for the OCPP charge point.

Every field can be overridden by an environment variable with the same name
in UPPER_CASE (e.g. ``max_current_a`` ← ``MAX_CURRENT_A``). A ``.env`` file in
the working directory is loaded first if python-dotenv is installed.

No credentials live in code: ``CSMS_URL`` and the Basic-Auth values must come
from the environment / ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields
from typing import Dict, Optional


def load_dotenv() -> None:
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv()
    except ImportError:
        pass


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on", "evet"):
        return True
    if v in ("0", "false", "no", "off", "hayir", "hayır", ""):
        return False
    raise ValueError(f"Geçersiz bool değeri: {value!r}")


def parse_id_tag_map(value: str) -> Dict[str, str]:
    """``"97350E07:tagA, 11223344:tagB"`` → ``{"97350E07": "tagA", ...}``"""
    result: Dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"NFC_ID_TAG_MAP öğesi 'UID:idTag' biçiminde olmalı: {item!r}")
        uid, tag = item.split(":", 1)
        result[uid.strip().upper()] = tag.strip()
    return result


@dataclass
class ChargePointConfig:
    # ─── CSMS Connection ─────────────────────────────────────────────────────
    csms_url: str = "ws://localhost:9000/CP-1"
    charge_box_id: str = "CP-1"
    basic_auth_user: str = ""
    basic_auth_password: str = ""
    subprotocol: str = "ocpp1.6"
    ping_interval: Optional[int] = None
    call_timeout: float = 30.0

    # ─── Charge Point Identity (BootNotification) ────────────────────────────
    vendor: str = "TestVendor"
    model: str = "TestModel"
    serial: str = "SN-001"
    firmware: str = "2.1.0"

    # ─── OCPP configuration keys (defaults; CSMS may change some) ───────────
    heartbeat_interval: int = 30            # HeartbeatInterval (overridden by BootNotification.conf)
    meter_value_sample_interval: int = 10   # MeterValueSampleInterval
    connection_timeout: int = 60            # ConnectionTimeOut: auth → plug-in window
    authorize_remote_tx_requests: bool = False
    stop_transaction_on_invalid_id: bool = True

    # ─── NFC / RFID ──────────────────────────────────────────────────────────
    nfc_enabled: bool = True
    nfc_poll_interval: float = 0.3
    # Optional UID→idTag mapping. Unmapped cards use their UID (hex, upper) as idTag.
    nfc_id_tag_map: Dict[str, str] = field(default_factory=dict)

    # ─── Hardware selection ──────────────────────────────────────────────────
    # auto: use real devices when found, simulate the rest (with warnings)
    # pi  : real devices only, fail if missing
    # sim : everything simulated (desktop)
    hardware_mode: str = "auto"

    # ─── Control Pilot PWM (GPIO12 / PWM0 → LM358 comparator → ±12 V) ───────
    pwm_chip: int = 0
    pwm_channel: int = 0
    pwm_frequency_hz: int = 1000
    max_current_a: float = 16.0             # current offered to the EV (sets PWM duty)

    # ─── ADS1115 (A0 = PILOTREAD, A1 = PP) ───────────────────────────────────
    cp_adc_enabled: bool = True
    cp_adc_channel: int = 0
    pp_adc_channel: int = 1
    cp_adc_gain: int = 1
    adc_vref: float = 3.3                   # +3.3 V rail feeding R5 and R2
    # PILOT → R7 → PILOTREAD, PILOTREAD → R6 → GND, PILOTREAD → R5 → +3.3 V
    cp_r_top: float = 200_000.0             # R7
    cp_r_bottom: float = 56_000.0           # R6
    cp_r_pullup: float = 47_000.0           # R5
    cp_low_voltage: float = -12.0           # measured negative pilot level (for PWM compensation)
    cp_status_interval: float = 0.5
    # IEC 61851-1 state thresholds on the positive pilot level (V)
    cp_threshold_a: float = 10.5            # ≥ → A (no EV)
    cp_threshold_b: float = 7.5             # ≥ → B (EV connected)
    cp_threshold_c: float = 4.5             # ≥ → C (charging)
    cp_threshold_d: float = 1.5             # ≥ → D (ventilation), below → E (fault)

    # ─── Proximity Pilot (PP → R2 330 Ω → +3.3 V) ────────────────────────────
    pp_enabled: bool = True
    pp_pullup_ohm: float = 330.0            # R2
    pp_required: bool = False               # True: refuse to charge without a valid cable code

    # ─── Nextion Display ─────────────────────────────────────────────────────
    nextion_port: str = "/dev/serial0"
    nextion_baudrate: int = 9600
    pic_car_connected: int = 3
    pic_car_disconnected: int = 0

    # ─── Pricing ─────────────────────────────────────────────────────────────
    cost_per_kwh: float = 0.19
    currency: str = "$"

    @classmethod
    def from_env(cls, require_csms: bool = True) -> "ChargePointConfig":
        """Build config from environment variables (after loading .env)."""
        load_dotenv()
        if require_csms and not os.environ.get("CSMS_URL"):
            raise ValueError("CSMS_URL tanımlı değil. .env.example dosyasını .env olarak kopyalayıp doldurun.")

        kwargs = {}
        for f in fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw is not None:
                kwargs[f.name] = cls._convert(f.name, raw)
        return cls(**kwargs)

    @classmethod
    def _convert(cls, name: str, raw: str):
        sample = cls._default_of(name)
        if name == "nfc_id_tag_map":
            return parse_id_tag_map(raw)
        if name == "ping_interval":
            return None if raw.strip().lower() in ("", "none", "0") else int(raw)
        if isinstance(sample, bool):
            return _parse_bool(raw)
        if isinstance(sample, int):
            return int(raw)
        if isinstance(sample, float):
            return float(raw)
        return raw

    @classmethod
    def _default_of(cls, name: str):
        for f in fields(cls):
            if f.name == name:
                if f.default_factory is not MISSING:
                    return f.default_factory()
                return f.default
        raise KeyError(name)

    def __post_init__(self) -> None:
        if not (self.csms_url.startswith("ws://") or self.csms_url.startswith("wss://")):
            raise ValueError("CSMS_URL must start with ws:// or wss://")
        if self.heartbeat_interval <= 0:
            raise ValueError("HEARTBEAT_INTERVAL must be > 0")
        if self.meter_value_sample_interval <= 0:
            raise ValueError("METER_VALUE_SAMPLE_INTERVAL must be > 0")
        if self.hardware_mode not in ("auto", "pi", "sim"):
            raise ValueError("HARDWARE_MODE must be one of: auto, pi, sim")
        if not 6.0 <= self.max_current_a <= 80.0:
            raise ValueError("MAX_CURRENT_A must be between 6 and 80 A (IEC 61851-1)")
        if not (self.cp_threshold_a > self.cp_threshold_b > self.cp_threshold_c > self.cp_threshold_d):
            raise ValueError("CP thresholds must satisfy A > B > C > D")
        self.nfc_id_tag_map = {k.upper(): v for k, v in self.nfc_id_tag_map.items()}

    def id_tag_for_uid(self, uid_hex: str) -> str:
        uid = uid_hex.upper()
        return self.nfc_id_tag_map.get(uid, uid)
