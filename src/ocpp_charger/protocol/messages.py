"""OCPP 1.6 message constants and types."""

from enum import IntEnum, Enum

class MessageType(IntEnum):
    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4

class Action(str, Enum):
    BOOT_NOTIFICATION = "BootNotification"
    HEARTBEAT = "Heartbeat"
    STATUS_NOTIFICATION = "StatusNotification"
    AUTHORIZE = "Authorize"
    START_TRANSACTION = "StartTransaction"
    STOP_TRANSACTION = "StopTransaction"
    METER_VALUES = "MeterValues"
    # Server-initiated
    GET_CONFIGURATION = "GetConfiguration"
    CHANGE_CONFIGURATION = "ChangeConfiguration"
    RESET = "Reset"
    REMOTE_START_TRANSACTION = "RemoteStartTransaction"
    REMOTE_STOP_TRANSACTION = "RemoteStopTransaction"
    TRIGGER_MESSAGE = "TriggerMessage"
    UNLOCK_CONNECTOR = "UnlockConnector"
    CLEAR_CACHE = "ClearCache"

# Default responses for server-initiated actions
SERVER_ACTION_RESPONSES: dict[str, dict] = {
    Action.GET_CONFIGURATION: {"configurationKey": [], "unknownKey": []},
    Action.CHANGE_CONFIGURATION: {"status": "Accepted"},
    Action.RESET: {"status": "Accepted"},
    Action.REMOTE_START_TRANSACTION: {"status": "Accepted"},
    Action.REMOTE_STOP_TRANSACTION: {"status": "Accepted"},
    Action.TRIGGER_MESSAGE: {"status": "Accepted"},
    Action.UNLOCK_CONNECTOR: {"status": "Unlocked"},
    Action.CLEAR_CACHE: {"status": "Accepted"},
}

class ChargePointStatus(str, Enum):
    AVAILABLE = "Available"
    PREPARING = "Preparing"
    CHARGING = "Charging"
    SUSPENDED_EV = "SuspendedEV"
    FAULTED = "Faulted"
    UNAVAILABLE = "Unavailable"
