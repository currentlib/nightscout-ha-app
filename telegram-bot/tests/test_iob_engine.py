"""Unit tests for app/services/iob_engine.py's suggest_bolus and calculate_fpu.

Pure functions, no DB, no event loop. Unexecuted in this environment (no Python
interpreter available in this sandbox) — run with `pytest` from the repo root
once a real environment is available.
"""
from app.services.iob_engine import (
    suggest_bolus,
    calculate_fpu,
    calculate_hypo_carb_target,
    calculate_prebolus_minutes,
    MAX_SINGLE_DOSE,
)


def test_hypo_fast_carbs_sufficient():
    r = suggest_bolus(carbs=20, current_glucose=3.4, icr=10, isf=3, target_bg=5.5, current_iob=0)
    assert r["total_suggested_dose"] == 0.0
    # weight=None -> hypo_carb_target falls back to the pre-existing flat 15g
    assert "вже покривають потрібну дозу для корекції гіпоглікемії (15г)" in r["timing_hint"]


def test_hypo_fast_carbs_insufficient():
    r = suggest_bolus(carbs=5, current_glucose=3.4, icr=10, isf=3, target_bg=5.5, current_iob=0)
    assert r["total_suggested_dose"] == 0.0
    assert "недостатньо для повної корекції" in r["timing_hint"]
    assert "Додайте ще" in r["timing_hint"]


def test_hypo_no_food_plain_glucose_check():
    # Regression case: carbs=0 must NOT be classified as "slow food" (is_fast_acting is False
    # for both "no food" and "slow food" — the function must special-case carbs<=0 first).
    r = suggest_bolus(carbs=0, current_glucose=3.4, icr=10, isf=3, target_bg=5.5, current_iob=0)
    assert r["total_suggested_dose"] == 0.0
    assert "Спочатку купіруйте гіпоглікемію" in r["timing_hint"]
    assert "засвоюється повільно" not in r["timing_hint"]


def test_hypo_slow_digesting_food():
    r = suggest_bolus(carbs=30, current_glucose=3.4, icr=10, isf=3, target_bg=5.5,
                       current_iob=0, protein=20, fat=15)
    assert r["total_suggested_dose"] == 0.0
    assert "засвоюється повільно" in r["timing_hint"]


def test_dose_clamped_to_zero_not_negative():
    # meal_bolus (1.0) - current_iob (5.0) would be negative without the max(0.0, ...) guard.
    r = suggest_bolus(carbs=10, current_glucose=5.5, icr=10, isf=3, target_bg=5.5, current_iob=5.0)
    assert r["total_suggested_dose"] == 0.0
    assert r["meal_bolus"] == 1.0


def test_high_glucose_correction_arithmetic():
    r = suggest_bolus(carbs=40, current_glucose=12.0, icr=10, isf=3.0, target_bg=6.0, current_iob=1.0)
    assert r["meal_bolus"] == 4.0
    assert r["correction_bolus"] == 2.0
    assert r["total_suggested_dose"] == 5.0
    assert "Цукор підвищений" in r["timing_hint"]
    # bg_delta=6.0, insulin_type=None->rapid(base=15), protein=fat=0 (fast, gi_offset=+10):
    # total = 15 + 10 + (6.0/2.8)*10 = 46.428..., round(46.428/5)*5 = round(9.2857)*5 = 9*5 = 45
    assert "зачекайте 45 хв" in r["timing_hint"]


def test_severe_hypo_flat_carb_target_overrides_weight():
    r = suggest_bolus(carbs=0, current_glucose=2.5, icr=10, isf=3, target_bg=5.5, current_iob=0, weight=80)
    assert r["total_suggested_dose"] == 0.0
    assert "Критично низький" in r["timing_hint"]
    assert "20г" in r["timing_hint"]


