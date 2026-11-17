# OCPP 1.6J — Message Flow Reference

## Connection lifecycle

```
Charge Point                         CSMS
     │──── WebSocket CONNECT (ocpp1.6, Basic Auth) ──▶│
     │──── BootNotification ─────────────▶│   repeated every `interval` while Pending/Rejected
     │◀─── BootNotification.conf ─────────│   (status=Accepted, heartbeat interval)
     │──── StatusNotification ───────────▶│   current connector status
     │──── Heartbeat ────────────────────▶│   every HeartbeatInterval; no answer → reconnect
```

On a dropped connection the charge point reconnects with backoff
(1→2→4→8→16→30 s). BootNotification is **not** repeated after a plain
reconnect (only after start-up or Reset); the current StatusNotification is
re-sent and queued StopTransaction messages are flushed.

Only one CALL is outstanding at a time (OCPP-J §4.1.1).

## Charging session (card per session)

```
Charge Point                         CSMS
  card tap
     │──── Authorize(idTag=UID) ────────▶│
     │◀─── idTagInfo.status=Accepted ───│
     │──── StatusNotification(Preparing)▶│
  EV plugged (pilot B)
     │──── StartTransaction ────────────▶│
     │◀─── transactionId, idTagInfo ────│   not Accepted → StopTransaction(DeAuthorized)
  PWM on → EV closes S2 (pilot C)
     │──── StatusNotification(Charging) ▶│
     │──── MeterValues (every MeterValueSampleInterval) ▶│
  same card / RemoteStop / unplug / pilot fault
  PWM off
     │──── StopTransaction(reason) ─────▶│   Local · Remote · EVDisconnected · Other · *Reset
     │──── StatusNotification(Finishing)▶│   (Available once unplugged)
```

Plug-first works too: plug in (Preparing), then tap the card.
If the card is tapped but no EV is plugged within `ConnectionTimeOut`, the
authorization expires.

## Connector status mapping

| Pilot | Transaction | Status |
|---|---|---|
| A (12 V) | — | Available (Preparing if a card was authorized) |
| B (9 V) | none | Preparing (Finishing after a session) |
| B (9 V) | open | SuspendedEV |
| C (6 V) | open | Charging |
| D (3 V) | any | Faulted / OtherError (ventilation not supported) |
| E (≤0 V / no diode) | any | Faulted / EVCommunicationError |
| — | ChangeAvailability Inoperative | Unavailable |

## Server-initiated requests

| Action | Behaviour |
|---|---|
| `RemoteStartTransaction` | Accepted if idle & operative → behaves like a card tap (Authorize first if AuthorizeRemoteTxRequests=true). Otherwise Rejected. |
| `RemoteStopTransaction` | Accepted only for the active transactionId; stops with reason Remote. |
| `Reset` | Accepted; stops any transaction (HardReset/SoftReset), reconnects and re-sends BootNotification. |
| `ChangeAvailability` | Accepted, or Scheduled while a transaction is running. |
| `GetConfiguration` / `ChangeConfiguration` | Real key store (HeartbeatInterval, MeterValueSampleInterval, ConnectionTimeOut, …). Unknown → NotSupported, read-only → Rejected. |
| `TriggerMessage` | BootNotification, Heartbeat, StatusNotification, MeterValues; others → NotImplemented. |
| `UnlockConnector` | NotSupported (no lock actuator). |
| `ClearCache` | Accepted (no local cache). |
| `DataTransfer` | UnknownVendorId. |
| anything else | CALLERROR `NotImplemented`. |

## Frame format

```
CALL        →  [2, "<messageId>", "<action>", {payload}]
CALLRESULT  →  [3, "<messageId>", {payload}]
CALLERROR   →  [4, "<messageId>", "<errorCode>", "<errorDescription>", {details}]
```
