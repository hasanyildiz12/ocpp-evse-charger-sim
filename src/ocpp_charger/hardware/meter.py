from __future__ import annotations

import math
import os
import site as _site
import struct
import sys
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional, Tuple

try:
    user_site = _site.getusersitepackages()
    if user_site and user_site not in sys.path:
        _site.addsitedir(user_site)
except Exception:
    pass

LogFn = Callable[[str, str], None]

class IMeterReader(ABC):
    @abstractmethod
    def read_wh(self) -> Optional[int]:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def configure_logger(self, log_fn: LogFn) -> None:
        pass

def _load_modbus_client():
    try:
        from pymodbus.client import ModbusSerialClient
        return ModbusSerialClient
    except ImportError:
        try:
            from pymodbus.client.sync import ModbusSerialClient
            return ModbusSerialClient
        except ImportError as exc:
            raise ImportError(
                "pymodbus paketi bulunamadi. Kurulum: "
                f"{sys.executable} -m pip install pymodbus pyserial"
            ) from exc

def _read_input_registers(client, address: int, count: int, slave_id: int):
    errors: List[Exception] = []

    for kwargs in (
        {"address": address, "count": count, "slave": slave_id},
        {"address": address, "count": count, "device_id": slave_id},
        {"address": address, "count": count, "unit": slave_id},
    ):
        try:
            return client.read_input_registers(**kwargs)
        except TypeError as exc:
            errors.append(exc)

    try:
        return client.read_input_registers(address, count, slave=slave_id)
    except TypeError as exc:
        errors.append(exc)

    try:
        return client.read_input_registers(address, count, unit=slave_id)
    except TypeError as exc:
        errors.append(exc)

    if errors:
        raise errors[-1]

    raise TypeError("Unsupported pymodbus read_input_registers API")

class ModbusMeter(IMeterReader):
    def __init__(self, log: Optional[LogFn] = None):
        self.client = None
        self.log = log or self._default_log
        self._lock = threading.Lock()
        self._last_format: Optional[str] = None
        self._last_candidates: Dict[str, float] = {}

        self.port = os.environ.get("METER_PORT", "/dev/ttyUSB0")
        self.slave_id = int(os.environ.get("METER_SLAVE_ID", "1"))
        self.baudrate = int(os.environ.get("METER_BAUDRATE", "9600"))
        self.bytesize = int(os.environ.get("METER_BYTESIZE", "8"))
        self.parity = os.environ.get("METER_PARITY", "E")
        self.stopbits = int(os.environ.get("METER_STOPBITS", "1"))
        self.timeout = float(os.environ.get("METER_TIMEOUT", "1"))
        self.start_address = int(os.environ.get("METER_START_ADDRESS", "342"))
        self.register_count = int(os.environ.get("METER_REGISTER_COUNT", "2"))
        self.max_valid_kwh = float(os.environ.get("METER_MAX_VALID_KWH", "10000000.0"))

    @staticmethod
    def _default_log(level: str, message: str):
        print(f"{level}: {message}")

    def configure_logger(self, log_fn: LogFn) -> None:
        self.log = log_fn

    def connect(self):
        if self.client is not None:
            return self.client

        ModbusSerialClient = _load_modbus_client()

        try:
            client = ModbusSerialClient(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout,
            )
        except TypeError:
            client = ModbusSerialClient(
                method="rtu",
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout,
            )

        if not client.connect():
            raise ConnectionError(f"Could not open serial port {self.port}")

        self.client = client
        self.log("INFO", f"Modbus sayac baglandi -> {self.port}")
        return self.client

    def close(self):
        if self.client is None:
            return

        try:
            self.client.close()
        except Exception:
            pass
        finally:
            self.client = None

    def decode_float(self, registers: List[int]) -> Tuple[Optional[float], Dict[str, float]]:
        if not registers or len(registers) != 2:
            return None, {}

        r0, r1 = registers
        byte_orders = {
            "Big-endian (word, byte) ABCD": (
                r0.to_bytes(2, "big") + r1.to_bytes(2, "big"),
                ">",
            ),
            "Little-endian DCBA": (
                r1.to_bytes(2, "little") + r0.to_bytes(2, "little"),
                "<",
            ),
            "Big-endian byte swap BADC": (
                r0.to_bytes(2, "little") + r1.to_bytes(2, "little"),
                "<",
            ),
            "Little-endian byte swap CDAB": (
                r1.to_bytes(2, "big") + r0.to_bytes(2, "big"),
                ">",
            ),
        }

        candidates: Dict[str, float] = {}
        for name, (raw_bytes, fmt) in byte_orders.items():
            try:
                candidates[name] = struct.unpack(fmt + "f", raw_bytes)[0]
            except Exception:
                candidates[name] = float("nan")

        valid = {
            name: value
            for name, value in candidates.items()
            if math.isfinite(value) and 0 <= value < self.max_valid_kwh
        }

        if not valid:
            return None, candidates

        changing_valid = {}
        for name, value in valid.items():
            previous = self._last_candidates.get(name)
            if previous is not None and value >= previous and value != previous:
                changing_valid[name] = value

        if changing_valid:
            chosen_name, chosen_value = min(
                changing_valid.items(),
                key=lambda item: item[1] - self._last_candidates[item[0]],
            )
        elif self._last_format in valid:
            chosen_name = self._last_format
            chosen_value = valid[chosen_name]
        elif "Big-endian (word, byte) ABCD" in valid:
            chosen_name = "Big-endian (word, byte) ABCD"
            chosen_value = valid[chosen_name]
        else:
            chosen_name, chosen_value = next(iter(valid.items()))

        self._last_format = chosen_name
        self._last_candidates = candidates.copy()
        return chosen_value, candidates

    def read_kwh(self) -> Optional[float]:
        with self._lock:
            return self._read_kwh_locked()

    def _read_kwh_locked(self) -> Optional[float]:
        try:
            client = self.connect()
            response = _read_input_registers(
                client=client,
                address=self.start_address,
                count=self.register_count,
                slave_id=self.slave_id,
            )

            if response is None:
                self.log("ERR", "Sayac bos Modbus cevabi dondu")
                return None

            if hasattr(response, "isError") and response.isError():
                self.log("ERR", f"Sayac Modbus hata cevabi: {response}")
                return None

            registers = getattr(response, "registers", None)
            if not registers or len(registers) != self.register_count:
                self.log("ERR", f"Sayac register cevabi gecersiz: {registers}")
                return None

            value, _candidates = self.decode_float(registers)
            if value is None:
                self.log("ERR", f"Sayac kWh decode edilemedi: {registers}")
                return None

            self.log("INFO", f"Sayac okundu: {value:.3f} kWh ({self._last_format})")
            return value

        except Exception as exc:
            self.log("ERR", f"Sayac okuma hatasi: {exc}")
            self.close()
            return None

    def read_wh(self) -> Optional[int]:
        value = self.read_kwh()
        if value is None:
            return None
        return int(round(value * 1000))

class MockMeter(IMeterReader):
    def __init__(self, initial_wh: int = 0, increment_wh: int = 500):
        self.initial_wh = initial_wh
        self.increment_wh = increment_wh
        self.call_count = 0
        self.log = lambda l, m: None

    def configure_logger(self, log_fn: LogFn) -> None:
        self.log = log_fn

    def read_wh(self) -> Optional[int]:
        val = self.initial_wh + (self.call_count * self.increment_wh)
        self.call_count += 1
        return val

    def close(self) -> None:
        pass
