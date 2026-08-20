import time
from abc import ABC, abstractmethod
from typing import Optional

class INfcReader(ABC):
    @abstractmethod
    def init(self) -> None:
        pass

    @abstractmethod
    def read_uid(self) -> Optional[bytes]:
        pass

class Pn532Reader(INfcReader):
    def __init__(self):
        self.bus = None
        self.PN532_ADDR = 0x24

    def init(self) -> None:
        import smbus2
        self.bus = smbus2.SMBus(1)
        self.pn532_write([0x00, 0x00, 0xFF, 0x03, 0xFD, 0xD4, 0x14, 0x01, 0x17, 0x00])
        time.sleep(0.05)
        
        self.pn532_read(6)
        time.sleep(0.05)
        
        self.pn532_read(8)

    def pn532_write(self, data):
        import smbus2
        try:
            msg = smbus2.i2c_msg.write(self.PN532_ADDR, data)
            self.bus.i2c_rdwr(msg)
        except OSError:
            time.sleep(0.05)
            try:
                msg = smbus2.i2c_msg.write(self.PN532_ADDR, data)
                self.bus.i2c_rdwr(msg)
            except:
                pass

    def pn532_read(self, length):
        import smbus2
        for _ in range(15):
            try:
                msg = smbus2.i2c_msg.read(self.PN532_ADDR, length + 1)
                self.bus.i2c_rdwr(msg)
                resp = list(msg)
                if resp[0] == 0x01:
                    return resp[1:]
            except OSError:
                pass
            time.sleep(0.05)
        return None

    def read_uid(self) -> Optional[bytes]:
        self.pn532_write([0x00, 0x00, 0xFF, 0x04, 0xFC, 0xD4, 0x4A, 0x01, 0x00, 0xE1, 0x00])
        time.sleep(0.05)
        
        ack = self.pn532_read(6)
        if not ack or len(ack) < 6:
            return None
            
        time.sleep(0.1)
        
        resp = self.pn532_read(22)
        
        if resp and len(resp) >= 13:
            if resp[5] == 0xD5 and resp[6] == 0x4B and resp[7] > 0:
                uid_len = resp[12]
                uid = resp[13:13+uid_len]
                return bytes(uid)
                
        return None

class MockNfcReader(INfcReader):
    def __init__(self, uid: Optional[bytes] = None):
        self.uid = uid

    def init(self) -> None:
        pass

    def read_uid(self) -> Optional[bytes]:
        return self.uid
