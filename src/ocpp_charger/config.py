"""Configuration module for the OCPP Simulator."""

import os
from dataclasses import dataclass, field
from typing import Optional


def load_dotenv() -> None:
    """Load environment variables from .env file if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv()
    except ImportError:
        pass


@dataclass
class ChargePointConfig:
    """Configuration for the Charge Point Simulator."""

    # ─── CSMS Connection ─────────────────────────────────────────────────────────
    csms_url: str = "wss://hasan-7fap.powerfill.app/ws/CP-1"
    charge_box_id: str = "CP-1"

    # ─── Charge Point Identity ───────────────────────────────────────────────────
    vendor: str = "TestVendor"
    model: str = "TestModel"
    serial: str = "SN-001"
    firmware: str = "1.0.0"

    # ─── Authentication ──────────────────────────────────────────────────────────
    user_id_tag: str = "hasanyildizidtag"
    basic_auth_user: str = "CP-1"
    basic_auth_password: str = "1234567890asdfgh"



    # ─── Heartbeat ───────────────────────────────────────────────────────────────
    heartbeat_interval: int = 30

    # ─── Meter Simulation ────────────────────────────────────────────────────────
    meter_increment_wh: int = 500
    default_voltage: int = 230
    default_current: int = 16

    # --- CP Signal / ADS1115 ---
    cp_adc_enabled: bool = True
    cp_adc_channel: int = 0
    cp_adc_gain: int = 1
    cp_voltage_divider: float = 4.0303
    cp_status_interval: float = 0.5

    # ─── OCPP Protocol ───────────────────────────────────────────────────────────
    subprotocol: str = "ocpp1.6"
    ping_interval: Optional[int] = None

    # ─── Nextion Display ─────────────────────────────────────────────────────────
    nextion_port: str = "/dev/ttyAMA0"
    nextion_baudrate: int = 9600

    # ─── Nextion Picture IDs ─────────────────────────────────────────────────────
    # Görseller ID:
    #   3  → yeşil araç (bağlı)
    #   0  → yeşil araç (bağlı değil/çıkış)
    #   5,6,7 → RFID animasyon kareleri (rfid_scan sayfası)
    pic_car_connected: int = 3
    pic_car_disconnected: int = 0
    pic_rfid_frames: list[int] = field(default_factory=lambda: [5, 6, 7])

    # ─── Şarj / Ücretlendirme ────────────────────────────────────────────────────
    # 22 kW maksimum şarj kapasitesi ile matematiksel enerji ölçümü
    max_power_kw: float = 22.0
    cost_per_kwh: float = 0.19

    @classmethod
    def from_env(cls) -> "ChargePointConfig":
        """Create a ChargePointConfig instance from environment variables."""
        load_dotenv()
        return cls(
            csms_url=os.environ.get("CSMS_URL", "wss://hasan-7fap.powerfill.app/ws/CP-1"),
            charge_box_id=os.environ.get("CHARGE_BOX_ID", "CP-1"),
            user_id_tag=os.environ.get("USER_ID_TAG", "hasanyildizidtag"),
            basic_auth_user=os.environ.get("BASIC_AUTH_USER", "CP-1"),
            basic_auth_password=os.environ.get("BASIC_AUTH_PASSWORD", "1234567890asdfgh")
        )

    def __post_init__(self) -> None:
        if not (self.csms_url.startswith("ws://") or self.csms_url.startswith("wss://")):
            raise ValueError("CSMS_URL must start with ws:// or wss://")
        if self.heartbeat_interval <= 0:
            raise ValueError("HEARTBEAT_INTERVAL must be > 0")
