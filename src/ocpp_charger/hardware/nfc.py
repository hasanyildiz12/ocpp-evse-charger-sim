"""PN532 NFC reader over I2C (address 0x24, bus 1).

The PN532 module's mode DIP switches must be set to I2C (check the table on
the module silkscreen); `i2cdetect -y 1` should then show 0x24.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque, Optional


class INfcReader(ABC):
    @abstractmethod
    def init(self) -> None:
        pass

    @abstractmethod
    def read_uid(self) -> Optional[bytes]:
        """Return the UID of a card in the field, or None."""

    def close(self) -> None:
        pass


class Pn532Reader(INfcReader):
    PN532_ADDR = 0x24

    def __init__(self, bus: int = 1):
        self.bus_no = bus
        self.bus = None

    def init(self) -> None:
        import smbus2  # type: ignore
        self.bus = smbus2.SMBus(self.bus_no)
        # SAMConfiguration: normal mode, no timeout, no IRQ
        self.pn532_write([0x00, 0x00, 0xFF, 0x03, 0xFD, 0xD4, 0x14, 0x01, 0x17, 0x00])
        time.sleep(0.05)
        ack = self.pn532_read(6)
        time.sleep(0.05)
        self.pn532_read(8)
        if ack is None:
            raise OSError("PN532 0x24 adresinde yanıt vermedi (I2C modu / kablolama?)")

    def close(self) -> None:
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
            self.bus = None

    def pn532_write(self, data) -> None:
        import smbus2  # type: ignore
        for _ in range(2):
            try:
                self.bus.i2c_rdwr(smbus2.i2c_msg.write(self.PN532_ADDR, data))
                return
            except OSError:
                time.sleep(0.05)

    def pn532_read(self, length):
        import smbus2  # type: ignore
        for _ in range(15):
            try:
                msg = smbus2.i2c_msg.read(self.PN532_ADDR, length + 1)
                self.bus.i2c_rdwr(msg)
                resp = list(msg)
                if resp[0] == 0x01:  # ready byte
                    return resp[1:]
            except OSError:
                pass
            time.sleep(0.05)
        return None

    def read_uid(self) -> Optional[bytes]:
        # InListPassiveTarget, 1 target, 106 kbps type A
        self.pn532_write([0x00, 0x00, 0xFF, 0x04, 0xFC, 0xD4, 0x4A, 0x01, 0x00, 0xE1, 0x00])
        time.sleep(0.05)

        ack = self.pn532_read(6)
        if not ack or len(ack) < 6:
            return None

        time.sleep(0.1)
        resp = self.pn532_read(22)
        if resp and len(resp) >= 13 and resp[5] == 0xD5 and resp[6] == 0x4B and resp[7] > 0:
            uid_len = resp[12]
            return bytes(resp[13:13 + uid_len])
        return None


class MockNfcReader(INfcReader):
    """Card taps queued from the console (``card <UID>``) or tests."""

    def __init__(self, uid: Optional[bytes] = None):
        self._queue: Deque[bytes] = deque()
        if uid:
            self._queue.append(uid)

    def init(self) -> None:
        pass

    def tap(self, uid: bytes) -> None:
        self._queue.append(uid)

    def read_uid(self) -> Optional[bytes]:
        return self._queue.popleft() if self._queue else None
