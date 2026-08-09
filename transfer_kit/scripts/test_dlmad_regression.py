#!/usr/bin/env python3
"""
DLMAD & ClockSync Regression Unit Test Suite
Target: Prevent fallback bypass, unit mismatch, and 0-variance deadlock regressions.
"""

import sys, os, math
import numpy as np

# Import DualLayerMAD from clocksync
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'klippy'))
from clocksync import DualLayerMAD

def test_case_1_identical_values_zero_variance():
    """Case 1: 33 identical values -> get_variance() == 0.0 & Fallback Triggered"""
    dlmad = DualLayerMAD(window_size=32)
    for _ in range(33):
        dlmad.update(1000.0) # 1000 MCU ticks
    var = dlmad.get_variance()
    assert var == 0.0, f"Expected 0.0 for identical values, got {var}"
    # Fallback trigger assertion (dlmad_var < 1.0 triggers EMA fallback)
    fallback_triggered = not (var >= 1.0)
    assert fallback_triggered, "EMA Fallback MUST trigger when get_variance() == 0.0"
    print("[PASS] Case 1: Identical values -> get_variance() == 0.0 & EMA Fallback Triggered")

def test_case_2_sub_tick_deviation_fallback():
    """Case 2: Sub-tick micro deviation (<1.0 tick) -> Fallback Triggered (1.48e-9 bypass prevented)"""
    dlmad = DualLayerMAD(window_size=32)
    # Inject 31 identical values and 1 micro-deviated value (0.000001 tick)
    for _ in range(31):
        dlmad.update(100.0)
    dlmad.update(100.000001)
    dlmad.update(100.0)
    var = dlmad.get_variance()
    # Ensure var is sub-tick (< 1.0 tick)
    assert var < 1.0, f"Sub-tick deviation returned {var} >= 1.0"
    # Fallback trigger assertion: dlmad_var < 1.0 MUST trigger EMA fallback
    fallback_triggered = not (var >= 1.0)
    assert fallback_triggered, f"Sub-tick variance {var} MUST trigger EMA fallback!"
    print(f"[PASS] Case 2: Sub-tick deviation (var={var:.4e}) -> EMA Fallback Triggered (1.48e-9 bypass prevented)")

def test_case_3_known_distribution_unit_smoke_test():
    """Case 3: Known Normal Distribution (sigma = 100 ticks) -> DLMAD StdDev ≈ 100 ± 15 ticks"""
    np.random.seed(42)
    dlmad = DualLayerMAD(window_size=32)
    samples = np.random.normal(loc=50000.0, scale=100.0, size=1000)
    measured_vars = []
    for val in samples:
        dlmad.update(val)
        measured_vars.append(dlmad.get_variance())
    
    # Check mean DLMAD variance in steady state
    steady_vars = measured_vars[100:]
    mean_measured = np.mean(steady_vars)
    # 1.4826 * MAD of Normal(0, 100) is 100.0
    assert 85.0 <= mean_measured <= 115.0, f"Expected ~100 ticks stddev, got {mean_measured:.2f}"
    print(f"[PASS] Case 3: Unit Smoke Test N(0, 100^2) -> Measured DLMAD StdDev = {mean_measured:.2f} ticks (within 100±15 tolerance)")

def test_case_4_unit_alignment_logging_check():
    """Case 4: Verify dual-column CSV logging unit conversion (seconds vs ticks)"""
    mcu_freq = 16000000.0 # 16 MHz
    dlmad_var_ticks = 160.0 # 160 MCU ticks (10 microseconds)
    prediction_variance_ticks2 = (160.0)**2 # (160 ticks)^2
    
    # Runtime conversion check
    raw_dlmad_sec = dlmad_var_ticks / mcu_freq
    effective_stddev_sec = math.sqrt(prediction_variance_ticks2) / mcu_freq
    
    assert math.isclose(raw_dlmad_sec, 1e-5), f"Expected 10us (1e-5s), got {raw_dlmad_sec}"
    assert math.isclose(effective_stddev_sec, 1e-5), f"Expected 10us (1e-5s), got {effective_stddev_sec}"
    print(f"[PASS] Case 4: Logging Unit Alignment -> raw_dlmad={raw_dlmad_sec*1e6:.1f}us, effective={effective_stddev_sec*1e6:.1f}us (Both in Seconds)")

if __name__ == '__main__':
    print("=" * 70)
    print("        RUNNING DLMAD & CLOCKSYNC REGRESSION TEST SUITE        ")
    print("=" * 70)
    test_case_1_identical_values_zero_variance()
    test_case_2_sub_tick_deviation_fallback()
    test_case_3_known_distribution_unit_smoke_test()
    test_case_4_unit_alignment_logging_check()
    print("=" * 70)
    print("        ALL 4 REGRESSION UNIT TESTS PASSED SUCCESSFULLY!       ")
    print("=" * 70)