def test_severe_hypo_takes_priority_over_fast_carb_sufficient_branch():
    # carbs=20 would satisfy the non-severe "already sufficient" branch (carbs >= hypo target) -
    # severe must be checked first and take priority regardless.
    r = suggest_bolus(carbs=20, current_glucose=2.5, icr=10, isf=3, target_bg=5.5, current_iob=0)
    assert "Критично низький" in r["timing_hint"]
    assert "вже покривають" not in r["timing_hint"]


def test_calculate_hypo_carb_target():
    assert calculate_hypo_carb_target(None, severe=False) == 15.0
    assert calculate_hypo_carb_target(30, severe=False) == 9.0
    assert calculate_hypo_carb_target(75, severe=False) == 15.0  # capped at 15
    assert calculate_hypo_carb_target(30, severe=True) == 20.0
    assert calculate_hypo_carb_target(None, severe=True) == 20.0  # severe ignores weight entirely


def test_calculate_prebolus_minutes_fast_insulin_fast_food_at_target():
    # base=5 (ultra_rapid) + gi_offset=10 (fat_protein=0<5) + delta_offset=0 (bg_delta=0) = 15
    assert calculate_prebolus_minutes("ultra_rapid", protein=0, fat=0, bg_delta=0, current_glucose=6.0) == 15


def test_calculate_prebolus_minutes_slow_insulin_slow_food_at_target():
    # base=30 (regular) + gi_offset=-10 (fat_protein=50>20) + delta_offset=0 = 20
    assert calculate_prebolus_minutes("regular", protein=30, fat=20, bg_delta=0, current_glucose=6.0) == 20


def test_calculate_prebolus_minutes_clamped_to_floor():
    # base=5 - 10 + (-20/2.8)*10=-71.428... = -76.428..., /5=-15.2857, round=-15, *5=-75, clamped to -15
    assert calculate_prebolus_minutes("ultra_rapid", protein=30, fat=20, bg_delta=-20, current_glucose=5.0) == -15


def test_calculate_prebolus_minutes_near_hypo_override():
    # current_glucose <= 4.0 forces total to 0 regardless of every other input
    assert calculate_prebolus_minutes("regular", protein=0, fat=0, bg_delta=-1.5, current_glucose=4.0) == 0


def test_calculate_prebolus_minutes_unrecognized_insulin_type_falls_back_to_rapid():
    # unknown insulin_type -> same base as "rapid" (15): 15 + 10 + 0 = 25
    assert calculate_prebolus_minutes("bogus", protein=0, fat=0, bg_delta=0, current_glucose=6.0) == 25


def test_dose_clamped_to_max_single_dose():
    r = suggest_bolus(carbs=200, current_glucose=6.0, icr=10, isf=3.0, target_bg=6.0, current_iob=0)
    assert r["total_suggested_dose"] == MAX_SINGLE_DOSE == 15.0


def test_fpu_zero_when_no_protein_or_fat():
    fpu = calculate_fpu(protein=0, fat=0, icr=10)
    assert fpu == {"fpu": 0.0, "dose": 0.0, "delay_hours": 0}


def test_fpu_delay_tier_boundaries():
    assert calculate_fpu(protein=25, fat=0, icr=10)["delay_hours"] == 3   # fpu = 1.0
    assert calculate_fpu(protein=50, fat=0, icr=10)["delay_hours"] == 4   # fpu = 2.0
    assert calculate_fpu(protein=75, fat=0, icr=10)["delay_hours"] == 5   # fpu = 3.0
    assert calculate_fpu(protein=100, fat=0, icr=10)["delay_hours"] == 8  # fpu = 4.0


def test_fpu_dose_rounds_to_nearest_half_unit():
    # fpu_count = 1.3, icr = 10 -> raw_dose = 1.3 -> round(2.6)/2 = 1.5
    fpu = calculate_fpu(protein=32.5, fat=0, icr=10)
    assert fpu["fpu"] == 1.3
    assert fpu["dose"] == 1.5
    assert fpu["delay_hours"] == 4
