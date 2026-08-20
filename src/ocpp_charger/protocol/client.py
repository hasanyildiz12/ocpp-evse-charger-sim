import asyncio
import base64
import json
from typing import Any, Callable, Coroutine, Dict, Optional
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    pass

class OcppClient:
    """OCPP 1.6J WebSocket client with reconnection and request/response correlation."""
    
    def __init__(
        self,
        csms_url: str,
        subprotocol: str,
        auth_user: str,
        auth_password: str,
        ping_interval: Optional[int],
        logger: Callable[[str, str], None],
    ):
        self._csms_url = csms_url
        self._subprotocol = subprotocol
        self._auth_user = auth_user
        self._auth_password = auth_password
        self._ping_interval = ping_interval
        self._log = logger
        
        self._ws = None
        self._msg_id: int = 1
        self._pending: Dict[str, asyncio.Future] = {}  # msg_id -> Future for response
        self._on_server_request: Optional[Callable] = None  # callback for server CALL
        self._connected: bool = False
        self._hb_task: Optional[asyncio.Task] = None
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    @property 
    def websocket(self):
        """Return raw ws for compatibility"""
        return self._ws
    
    def _next_id(self) -> str:
        """Generate next message ID (instance-level counter)."""
        mid = str(self._msg_id)
        self._msg_id += 1
        return mid
    
    def set_server_request_handler(self, handler: Callable) -> None:
        """Set callback for server-initiated CALL messages.
        handler(action: str, payload: dict) -> dict (response payload)
        """
        self._on_server_request = handler
    
    async def connect(self) -> None:
        """Connect to CSMS with Basic Auth. Raises on failure."""
        _credentials = base64.b64encode(
            f"{self._auth_user}:{self._auth_password}".encode()
        ).decode()
        
        self._ws = await websockets.connect(
            self._csms_url,
            subprotocols=[self._subprotocol],
            ping_interval=self._ping_interval,
            extra_headers={"Authorization": f"Basic {_credentials}"},
        )
        self._connected = True
        self._log("INFO", f"Bağlandı ✓  ({self._csms_url})")
    
    async def connect_with_retry(self, max_retries: int = 0) -> None:
        """Connect with exponential backoff retry.
        max_retries=0 means infinite retries.
        Backoff: 1s, 2s, 4s, 8s, 16s, 30s (capped at 30s)
        """
        backoffs = [1, 2, 4, 8, 16, 30]
        retries = 0
        while True:
            try:
                await self.connect()
                return
            except Exception as e:
                self._log("ERR", f"Bağlantı başarısız: {e}")
                if max_retries > 0 and retries >= max_retries:
                    raise
                wait_time = backoffs[min(retries, len(backoffs)-1)]
                self._log("INFO", f"Yeniden bağlanılıyor ({wait_time}s sonra)...")
                await asyncio.sleep(wait_time)
                retries += 1
    
    async def disconnect(self) -> None:
        """Gracefully close WebSocket connection."""
        self._connected = False
        if self._hb_task:
            self._hb_task.cancel()
        if self._ws:
            await self._ws.close()
    
    async def call(self, action: str, payload: dict, timeout: float = 30.0) -> dict:
        """Send OCPP CALL and wait for CALLRESULT with correlation."""
        mid = self._next_id()
        msg = json.dumps([2, mid, action, payload])
        
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[mid] = future
        
        await self._ws.send(msg)
        self._log("SEND", f"[{action}] {payload}")
        
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise TimeoutError(f"Call {action} (mid={mid}) timed out after {timeout}s")
    
    async def send(self, action: str, payload: dict) -> str:
        """Send OCPP CALL without waiting for response (fire-and-forget).
        For backward compatibility with original code flow.
        Returns the message ID.
        """
        mid = self._next_id()
        msg = json.dumps([2, mid, action, payload])
        await self._ws.send(msg)
        self._log("SEND", f"[{action}] {payload}")
        return mid
    
    async def respond(self, message_id: str, payload: dict) -> None:
        """Send CALLRESULT response to a server-initiated request."""
        msg = json.dumps([3, message_id, payload])  # MessageType.CALLRESULT
        await self._ws.send(msg)
        self._log("SEND", f"[Response/{message_id}] {payload}")
    
    async def recv_loop(self) -> None:
        """Main receive loop. Routes messages:
        - CALLRESULT (3): resolve pending Future
        - CALL (2): dispatch to server_request_handler
        - CALLERROR (4): log error, reject pending Future
        """
        try:
            async for raw in self._ws:
                await self._handle_raw_message(raw)
        except Exception as e:
            self._connected = False
            self._log("WARN", f"Bağlantı kapandı: {e}")
            # Cancel all pending futures
            for mid, future in self._pending.items():
                if not future.done():
                    future.set_exception(Exception("Connection closed"))
            self._pending.clear()
    
    async def _handle_raw_message(self, raw: str) -> None:
        """Parse and route a single raw OCPP message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._log("ERR", f"JSON parse hatası: {raw}")
            return
        
        msg_type = msg[0]
        mid = str(msg[1])
        
        if msg_type == 3:  # CALLRESULT
            payload = msg[2]
            self._log("RECV", f"[Response/{mid}] {payload}")
            future = self._pending.pop(mid, None)
            if future and not future.done():
                future.set_result(payload)
        
        elif msg_type == 2:  # CALL from server
            action = msg[2]
            payload = msg[3] if len(msg) > 3 else {}
            self._log("RECV", f"[{action}] ← Sunucu: {payload}")
            if self._on_server_request:
                try:
                    response = await self._on_server_request(action, payload)
                    if response is not None:
                        await self.respond(mid, response)
                except Exception as e:
                    self._log("ERR", f"Server request handler error: {e}")
        
        elif msg_type == 4:  # CALLERROR
            self._log("ERR", f"CALLERROR [{mid}]: {msg[2]} — {msg[3]}")
            future = self._pending.pop(mid, None)
            if future and not future.done():
                future.set_exception(Exception(f"CALLERROR: {msg[2]} — {msg[3]}"))
    
    async def start_heartbeat(self, interval: int) -> None:
        """Start/restart heartbeat loop."""
        if self._hb_task:
            self._hb_task.cancel()
        self._hb_task = asyncio.create_task(self._heartbeat_loop(interval))
        self._log("INFO", f"Heartbeat başladı — her {interval}s")
    
    async def _heartbeat_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.send("Heartbeat", {})
            except Exception:
                break
