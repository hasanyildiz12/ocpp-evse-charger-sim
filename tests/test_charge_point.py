import asyncio
from datetime import datetime
import pytest

from ocpp_charger.config import ChargePointConfig
from ocpp_charger.charge_point import ChargePoint
from ocpp_charger.hardware.display import NullDisplay
from ocpp_charger.hardware.meter import IMeterReader
from ocpp_charger.hardware.cp_signal import ICpSignalReader, CpReading
from ocpp_charger.protocol.messages import Action, SERVER_ACTION_RESPONSES


class MockMeter(IMeterReader):
    def read_wh(self):
        return 1000

    def close(self):
        pass

    def configure_logger(self, log_fn):
        pass


class MockCpReader(ICpSignalReader):
    def read(self):
        return CpReading(adc_voltage=3.0, cp_voltage=12.0, status="Available")


class MockClient:
    """Minimal mock that mimics OcppClient for unit testing."""

    def __init__(self):
        self.is_connected = True
        self._handler = None

    def set_server_request_handler(self, handler):
        self._handler = handler

    async def send(self, action, payload):
        return "123"

    async def call(self, action, payload, timeout=30.0):
        return {}

    async def connect_with_retry(self, max_retries=0):
        pass

    async def disconnect(self):
        pass

    async def start_heartbeat(self, interval):
        pass

    async def recv_loop(self):
        await asyncio.sleep(999999)


def _make_charge_point(**overrides):
    """Helper to build a ChargePoint with mock dependencies."""
    defaults = dict(
        config=ChargePointConfig(),
        client=MockClient(),
        display=NullDisplay(),
        meter=MockMeter(),
        cp_reader=MockCpReader(),
        logger=lambda *args: None,
    )
    defaults.update(overrides)
    cp = ChargePoint(**defaults)
    # Wire the server request handler just like main.py / run() does
    defaults["client"].set_server_request_handler(cp.handle_server_request)
    return cp, defaults["client"]


def test_initial_state():
    cp, _ = _make_charge_point()
    assert cp.transaction_id is None
    assert cp.session is None
    assert cp.charging_active is False
    assert cp.charge_start_time is None
    assert cp.transaction_end_time is None
    assert cp.current_status == "NOT CONNECTED"
    assert cp.cp_status_source_active is False


@pytest.mark.asyncio
async def test_handle_server_request_known_actions():
    cp, client = _make_charge_point()

    # Test Reset
    resp = await cp.handle_server_request(Action.RESET, {"type": "Hard"})
    assert resp == {"status": "Accepted"}

    # Test UnlockConnector
    resp = await cp.handle_server_request(Action.UNLOCK_CONNECTOR, {"connectorId": 1})
    assert resp == {"status": "Unlocked"}

    # Test ClearCache
    resp = await cp.handle_server_request(Action.CLEAR_CACHE, {})
    assert resp == {"status": "Accepted"}

    # Test GetConfiguration
    resp = await cp.handle_server_request(Action.GET_CONFIGURATION, {})
    assert resp == {"configurationKey": [], "unknownKey": []}


@pytest.mark.asyncio
async def test_handle_server_request_unknown_action():
    cp, _ = _make_charge_point()
    resp = await cp.handle_server_request("UnknownAction", {})
    assert resp == {}


def test_iso_now_format():
    cp, _ = _make_charge_point()
    iso_str = cp.iso_now()
    assert iso_str.endswith("Z")
    # Verify it can be parsed back (remove trailing Z)
    datetime.fromisoformat(iso_str[:-1])


def test_update_display_status_no_session():
    """When there's no active session, display should show zeros."""
    cp, _ = _make_charge_point()
    # Should not raise
    cp._update_display_status()


def test_init_cp_disabled():
    """When CP ADC is disabled, init_cp_status_source returns False."""
    config = ChargePointConfig(cp_adc_enabled=False)
    cp, _ = _make_charge_point(config=config)
    assert cp.init_cp_status_source() is False
    assert cp.cp_status_source_active is False


def test_init_cp_no_reader():
    """When cp_reader is None, init_cp_status_source returns False."""
    cp, _ = _make_charge_point(cp_reader=None)
    assert cp.init_cp_status_source() is False
