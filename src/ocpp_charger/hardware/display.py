import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Callable

@dataclass
class TouchEvent:
    page_id: int
    component_id: int
    event_type: int

class IDisplayDriver(ABC):
    @abstractmethod
    def open(self) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def send_command(self, cmd: str) -> None:
        pass

    @abstractmethod
    def set_status(self, status: str) -> None:
        pass

    @abstractmethod
    def set_time(self) -> None:
        pass

    @abstractmethod
    def set_user_id(self, id_tag: str) -> None:
        pass

    @abstractmethod
    def update_charging_info(self, energy_kwh: float, power_kw: float, elapsed_seconds: float, cost: float, currency: str = "$") -> None:
        pass

    @abstractmethod
    def show_page(self, page: str) -> None:
        pass

    @abstractmethod
    async def read_touch_events(self) -> AsyncIterator[TouchEvent]:
        pass

class RGB565:
    RED = 63488
    CYAN = 2047
    GREEN = 11939
    YELLOW = 65504

TOUCH_COMP_USER_ID_TAG = 2


def _nx(text: str) -> str:
    """Make text safe inside a Nextion string literal."""
    return str(text).replace("\\", "/").replace('"', "'")

class NextionDisplay(IDisplayDriver):
    def __init__(self, port: str, baudrate: int, logger: Callable, pic_car_connected: int, pic_car_disconnected: int):
        self.port = port
        self.baudrate = baudrate
        self.logger = logger
        self.pic_car_connected = pic_car_connected
        self.pic_car_disconnected = pic_car_disconnected
        self._serial = None

    def open(self) -> None:
        try:
            import serial
            self._serial = serial.Serial(self.port, self.baudrate, timeout=0.1)
            self.logger("INFO", f"Nextion bağlandı → {self.port} @ {self.baudrate}")
        except Exception as e:
            self.logger("WARN", f"Nextion açılamadı: {e} — ekran komutları devre dışı")
            self._serial = None

    def close(self) -> None:
        if self._serial:
            self._serial.close()
            self._serial = None

    def send_command(self, cmd: str) -> None:
        if self._serial is None:
            return
        try:
            self._serial.write((cmd + "\xff\xff\xff").encode("latin-1"))
        except Exception as e:
            self.logger("WARN", f"Nextion yazma hatası: {e}")

    def set_status(self, status: str) -> None:
        normalized = (status or "NOT CONNECTED").strip().upper()
        
        colors = {
            "NOT CONNECTED": RGB565.RED,
            "CONNECTED":     RGB565.CYAN,
            "AVAILABLE":     RGB565.CYAN,
            "PREPARING":     RGB565.CYAN,
            "CHARGING":      RGB565.GREEN,
            "SUSPENDEDEV":   RGB565.YELLOW,
            "SUSPENDEDEVSE": RGB565.YELLOW,
            "FINISHING":     RGB565.CYAN,
            "VENTILATION":   RGB565.YELLOW,
            "FAULT":         RGB565.RED,
            "FAULTED":       RGB565.RED,
            "UNAVAILABLE":   RGB565.RED,
        }

        display_text = {
            "NOT CONNECTED": "Not Connected",
            "CONNECTED":     "Connected",
            "AVAILABLE":     "Available",
            "PREPARING":     "Preparing",
            "CHARGING":      "Charging",
            "SUSPENDEDEV":   "Suspended EV",
            "SUSPENDEDEVSE": "Suspended",
            "FINISHING":     "Finishing",
            "VENTILATION":   "Ventilation",
            "FAULT":         "Fault",
            "FAULTED":       "Fault",
            "UNAVAILABLE":   "Unavailable",
        }
        text_to_show = display_text.get(normalized, normalized.title())

        if normalized == "CHARGING":
            pic = 0
        elif normalized in ("NOT CONNECTED", "FAULT", "FAULTED", "UNAVAILABLE"):
            pic = self.pic_car_disconnected
        else:
            pic = self.pic_car_connected

        pco = colors.get(normalized, RGB565.RED)
        self.send_command(f'con.txt="{_nx(text_to_show)}"')
        self.send_command(f"con.pco={pco}")
        self.send_command(f"araba.pic={pic}")

    def set_time(self) -> None:
        from datetime import timezone
        utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.send_command(f'saat.txt="{utc}"')

    def set_user_id(self, id_tag: str) -> None:
        self.send_command(f'userinfo.txt="{_nx(id_tag)}"')

    def update_charging_info(self, energy_kwh: float, power_kw: float, elapsed_seconds: float, cost: float, currency: str = "$") -> None:
        cur = _nx(currency)
        if elapsed_seconds >= 0:
            h = int(elapsed_seconds // 3600)
            m = int((elapsed_seconds % 3600) // 60)
            s = int(elapsed_seconds % 60)
            self.send_command(f'time.txt="TIME : {h:02d}.{m:02d}.{s:02d}"')
            self.send_command(f'energy.txt="amount of use : {energy_kwh:.2f} kWh"')
            self.send_command(f'cost.txt="{cost:.2f} {cur}"')
            self.send_command(f'power.txt="POWER : {max(power_kw, 0.0):.2f} kW"')
        else:
            self.send_command('time.txt="TIME : 00.00.00"')
            self.send_command('energy.txt="amount of use : 0.00 kWh"')
            self.send_command(f'cost.txt="0.00 {cur}"')
            self.send_command('power.txt="POWER : 0.00 kW"')

    def show_page(self, page: str) -> None:
        self.send_command(f'page {page}')

    async def read_touch_events(self) -> AsyncIterator[TouchEvent]:
        loop = asyncio.get_running_loop()
        buf = bytearray()
        while True:
            if self._serial is None:
                await asyncio.sleep(0.1)
                continue
            try:
                chunk = await loop.run_in_executor(None, self._serial.read, 32)
                if chunk:
                    buf.extend(chunk)
                while len(buf) >= 7:
                    idx = buf.find(0x65)
                    if idx == -1:
                        buf.clear()
                        break
                    if idx > 0:
                        del buf[:idx]
                    if len(buf) < 7:
                        break
                    if buf[4] == 0xFF and buf[5] == 0xFF and buf[6] == 0xFF:
                        page_id = buf[1]
                        comp_id = buf[2]
                        event = buf[3]
                        del buf[:7]
                        yield TouchEvent(page_id=page_id, component_id=comp_id, event_type=event)
                    else:
                        del buf[:1]
            except Exception as e:
                self.logger("WARN", f"Nextion okuma hatası: {e}")
                await asyncio.sleep(0.1)

class NullDisplay(IDisplayDriver):
    def open(self) -> None:
        pass
    def close(self) -> None:
        pass
    def send_command(self, cmd: str) -> None:
        pass
    def set_status(self, status: str) -> None:
        pass
    def set_time(self) -> None:
        pass
    def set_user_id(self, id_tag: str) -> None:
        pass
    def update_charging_info(self, energy_kwh: float, power_kw: float, elapsed_seconds: float, cost: float, currency: str = "$") -> None:
        pass
    def show_page(self, page: str) -> None:
        pass
    async def read_touch_events(self) -> AsyncIterator[TouchEvent]:
        # Async generator that never yields an event.
        while True:
            await asyncio.sleep(3600)
            if False:  # pragma: no cover
                yield TouchEvent(0, 0, 0)
