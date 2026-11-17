import pytest

from ocpp_charger.charge_point import ChargePoint
from ocpp_charger.config import ChargePointConfig
from ocpp_charger.hardware.display import NullDisplay
from ocpp_charger.hardware.meter import MockMeter
from ocpp_charger.hardware.pilot import MockPilotPwm


class FakeClient:
    """Records CALLs; answers with canned responses per action."""

    def __init__(self):
        self.is_connected = True
        self.calls = []
        self.handler = None
        self.heartbeat_interval = None
        self.responses = {
            "BootNotification": {"status": "Accepted", "interval": 30, "currentTime": "2026-01-01T00:00:00Z"},
            "Authorize": {"idTagInfo": {"status": "Accepted"}},
            "StartTransaction": {"transactionId": 42, "idTagInfo": {"status": "Accepted"}},
            "StopTransaction": {"idTagInfo": {"status": "Accepted"}},
        }

    def set_server_request_handler(self, handler):
        self.handler = handler

    async def call(self, action, payload, timeout=None):
        action = getattr(action, "value", action)
        self.calls.append((action, payload))
        r = self.responses.get(action, {})
        if isinstance(r, Exception):
            raise r
        return r(payload) if callable(r) else r

    async def start_heartbeat(self, interval):
        self.heartbeat_interval = interval

    def stop_heartbeat(self):
        pass

    async def disconnect(self):
        self.is_connected = False

    def actions(self):
        return [a for a, _ in self.calls]

    def last(self, action):
        for a, p in reversed(self.calls):
            if a == action:
                return p
        return None


@pytest.fixture
def make_cp():
    def _make(**cfg):
        config = ChargePointConfig(**cfg)
        client = FakeClient()
        pwm = MockPilotPwm()
        pwm.open()
        cp = ChargePoint(config=config, client=client, display=NullDisplay(),
                         meter=MockMeter(initial_wh=1000, increment_wh=100),
                         cp_reader=None, logger=lambda *a: None, pwm=pwm)
        client.set_server_request_handler(cp.handle_server_request)
        cp.registered = True
        return cp, client
    return _make
