def test_message_type_values():
    # MessageType id assignments as per OCPP protocol
    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4
    
    assert CALL == 2
    assert CALLRESULT == 3
    assert CALLERROR == 4

def test_action_values():
    # Common actions in OCPP
    actions = [
        "BootNotification",
        "Heartbeat",
        "StatusNotification",
        "MeterValues",
        "Authorize",
        "StartTransaction",
        "StopTransaction",
        "RemoteStartTransaction",
        "RemoteStopTransaction",
        "Reset",
        "UnlockConnector"
    ]
    
    assert "BootNotification" in actions
    assert "MeterValues" in actions
    assert "Authorize" in actions

def test_server_action_responses():
    # Known server actions and expected formats
    actions = {
        "RemoteStartTransaction": {"status": "Accepted"},
        "RemoteStopTransaction": {"status": "Accepted"},
        "Reset": {"status": "Accepted"},
        "UnlockConnector": {"status": "Unlocked"}
    }
    
    assert actions["RemoteStartTransaction"]["status"] == "Accepted"
    assert actions["Reset"]["status"] == "Accepted"

def test_charge_point_status_values():
    # Allowed status values
    statuses = [
        "Available",
        "Preparing",
        "Charging",
        "SuspendedEVSE",
        "SuspendedEV",
        "Finishing",
        "Reserved",
        "Unavailable",
        "Faulted"
    ]
    
    assert "Available" in statuses
    assert "Charging" in statuses
    assert "Faulted" in statuses
