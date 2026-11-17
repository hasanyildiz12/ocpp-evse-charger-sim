import asyncio
import json

import pytest

from ocpp_charger.protocol.client import NotConnectedError, OcppClient
from ocpp_charger.protocol.messages import CallResult, OcppCallError, id_tag_accepted


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass


def make_client():
    c = OcppClient("ws://x/CP-1", "ocpp1.6", "u", "p", None, lambda *a: None, call_timeout=1.0)
    c._ws = FakeWs()
    c._connected = True
    return c


async def test_call_resolves_with_callresult():
    c = make_client()
    task = asyncio.ensure_future(c.call("Heartbeat", {}))
    await asyncio.sleep(0)
    frame = c._ws.sent[0]
    assert frame[0] == 2 and frame[2] == "Heartbeat"
    await c._handle_raw_message(json.dumps([3, frame[1], {"currentTime": "t"}]))
    assert await task == {"currentTime": "t"}


async def test_call_raises_on_callerror():
    c = make_client()
    task = asyncio.ensure_future(c.call("Authorize", {"idTag": "x"}))
    await asyncio.sleep(0)
    mid = c._ws.sent[0][1]
    await c._handle_raw_message(json.dumps([4, mid, "FormationViolation", "bad", {}]))
    with pytest.raises(OcppCallError):
        await task


async def test_calls_are_serialised():
    c = make_client()
    t1 = asyncio.ensure_future(c.call("Heartbeat", {}))
    t2 = asyncio.ensure_future(c.call("Heartbeat", {}))
    await asyncio.sleep(0.01)
    assert len(c._ws.sent) == 1  # second waits for the first response
    await c._handle_raw_message(json.dumps([3, c._ws.sent[0][1], {}]))
    await asyncio.sleep(0.01)
    assert len(c._ws.sent) == 2
    await c._handle_raw_message(json.dumps([3, c._ws.sent[1][1], {}]))
    await asyncio.gather(t1, t2)


async def test_call_when_disconnected():
    c = make_client()
    c._connected = False
    with pytest.raises(NotConnectedError):
        await c.call("Heartbeat", {})


async def test_call_timeout():
    c = make_client()
    with pytest.raises(TimeoutError):
        await c.call("Heartbeat", {}, timeout=0.01)
    assert not c._pending


async def test_server_call_unknown_action_gets_callerror():
    c = make_client()

    async def handler(action, payload):
        raise OcppCallError("NotImplemented", f"{action} not supported")
    c.set_server_request_handler(handler)
    await c._handle_raw_message(json.dumps([2, "abc", "UpdateFirmware", {}]))
    assert c._ws.sent[0][:3] == [4, "abc", "NotImplemented"]


async def test_server_call_followup_runs_after_response():
    c = make_client()
    order = []

    async def followup():
        order.append(("followup", len(c._ws.sent)))

    async def handler(action, payload):
        return CallResult({"status": "Accepted"}, followup)
    c.set_server_request_handler(handler)
    await c._handle_raw_message(json.dumps([2, "m1", "TriggerMessage", {"requestedMessage": "Heartbeat"}]))
    await asyncio.sleep(0)
    assert c._ws.sent[0] == [3, "m1", {"status": "Accepted"}]
    assert order == [("followup", 1)]


async def test_handler_exception_gives_internal_error():
    c = make_client()

    async def handler(action, payload):
        raise RuntimeError("boom")
    c.set_server_request_handler(handler)
    await c._handle_raw_message(json.dumps([2, "m2", "Reset", {"type": "Soft"}]))
    assert c._ws.sent[0][:3] == [4, "m2", "InternalError"]


async def test_malformed_call_gets_formation_violation():
    c = make_client()
    await c._handle_raw_message(json.dumps([2, "m3", "Reset"]))
    assert c._ws.sent[0][:3] == [4, "m3", "FormationViolation"]


async def test_garbage_is_ignored():
    c = make_client()
    await c._handle_raw_message("not json")
    await c._handle_raw_message(json.dumps({"a": 1}))
    assert c._ws.sent == []


def test_id_tag_accepted():
    assert id_tag_accepted({"idTagInfo": {"status": "Accepted"}})
    assert not id_tag_accepted({"idTagInfo": {"status": "Invalid"}})
    assert not id_tag_accepted(None)
