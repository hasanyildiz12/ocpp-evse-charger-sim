import pytest

from ocpp_charger.config import ChargePointConfig, parse_id_tag_map


def test_defaults_have_no_credentials():
    c = ChargePointConfig()
    assert c.basic_auth_password == ""
    assert "powerfill" not in c.csms_url
    assert c.hardware_mode == "auto"
    assert c.max_current_a == 16.0
    assert (c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup) == (200_000.0, 56_000.0, 47_000.0)


def test_from_env(monkeypatch):
    monkeypatch.setenv("CSMS_URL", "wss://test.url/ws/CP-2")
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", "secret")
    monkeypatch.setenv("MAX_CURRENT_A", "10")
    monkeypatch.setenv("NFC_ENABLED", "false")
    monkeypatch.setenv("PING_INTERVAL", "none")
    monkeypatch.setenv("HEARTBEAT_INTERVAL", "60")
    monkeypatch.setenv("NFC_ID_TAG_MAP", "97350e07:tagA, 11:tagB")
    c = ChargePointConfig.from_env()
    assert c.csms_url == "wss://test.url/ws/CP-2"
    assert c.basic_auth_password == "secret"
    assert c.max_current_a == 10.0
    assert c.nfc_enabled is False
    assert c.ping_interval is None
    assert c.heartbeat_interval == 60
    assert c.id_tag_for_uid("97350E07") == "tagA"
    assert c.id_tag_for_uid("abcd") == "ABCD"


def test_from_env_requires_csms_url(monkeypatch):
    monkeypatch.delenv("CSMS_URL", raising=False)
    monkeypatch.setattr("ocpp_charger.config.load_dotenv", lambda: None)
    with pytest.raises(ValueError, match="CSMS_URL"):
        ChargePointConfig.from_env()
    assert ChargePointConfig.from_env(require_csms=False).csms_url.startswith("ws")


def test_validation_csms_url():
    with pytest.raises(ValueError, match="CSMS_URL must start with ws:// or wss://"):
        ChargePointConfig(csms_url="http://test.com")


def test_validation_heartbeat():
    with pytest.raises(ValueError, match="HEARTBEAT_INTERVAL must be > 0"):
        ChargePointConfig(heartbeat_interval=0)


def test_validation_current_and_mode():
    with pytest.raises(ValueError):
        ChargePointConfig(max_current_a=5)
    with pytest.raises(ValueError):
        ChargePointConfig(hardware_mode="foo")


def test_parse_id_tag_map_rejects_bad_item():
    with pytest.raises(ValueError):
        parse_id_tag_map("noColon")
