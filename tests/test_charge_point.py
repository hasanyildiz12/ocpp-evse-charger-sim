import time
from datetime import datetime

import pytest

from ocpp_charger.protocol.messages import CallResult, OcppCallError


async def server(cp, action, payload):
    """Run a server CALL like the client does: reply, then follow-up."""
    res = await cp.handle_server_request(action, payload)
    if isinstance(res, CallResult):
        if res.followup:
            await res.followup()
        return res.payload
    return res


def test_initial_state(make_cp):
    cp, _ = make_cp()
    assert cp.transaction_id is None
    assert cp.pending_auth is None
    assert cp.charging_active is False
    assert cp.current_status == "NOT CONNECTED"


def test_iso_now_format(make_cp):
    cp, _ = make_cp()
    s = cp.iso_now()
    assert s.endswith("Z")
    datetime.fromisoformat(s[:-1])


# ─── Card-per-session flow ───────────────────────────────────────────────────

async def test_card_then_plug_full_session(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("A", debounce=False)
    assert client.last("StatusNotification")["status"] == "Available"

    await cp.on_card("97350e07")
    assert client.last("Authorize") == {"idTag": "97350E07"}
    assert cp.pending_auth is not None
    assert client.last("StatusNotification")["status"] == "Preparing"
    assert cp.pwm.duty == 1.0  # no PWM before the EV is plugged and tx is open

    await cp.process_pilot("B", debounce=False)
    start = client.last("StartTransaction")
    assert start["idTag"] == "97350E07" and start["connectorId"] == 1
    assert cp.transaction_id == 42
    assert cp.pwm.offering and abs(cp.pwm.duty - 16 / 60) < 1e-3
    assert client.last("StatusNotification")["status"] == "SuspendedEV"

    await cp.process_pilot("C", debounce=False)
    assert client.last("StatusNotification")["status"] == "Charging"

    await cp.on_card("97350E07")  # same card stops
    stop = client.last("StopTransaction")
    assert stop["reason"] == "Local" and stop["idTag"] == "97350E07" and stop["transactionId"] == 42
    assert cp.transaction_id is None
    assert cp.pwm.duty == 1.0
    assert client.last("StatusNotification")["status"] == "Finishing"

    await cp.process_pilot("A", debounce=False)
    assert client.last("StatusNotification")["status"] == "Available"


async def test_start_transaction_sent_before_pwm(make_cp):
    cp, client = make_cp()
    duty_at_start = []
    client.responses["StartTransaction"] = lambda p: (duty_at_start.append(cp.pwm.duty),
                                                      {"transactionId": 1, "idTagInfo": {"status": "Accepted"}})[1]
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    assert duty_at_start == [1.0]
    assert cp.pwm.offering


async def test_plug_then_card(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    assert client.last("StatusNotification")["status"] == "Preparing"
    assert "StartTransaction" not in client.actions()
    await cp.on_card("AABBCCDD")
    assert cp.transaction_id == 42


async def test_nfc_id_tag_map(make_cp):
    cp, client = make_cp(nfc_id_tag_map={"97350e07": "hasanyildizidtag"})
    await cp.on_card("97350E07")
    assert client.last("Authorize") == {"idTag": "hasanyildizidtag"}


async def test_rejected_card_does_not_start(make_cp):
    cp, client = make_cp()
    client.responses["Authorize"] = {"idTagInfo": {"status": "Invalid"}}
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("DEADBEEF")
    assert cp.pending_auth is None
    assert "StartTransaction" not in client.actions()
    assert cp.pwm.duty == 1.0


async def test_other_card_cannot_stop(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    await cp.on_card("BB")
    assert cp.transaction_id == 42
    assert "StopTransaction" not in client.actions()


async def test_unplug_stops_with_ev_disconnected(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    await cp.process_pilot("C", debounce=False)
    await cp.process_pilot("A", debounce=False)
    assert client.last("StopTransaction")["reason"] == "EVDisconnected"
    assert client.last("StatusNotification")["status"] == "Available"


async def test_pilot_fault_stops_and_faults(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    await cp.process_pilot("E", debounce=False)
    assert client.last("StopTransaction")["reason"] == "Other"
    st = client.last("StatusNotification")
    assert st["status"] == "Faulted" and st["errorCode"] == "EVCommunicationError"
    assert cp.pwm.duty == 1.0


async def test_start_transaction_invalid_id_tag_stops(make_cp):
    cp, client = make_cp()
    client.responses["StartTransaction"] = {"transactionId": 7, "idTagInfo": {"status": "Blocked"}}
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    assert client.last("StopTransaction")["reason"] == "DeAuthorized"
    assert cp.transaction_id is None


async def test_auth_expires_after_connection_timeout(make_cp):
    cp, client = make_cp(connection_timeout=1)
    await cp.on_card("AA")
    cp.pending_auth.expires_at = time.monotonic() - 1
    await cp.evaluate()
    assert cp.pending_auth is None
    assert client.last("StatusNotification")["status"] == "Available"


async def test_card_ignored_when_offline(make_cp):
    cp, client = make_cp()
    client.is_connected = False
    await cp.on_card("AA")
    assert "Authorize" not in client.actions()
    assert cp.pending_auth is None


async def test_stop_transaction_queued_when_offline(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    client.is_connected = False
    await cp.process_pilot("A", debounce=False)
    assert cp._offline_queue and cp._offline_queue[0][0] == "StopTransaction"
    client.is_connected = True
    await cp._flush_offline_queue()
    assert not cp._offline_queue
    assert client.last("StopTransaction")["reason"] == "EVDisconnected"


async def test_meter_values_payload(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    await cp.meter_values()
    mv = client.last("MeterValues")
    assert mv["transactionId"] == 42
    measurands = {s["measurand"] for s in mv["meterValue"][0]["sampledValue"]}
    assert "Energy.Active.Import.Register" in measurands
    assert "Voltage" not in measurands  # no fake constant values


async def test_cable_limit_reduces_offered_current(make_cp):
    cp, _ = make_cp(max_current_a=32)
    cp.cable_max_current = 20.0
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    assert abs(cp.pwm.duty - 20 / 60) < 1e-3


# ─── Server-initiated requests ───────────────────────────────────────────────

async def test_remote_start_and_stop(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    assert await server(cp, "RemoteStartTransaction", {"idTag": "REMOTE1", "connectorId": 1}) == {"status": "Accepted"}
    assert client.last("StartTransaction")["idTag"] == "REMOTE1"
    assert "Authorize" not in client.actions()  # AuthorizeRemoteTxRequests=false

    assert await server(cp, "RemoteStopTransaction", {"transactionId": 999}) == {"status": "Rejected"}
    assert await server(cp, "RemoteStopTransaction", {"transactionId": 42}) == {"status": "Accepted"}
    assert client.last("StopTransaction")["reason"] == "Remote"
    assert client.last("StopTransaction")["idTag"] == "REMOTE1"


async def test_remote_start_rejected_when_busy(make_cp):
    cp, _ = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    assert await server(cp, "RemoteStartTransaction", {"idTag": "X"}) == {"status": "Rejected"}


async def test_remote_start_requires_id_tag(make_cp):
    cp, _ = make_cp()
    with pytest.raises(OcppCallError):
        await server(cp, "RemoteStartTransaction", {})


async def test_unknown_action_not_implemented(make_cp):
    cp, _ = make_cp()
    with pytest.raises(OcppCallError) as e:
        await server(cp, "UpdateFirmware", {})
    assert e.value.code == "NotImplemented"


async def test_unlock_and_simple_actions(make_cp):
    cp, _ = make_cp()
    assert await server(cp, "UnlockConnector", {"connectorId": 1}) == {"status": "NotSupported"}
    assert await server(cp, "ClearCache", {}) == {"status": "Accepted"}
    assert await server(cp, "DataTransfer", {"vendorId": "x"}) == {"status": "UnknownVendorId"}


async def test_get_and_change_configuration(make_cp):
    cp, client = make_cp()
    res = await server(cp, "GetConfiguration", {"key": ["HeartbeatInterval", "Foo"]})
    assert res["configurationKey"] == [{"key": "HeartbeatInterval", "readonly": False, "value": "30"}]
    assert res["unknownKey"] == ["Foo"]

    assert await server(cp, "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "60"}) == {"status": "Accepted"}
    assert cp.config.heartbeat_interval == 60 and client.heartbeat_interval == 60
    assert await server(cp, "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "abc"}) == {"status": "Rejected"}
    assert await server(cp, "ChangeConfiguration", {"key": "NumberOfConnectors", "value": "2"}) == {"status": "Rejected"}
    assert await server(cp, "ChangeConfiguration", {"key": "Nope", "value": "1"}) == {"status": "NotSupported"}

    all_keys = await server(cp, "GetConfiguration", {})
    assert "unknownKey" not in all_keys and len(all_keys["configurationKey"]) > 5


async def test_change_availability(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("A", debounce=False)
    assert await server(cp, "ChangeAvailability", {"connectorId": 1, "type": "Inoperative"}) == {"status": "Accepted"}
    assert client.last("StatusNotification")["status"] == "Unavailable"
    await cp.on_card("AA")
    assert "Authorize" not in client.actions()
    assert await server(cp, "ChangeAvailability", {"connectorId": 0, "type": "Operative"}) == {"status": "Accepted"}
    assert client.last("StatusNotification")["status"] == "Available"


async def test_change_availability_scheduled_during_transaction(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    assert await server(cp, "ChangeAvailability", {"connectorId": 1, "type": "Inoperative"}) == {"status": "Scheduled"}
    await cp.on_card("AA")  # stop
    await cp.process_pilot("A", debounce=False)
    assert cp.availability == "Inoperative"
    assert client.last("StatusNotification")["status"] == "Unavailable"


async def test_trigger_message(make_cp):
    cp, client = make_cp()
    n = client.actions().count("StatusNotification")
    assert await server(cp, "TriggerMessage", {"requestedMessage": "StatusNotification"}) == {"status": "Accepted"}
    assert client.actions().count("StatusNotification") == n + 1
    assert await server(cp, "TriggerMessage", {"requestedMessage": "FirmwareStatusNotification"}) == {"status": "NotImplemented"}


async def test_reset_stops_transaction_and_requests_reboot(make_cp):
    cp, client = make_cp()
    await cp.process_pilot("B", debounce=False)
    await cp.on_card("AA")
    assert await server(cp, "Reset", {"type": "Hard"}) == {"status": "Accepted"}
    assert client.last("StopTransaction")["reason"] == "HardReset"
    assert cp._reset_requested and client.is_connected is False


async def test_boot_retries_until_accepted(make_cp, monkeypatch):
    cp, client = make_cp()
    seq = iter([{"status": "Pending", "interval": 1}, {"status": "Accepted", "interval": 45}])
    client.responses["BootNotification"] = lambda p: next(seq)

    async def no_sleep(_):
        return None
    monkeypatch.setattr("ocpp_charger.charge_point.asyncio.sleep", no_sleep)
    await cp._boot()
    assert cp.registered and cp.config.heartbeat_interval == 45
    assert client.actions().count("BootNotification") == 2


# ─── Pilot classification (ADC → state) ─────────────────────────────────────

def test_classify_adc_standby_levels(make_cp):
    from ocpp_charger.hardware.cp_signal import adc_from_cp_voltage
    cp, _ = make_cp()
    c = cp.config
    for v, state in ((12, "A"), (9, "B"), (6, "C"), (3, "D"), (0, "E")):
        adc = adc_from_cp_voltage(v, c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup, c.adc_vref)
        assert cp.classify_adc(adc)[0] == state


def test_classify_adc_with_pwm_and_missing_diode(make_cp):
    from ocpp_charger.hardware.cp_signal import adc_from_cp_voltage
    cp, _ = make_cp()
    c = cp.config
    cp.pwm.offer_current(16)
    d = cp.pwm.duty
    avg_c = d * 6 + (1 - d) * -12
    assert cp.classify_adc(adc_from_cp_voltage(avg_c, c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup, c.adc_vref))[0] == "C"
    # short to PE: pilot stuck at 0 V → impossible high level → fault
    assert cp.classify_adc(adc_from_cp_voltage(0.0, c.cp_r_top, c.cp_r_bottom, c.cp_r_pullup, c.adc_vref))[0] == "E"


async def test_debounce_requires_two_samples(make_cp):
    cp, _ = make_cp()
    await cp.process_pilot("B")
    assert cp.pilot_state is None
    await cp.process_pilot("B")
    assert cp.pilot_state == "B"
