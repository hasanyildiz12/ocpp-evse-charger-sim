import os
import pytest
from ocpp_charger.config import ChargePointConfig

def test_default_values():
    config = ChargePointConfig()
    assert config.csms_url == "wss://hasan-7fap.powerfill.app/ws/CP-1"
    assert config.charge_box_id == "CP-1"
    assert config.vendor == "TestVendor"
    assert config.model == "TestModel"
    assert config.serial == "SN-001"
    assert config.firmware == "1.0.0"
    assert config.user_id_tag == "hasanyildizidtag"
    assert config.basic_auth_user == "CP-1"
    assert config.basic_auth_password == "1234567890asdfgh"
    assert config.heartbeat_interval == 30
    assert config.meter_increment_wh == 500
    assert config.default_voltage == 230
    assert config.default_current == 16
    assert config.cp_adc_enabled is True
    assert config.cp_adc_channel == 0
    assert config.cp_adc_gain == 1
    assert config.cp_voltage_divider == 4.0303
    assert config.cp_status_interval == 0.5
    assert config.subprotocol == "ocpp1.6"
    assert config.ping_interval is None
    assert config.nextion_port == "/dev/ttyAMA0"
    assert config.nextion_baudrate == 9600
    assert config.pic_car_connected == 3
    assert config.pic_car_disconnected == 0
    assert config.pic_rfid_frames == [5, 6, 7]
    assert config.max_power_kw == 22.0
    assert config.cost_per_kwh == 0.19

def test_from_env(monkeypatch):
    monkeypatch.setenv("CSMS_URL", "wss://test.url/ws/CP-2")
    monkeypatch.setenv("CHARGE_BOX_ID", "CP-2")
    monkeypatch.setenv("USER_ID_TAG", "testtag")
    monkeypatch.setenv("BASIC_AUTH_USER", "testuser")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "testpass")
    
    config = ChargePointConfig.from_env()
    assert config.csms_url == "wss://test.url/ws/CP-2"
    assert config.charge_box_id == "CP-2"
    assert config.user_id_tag == "testtag"
    assert config.basic_auth_user == "testuser"
    assert config.basic_auth_password == "testpass"

def test_validation_csms_url():
    with pytest.raises(ValueError, match="CSMS_URL must start with ws:// or wss://"):
        ChargePointConfig(csms_url="http://test.com")

def test_validation_heartbeat():
    with pytest.raises(ValueError, match="HEARTBEAT_INTERVAL must be > 0"):
        ChargePointConfig(heartbeat_interval=0)
    with pytest.raises(ValueError, match="HEARTBEAT_INTERVAL must be > 0"):
        ChargePointConfig(heartbeat_interval=-5)
