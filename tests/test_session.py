import time
from ocpp_charger.session import MeterSession

def test_initial_state():
    session = MeterSession(start_wh=1000, log=lambda *args: None)
    assert session.meter_start_wh == 1000
    assert session.meter_wh == 1000
    assert session.energy_kwh == 0.0
    assert session.measured_power_w == 0

def test_update_increases():
    session = MeterSession(start_wh=1000, log=lambda *args: None)
    reported = session.update(1200)
    assert reported == 1200
    assert session.meter_wh == 1200
    assert session.energy_kwh == 0.2

def test_update_negative_delta():
    session = MeterSession(start_wh=1000, log=lambda *args: None)
    session.update(1200)
    
    # Simulate negative delta (e.g., meter bug)
    reported = session.update(1100)
    assert reported == 1200  # Should keep last valid value
    assert session.meter_wh == 1200
    assert session.energy_kwh == 0.2

def test_energy_kwh_calculation():
    session = MeterSession(start_wh=1000, log=lambda *args: None)
    session.update(2500)
    # (2500 - 1000) / 1000 = 1.5
    assert session.energy_kwh == 1.5

def test_power_calculation(monkeypatch):
    session = MeterSession(start_wh=1000, log=lambda *args: None)
    
    # Mock time.time() to simulate elapsed time
    start_time = 1000.0
    monkeypatch.setattr(time, "time", lambda: start_time)
    
    session.last_meter_read_ts = start_time
    
    monkeypatch.setattr(time, "time", lambda: start_time + 3600.0) # 1 hour later
    session.update(2000)
    
    # Delta Wh = 1000, elapsed = 3600s
    # Power = (1000 * 3600) / 3600 = 1000 W
    assert session.measured_power_w == 1000
