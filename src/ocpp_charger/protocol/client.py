"""OCPP-J 1.6 WebSocket client.

* One outstanding CALL at a time (OCPP-J 1.6 §4.1.1) — every CALL goes
  through :meth:`call`, which is serialised by a lock and waits for the
  matching CALLRESULT / CALLERROR.
* Server CALLs are dispatched to a handler; the handler may return a dict,
  a :class:`CallResult` (with a follow-up coroutine) or raise
  :class:`OcppCallError`. Unknown/failed handlers produce a CALLERROR.
* :meth:`recv_loop` returns when the socket closes so the caller can
  reconnect.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Union

try:  # websockets >= 13: new asyncio client
    from websockets.asyncio.client import connect as _ws_connect
    _HEADERS_KWARG = "additional_headers"
except ImportError:  # websockets 10–12: legacy client
    try:
        from websockets import connect as _ws_connect  # type: ignore
        _HEADERS_KWARG = "extra_headers"
    except ImportError:  # websockets not installed (unit tests)
        _ws_connect = None
        _HEADERS_KWARG = "additional_headers"

from .messages import CallErrorCode, CallResult, MessageType, OcppCallError

HandlerResult = Union[dict, CallResult, None]
ServerHandler = Callable[[str, dict], Awaitable[HandlerResult]]


class NotConnectedError(ConnectionError):
    """Raised when a CALL is attempted while the WebSocket is down."""


class OcppClient:
    def __init__(
        self,
        csms_url: str,
        subprotocol: str,
        auth_user: str,
        auth_password: str,
        ping_interval: Optional[int],
        logger: Callable[[str, str], None],
        call_timeout: float = 30.0,
    ):
        self._csms_url = csms_url
        self._subprotocol = subprotocol
        self._auth_user = auth_user
        self._auth_password = auth_password
        self._ping_interval = ping_interval
        self._log = logger
        self.call_timeout = call_timeout

        self._ws = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._call_lock: Optional[asyncio.Lock] = None
        self._on_server_request: Optional[ServerHandler] = None
        self._connected: bool = False
        self._hb_task: Optional[asyncio.Task] = None
        self._bg_tasks: Set[asyncio.Task] = set()

    # ─── State ───────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_server_request_handler(self, handler: ServerHandler) -> None:
        self._on_server_request = handler

    def _lock(self) -> asyncio.Lock:
        if self._call_lock is None:
            self._call_lock = asyncio.Lock()
        return self._call_lock

    # ─── Connection ──────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if _ws_connect is None:
            raise RuntimeError("websockets paketi kurulu değil (pip install websockets)")

        kwargs: Dict[str, Any] = {
            "subprotocols": [self._subprotocol],
            "ping_interval": self._ping_interval,
            "open_timeout": 15,
        }
        if self._auth_user or self._auth_password:
            creds = base64.b64encode(f"{self._auth_user}:{self._auth_password}".encode()).decode()
            kwargs[_HEADERS_KWARG] = {"Authorization": f"Basic {creds}"}

        self._ws = await _ws_connect(self._csms_url, **kwargs)
        self._connected = True
        self._log("INFO", f"Bağlandı ✓  ({self._csms_url})")

    async def connect_with_retry(self, max_retries: int = 0) -> None:
        """Exponential backoff: 1, 2, 4, 8, 16, 30, 30, ... seconds.
        ``max_retries=0`` retries forever."""
        backoffs = [1, 2, 4, 8, 16, 30]
        retries = 0
        while True:
            try:
                await self.connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log("ERR", f"Bağlantı başarısız: {e}")
                if max_retries > 0 and retries >= max_retries:
                    raise
                wait_time = backoffs[min(retries, len(backoffs) - 1)]
                self._log("INFO", f"Yeniden bağlanılıyor ({wait_time}s sonra)...")
                await asyncio.sleep(wait_time)
                retries += 1

    async def disconnect(self) -> None:
        self._connected = False
        self.stop_heartbeat()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._fail_pending("Connection closed")

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(NotConnectedError(reason))
        self._pending.clear()

    # ─── Outgoing CALL ───────────────────────────────────────────────────────
    async def call(self, action: str, payload: dict, timeout: Optional[float] = None) -> dict:
        """Send a CALL and wait for its CALLRESULT payload.

        Raises NotConnectedError, TimeoutError, or OcppCallError (on CALLERROR).
        """
        action = str(getattr(action, "value", action))
        timeout = self.call_timeout if timeout is None else timeout

        async with self._lock():
            if not self._connected or self._ws is None:
                raise NotConnectedError(f"{action} gönderilemedi: CSMS bağlantısı yok")

            mid = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            self._pending[mid] = future
            try:
                try:
                    await self._ws.send(json.dumps([MessageType.CALL, mid, action, payload]))
                except Exception as e:  # websockets.ConnectionClosed etc.
                    self._connected = False
                    raise NotConnectedError(f"{action} gönderilemedi: {e}") from e
                self._log("SEND", f"[{action}] {payload}")
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"{action} yanıtı {timeout:.0f}s içinde gelmedi")
            finally:
                self._pending.pop(mid, None)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # mark as retrieved

    # ─── Incoming ────────────────────────────────────────────────────────────
    async def recv_loop(self) -> None:
        """Read frames until the socket closes, then return."""
        try:
            async for raw in self._ws:
                await self._handle_raw_message(raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log("WARN", f"Bağlantı kapandı: {e}")
        else:
            self._log("WARN", "Bağlantı sunucu tarafından kapatıldı")
        finally:
            self._connected = False
            self.stop_heartbeat()
            self._fail_pending("Connection closed")

    async def _send_frame(self, frame: list) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(frame))

    async def respond(self, message_id: str, payload: dict) -> None:
        await self._send_frame([MessageType.CALLRESULT, message_id, payload])
        self._log("SEND", f"[Response/{message_id}] {payload}")

    async def respond_error(self, message_id: str, code: str, description: str = "", details: Optional[dict] = None) -> None:
        await self._send_frame([MessageType.CALLERROR, message_id, code, description, details or {}])
        self._log("SEND", f"[CallError/{message_id}] {code} — {description}")

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self._log("ERR", f"Arka plan görevi hatası: {t.exception()}")

        task.add_done_callback(_done)

    async def _handle_raw_message(self, raw: Union[str, bytes]) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._log("ERR", f"JSON parse hatası: {raw!r}")
            return

        if not isinstance(msg, list) or len(msg) < 3 or not isinstance(msg[0], int):
            self._log("ERR", f"Geçersiz OCPP çerçevesi: {msg!r}")
            return

        msg_type, mid = msg[0], str(msg[1])

        if msg_type == MessageType.CALLRESULT:
            payload = msg[2]
            self._log("RECV", f"[Response/{mid}] {payload}")
            future = self._pending.get(mid)
            if future and not future.done():
                future.set_result(payload if isinstance(payload, dict) else {})

        elif msg_type == MessageType.CALLERROR:
            code = msg[2]
            desc = msg[3] if len(msg) > 3 else ""
            details = msg[4] if len(msg) > 4 else {}
            self._log("ERR", f"CALLERROR [{mid}]: {code} — {desc}")
            future = self._pending.get(mid)
            if future and not future.done():
                future.set_exception(OcppCallError(code, desc, details))

        elif msg_type == MessageType.CALL:
            if len(msg) < 4 or not isinstance(msg[2], str) or not isinstance(msg[3], dict):
                await self.respond_error(mid, CallErrorCode.FORMATION_VIOLATION, "CALL must be [2, id, action, {payload}]")
                return
            await self._dispatch_call(mid, msg[2], msg[3])

        else:
            self._log("ERR", f"Bilinmeyen mesaj tipi {msg_type}: {msg!r}")

    async def _dispatch_call(self, mid: str, action: str, payload: dict) -> None:
        self._log("RECV", f"[{action}] ← Sunucu: {payload}")
        if self._on_server_request is None:
            await self.respond_error(mid, CallErrorCode.NOT_IMPLEMENTED, f"{action} not implemented")
            return
        try:
            result = await self._on_server_request(action, payload)
        except OcppCallError as e:
            await self.respond_error(mid, e.code, e.description, e.details)
            return
        except Exception as e:
            self._log("ERR", f"Sunucu isteği işlenemedi ({action}): {e}")
            await self.respond_error(mid, CallErrorCode.INTERNAL_ERROR, str(e))
            return

        if isinstance(result, CallResult):
            await self.respond(mid, result.payload)
            if result.followup is not None:
                self._spawn(result.followup())
        else:
            await self.respond(mid, result if isinstance(result, dict) else {})

    # ─── Heartbeat ───────────────────────────────────────────────────────────
    async def start_heartbeat(self, interval: int) -> None:
        self.stop_heartbeat()
        self._hb_task = asyncio.ensure_future(self._heartbeat_loop(interval))
        self._log("INFO", f"Heartbeat başladı — her {interval}s")

    def stop_heartbeat(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
            self._hb_task = None

    async def _heartbeat_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.call("Heartbeat", {})
            except asyncio.CancelledError:
                raise
            except NotConnectedError:
                return
            except Exception as e:
                # No answer to a heartbeat → the link is dead even if TCP
                # hasn't noticed. Close it so the run loop reconnects.
                self._log("WARN", f"Heartbeat başarısız ({e}); bağlantı yenileniyor")
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                return
