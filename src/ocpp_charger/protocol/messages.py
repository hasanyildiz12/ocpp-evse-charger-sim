"""OCPP 1.6 message constants and types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Awaitable, Callable, Dict, Optional


class MessageType(IntEnum):
    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4


class Action(str, Enum):
    # Charge Point -> CSMS
    BOOT_NOTIFICATION = "BootNotification"
    HEARTBEAT = "Heartbeat"
    STATUS_NOTIFICATION = "StatusNotification"
    AUTHORIZE = "Authorize"
    START_TRANSACTION = "StartTransaction"
    STOP_TRANSACTION = "StopTransaction"
    METER_VALUES = "MeterValues"
    # CSMS -> Charge Point
    CHANGE_AVAILABILITY = "ChangeAvailability"
    GET_CONFIGURATION = "GetConfiguration"
    CHANGE_CONFIGURATION = "ChangeConfiguration"
    RESET = "Reset"
    REMOTE_START_TRANSACTION = "RemoteStartTransaction"
    REMOTE_STOP_TRANSACTION = "RemoteStopTransaction"
    TRIGGER_MESSAGE = "TriggerMessage"
    UNLOCK_CONNECTOR = "UnlockConnector"
    CLEAR_CACHE = "ClearCache"
    DATA_TRANSFER = "DataTransfer"


class ChargePointStatus(str, Enum):
    AVAILABLE = "Available"
    PREPARING = "Preparing"
    CHARGING = "Charging"
    SUSPENDED_EVSE = "SuspendedEVSE"
    SUSPENDED_EV = "SuspendedEV"
    FINISHING = "Finishing"
    RESERVED = "Reserved"
    UNAVAILABLE = "Unavailable"
    FAULTED = "Faulted"


class ErrorCode(str, Enum):
    """OCPP 1.6 ChargePointErrorCode (subset used here)."""
    NO_ERROR = "NoError"
    EV_COMMUNICATION_ERROR = "EVCommunicationError"
    POWER_METER_FAILURE = "PowerMeterFailure"
    READER_FAILURE = "ReaderFailure"
    OTHER_ERROR = "OtherError"


class StopReason(str, Enum):
    LOCAL = "Local"
    REMOTE = "Remote"
    EV_DISCONNECTED = "EVDisconnected"
    DE_AUTHORIZED = "DeAuthorized"
    HARD_RESET = "HardReset"
    SOFT_RESET = "SoftReset"
    EMERGENCY_STOP = "EmergencyStop"
    OTHER = "Other"


class CallErrorCode(str, Enum):
    """OCPP-J 1.6 CALLERROR codes."""
    NOT_IMPLEMENTED = "NotImplemented"
    NOT_SUPPORTED = "NotSupported"
    INTERNAL_ERROR = "InternalError"
    PROTOCOL_ERROR = "ProtocolError"
    PROPERTY_CONSTRAINT_VIOLATION = "PropertyConstraintViolation"
    FORMATION_VIOLATION = "FormationViolation"
    GENERIC_ERROR = "GenericError"


class OcppCallError(Exception):
    """Raised by a server-request handler to answer with a CALLERROR frame."""

    def __init__(self, code: str, description: str = "", details: Optional[dict] = None):
        super().__init__(f"{code}: {description}")
        self.code = str(code.value if isinstance(code, Enum) else code)
        self.description = description
        self.details = details or {}


@dataclass
class CallResult:
    """Response to a server-initiated CALL.

    ``followup`` is started as a background task *after* the CALLRESULT has
    been written to the socket. Use it for work that must happen after the
    reply (e.g. TriggerMessage, RemoteStart) and for anything that itself
    sends a CALL — awaiting a CALL inside the handler would deadlock the
    receive loop.
    """
    payload: Dict[str, Any]
    followup: Optional[Callable[[], Awaitable[None]]] = None


def id_tag_accepted(response: Optional[dict]) -> bool:
    """True if an Authorize/StartTransaction response carries idTagInfo.status == Accepted."""
    if not isinstance(response, dict):
        return False
    info = response.get("idTagInfo") or {}
    return info.get("status") == "Accepted"


def id_tag_status(response: Optional[dict]) -> str:
    if not isinstance(response, dict):
        return "NoResponse"
    return (response.get("idTagInfo") or {}).get("status", "Missing")
