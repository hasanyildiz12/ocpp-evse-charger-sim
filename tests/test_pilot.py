import pytest

from ocpp_charger.hardware.cp_signal import (
    MockCpReader,
    adc_from_cp_voltage,
    cable_current_from_pp,
    cp_voltage_from_adc,
    pilot_high_from_average,
    pilot_state_from_voltage,
    pp_resistance_from_adc,
)
from ocpp_charger.hardware.pilot import MockPilotPwm, current_from_duty, duty_from_current

R = (200_000.0, 56_000.0, 47_000.0, 3.3)  # R7, R6, R5, Vref from the schematic


@pytest.mark.parametrize("v_cp, v_adc", [(12, 2.950), (9, 2.610), (6, 2.271), (3, 1.931), (0, 1.591), (-12, 0.231)])
def test_divider_matches_schematic(v_cp, v_adc):
    assert adc_from_cp_voltage(v_cp, *R) == pytest.approx(v_adc, abs=0.002)
    assert cp_voltage_from_adc(v_adc, *R) == pytest.approx(v_cp, abs=0.02)


def test_adc_stays_within_ads1115_input_range():
    assert 0.0 < adc_from_cp_voltage(-13, *R) and adc_from_cp_voltage(13, *R) < 3.3


@pytest.mark.parametrize("amps, duty", [(6, 0.10), (16, 0.2667), (32, 0.5333), (51, 0.85), (63, 0.892)])
def test_duty_from_current(amps, duty):
    assert duty_from_current(amps) == pytest.approx(duty, abs=1e-3)
    assert current_from_duty(duty_from_current(amps)) == pytest.approx(amps, abs=0.1)


def test_duty_below_6a_rejected():
    with pytest.raises(ValueError):
        duty_from_current(5)


def test_pilot_high_from_average():
    d = 0.2667
    for high in (12, 9, 6):
        avg = d * high + (1 - d) * -12
        assert pilot_high_from_average(avg, d, -12) == pytest.approx(high, abs=1e-6)
    assert pilot_high_from_average(9.0, 1.0, -12) == 9.0


@pytest.mark.parametrize("v, s", [(12, "A"), (10.6, "A"), (9, "B"), (6, "C"), (3, "D"), (0.5, "E"), (-12, "E")])
def test_state_thresholds(v, s):
    assert pilot_state_from_voltage(v) == s


@pytest.mark.parametrize("rc, amps", [(None, None), (1500, 13), (680, 20), (220, 32), (100, 63), (3000, None)])
def test_pp_cable_detection(rc, amps):
    v = 3.3 if rc is None else 3.3 * rc / (rc + 330.0)
    assert cable_current_from_pp(pp_resistance_from_adc(v, 330.0, 3.3)) == amps


def test_mock_reader_auto_ev_goes_to_c_with_pwm():
    pwm = MockPilotPwm()
    pwm.open()
    ev = MockCpReader(state="B", pwm=pwm)
    assert cp_voltage_from_adc(ev.read_cp_adc(), *R) == pytest.approx(9.0, abs=0.01)
    pwm.offer_current(16)
    avg = cp_voltage_from_adc(ev.read_cp_adc(), *R)
    assert pilot_high_from_average(avg, pwm.duty, -12) == pytest.approx(6.0, abs=0.01)


def test_pwm_states():
    pwm = MockPilotPwm()
    pwm.open()
    assert pwm.duty == 1.0 and not pwm.offering
    pwm.offer_current(16)
    assert pwm.offering
    pwm.disable()
    assert pwm.duty == 0.0


# ─── Energy estimate (no meter on the board) ─────────────────────────────────
from ocpp_charger.hardware.meter import EstimatedMeter


def test_estimated_meter_integrates_power():
    now = [0.0]
    m = EstimatedMeter(power_fn=lambda: 3680.0, clock=lambda: now[0])
    assert m.read_wh() == 0
    now[0] = 3600.0
    assert m.read_wh() == 3680


def test_estimated_meter_idle_does_not_count():
    now = [0.0]
    m = EstimatedMeter(power_fn=lambda: 0.0, clock=lambda: now[0])
    now[0] = 3600.0
    assert m.read_wh() == 0
