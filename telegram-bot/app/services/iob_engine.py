import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from app.database import InsulinInjection, LogEntry

logger = logging.getLogger(__name__)

# Safety clamps
MAX_SINGLE_DOSE = 15.0  # Units limit to prevent accidental overdose suggested values

# Base pre-bolus onset delay per insulin formulation (minutes) — from published PK profiles,
# also matches the constants used in Loop/OpenAPS's own pre-bolus guidance.
INSULIN_ONSET_MINUTES = {"ultra_rapid": 5, "rapid": 15, "regular": 30}

# Nightscout's carb absorption defaults.
CARB_DELAY_MINUTES = 20
DEFAULT_CARBS_PER_HOUR = 20.0


def calculate_hypo_carb_target(weight_kg: float | None, severe: bool) -> float:
    """
    Grams of fast-acting carbs to treat a hypo. ISPAD 2022: standard treatment is 0.3 g/kg
    (capped at 15g for adults, since we don't distinguish adult/pediatric elsewhere yet);
    severe hypoglycemia (<3.0 mmol/L) skips weight-scaling in favor of a flat, immediately
    adequate dose — speed and certainty matter more than precision when this low.
    Falls back to the pre-existing flat 15g when weight hasn't been set (no behavior change
    for users who never completed the physical-profile onboarding step).
    """
    if severe:
        return 20.0
    if not weight_kg:
        return 15.0
    return round(min(15.0, weight_kg * 0.3), 1)


def calculate_prebolus_minutes(insulin_type: str | None, protein: float, fat: float, bg_delta: float, current_glucose: float) -> int:
    """
    Minutes to wait after injecting before eating (negative = inject that many minutes AFTER
    starting the meal instead). Base onset delay per insulin type, offset by how fast the food
    will digest (proxied by protein+fat grams, since this app doesn't have a real glycemic-index
    database) and by how far current glucose is from target — every 2.8 mmol/L above target adds
    10 minutes to let insulin get a head start on the existing high, mirroring the standard
    clinical pre-bolus sliding scale.
    """
    base_wait = INSULIN_ONSET_MINUTES.get(insulin_type, INSULIN_ONSET_MINUTES["rapid"])

    fat_protein = protein + fat
    if fat_protein < 5.0:
        gi_offset = 10   # fast-digesting (low fat/protein) — head start needed sooner
    elif fat_protein <= 20.0:
        gi_offset = 0    # moderate
    else:
        gi_offset = -10  # slow-digesting (high fat/protein) — less urgency, food takes longer to hit

    delta_offset = (bg_delta / 2.8) * 10

    total = base_wait + gi_offset + delta_offset

    # Safety override: approaching hypoglycemia (but not yet in the dedicated hypo branch) —
    # don't give insulin a "head start", inject right at the meal instead.
    if current_glucose <= 4.0:
        total = 0

    total = round(total / 5) * 5
    return max(-15, min(45, int(total)))


def insulin_remaining(elapsed_hours: float, dia_hours: float) -> float:
    """Share of a dose still active, on Nightscout's curve (lib/plugins/iob.js).

    Same formula on purpose: the bot, Nightscout and Home Assistant must show one IOB, and
    a dose suggestion must not assume insulin has acted sooner than Nightscout does.
    """
    if elapsed_hours < 0:
        return 0.0
    scaled_minutes = elapsed_hours * 60 * 3.0 / dia_hours
    if scaled_minutes < 75:
        x = scaled_minutes / 5 + 1
        return 1 - 0.001852 * x * x + 0.001852 * x
    if scaled_minutes < 180:
        x = (scaled_minutes - 75) / 5
        return 0.001323 * x * x - 0.054233 * x + 0.55556
    return 0.0


def calculate_iob(injections: List[InsulinInjection], dia_hours: float) -> float:
    """Calculates active Insulin on Board (IOB) on Nightscout's curve."""
    now = datetime.now(timezone.utc)
    total_iob = 0.0

    for injection in injections:
        # Exclude basal (long-acting) insulin from active IOB
        if injection.injection_type != "bolus":
            continue

        # Normalize injection timestamp to timezone-aware UTC
        inj_time = injection.timestamp
        if inj_time.tzinfo is None:
            inj_time = inj_time.replace(tzinfo=timezone.utc)

        elapsed_seconds = (now - inj_time).total_seconds()
        elapsed_hours = elapsed_seconds / 3600.0

        if 0 <= elapsed_hours <= dia_hours:
            iob_from_dose = injection.dosage * insulin_remaining(elapsed_hours, dia_hours)
            total_iob += iob_from_dose
            logger.debug(f"Injection ID {injection.id}: dosage={injection.dosage}U, elapsed={elapsed_hours:.2f}h, active IOB={iob_from_dose:.2f}U")

    return round(total_iob, 2)


def carbs_active_at(meals: List[LogEntry], moment: datetime, carbs_per_hour: float) -> float:
    """Carbs still absorbing at `moment`, on Nightscout's model (lib/plugins/cob.js).

    Each meal waits CARB_DELAY_MINUTES, then absorbs at a fixed rate, and overlapping meals
    queue behind each other. Nightscout also slows absorption while insulin is active. That
    part isn't copied, so this runs slightly low while a bolus is working.
    """
    total = 0.0
    previous_end = None
    timed = []
    for meal in meals:
        if meal.carbs:
            when = meal.timestamp.replace(tzinfo=timezone.utc) if meal.timestamp.tzinfo is None else meal.timestamp
            timed.append((when, meal.carbs))
    for when, carbs in sorted(timed):
        if when >= moment:
            continue
        queued_minutes = (previous_end - when).total_seconds() / 60 if previous_end else 0.0
        wait = max(CARB_DELAY_MINUTES, queued_minutes)
        end = when + timedelta(minutes=wait + carbs / carbs_per_hour * 60)
        previous_end = end
        hours_left = (end - moment).total_seconds() / 3600.0
        total = total + min(carbs, hours_left * carbs_per_hour) if hours_left > 0 else 0.0
    return total


def calculate_cob(meals: List[LogEntry], carbs_per_hour: float = DEFAULT_CARBS_PER_HOUR) -> float:
    """Calculates active Carbs on Board (COB) on Nightscout's model."""
    return round(carbs_active_at(meals, datetime.now(timezone.utc), carbs_per_hour), 2)


def calculate_fpu(protein: float, fat: float, icr: float) -> Dict[str, Any]:
    """
    Calculates Fat-Protein Units (FPU) and suggested insulin dose.
    1 FPU = 100 kcal from protein (4 kcal/g) and fat (9 kcal/g).
    Dose = FPU * (10 / ICR), rounded to the nearest 0.5 units for insulin pens.
    """
    fp_calories = (protein * 4.0) + (fat * 9.0)
    fpu_count = fp_calories / 100.0
    
    if fpu_count <= 0:
        return {
            "fpu": 0.0,
            "dose": 0.0,
            "delay_hours": 0
        }
        
    # Dose equivalent to 10g carbs per FPU
    raw_dose = fpu_count * (10.0 / icr) if icr > 0 else 0.0
    
    # Round to nearest 0.5 units for standard MDI insulin pens
    dose = round(raw_dose * 2) / 2
    
    # Dynamic delay based on Warsaw/Pańkowska method guidelines
    if fpu_count <= 1.0:
        delay = 3
    elif fpu_count <= 2.0:
        delay = 4
    elif fpu_count <= 3.0:
        delay = 5
    else:
        delay = 8
        
    return {
        "fpu": round(fpu_count, 2),
        "dose": dose,
        "delay_hours": delay
    }


def predict_glucose_timeline(
    current_glucose: float,
    injections: List[InsulinInjection],
    meals: List[LogEntry],
    isf: float,
    icr: float,
    dia_hours: float,
    carbs_per_hour: float = DEFAULT_CARBS_PER_HOUR
) -> List[Dict[str, Any]]:
    """
    Generates predicted blood glucose progression for the next 4 hours at 30-min intervals.
    Projects remaining COB and IOB for each event individually to prevent stretching errors.
    Formula:
    BG(t) = BG(0) + Carbs_absorbed(t) * (ISF / ICR) - Insulin_absorbed(t) * ISF
    """
    now = datetime.now(timezone.utc)
    timeline = []
    
    # 1. Calculate the initial active IOB and COB at t=0
    initial_iob = 0.0
    for inj in injections:
        if inj.injection_type != "bolus":
            continue
        inj_time = inj.timestamp.replace(tzinfo=timezone.utc) if inj.timestamp.tzinfo is None else inj.timestamp
        elapsed_hours = (now - inj_time).total_seconds() / 3600.0
        if 0 <= elapsed_hours <= dia_hours:
            initial_iob += inj.dosage * insulin_remaining(elapsed_hours, dia_hours)
            
    initial_cob = carbs_active_at(meals, now, carbs_per_hour)

    # 2. Project timeline steps
    for step in range(9):
        minutes = step * 30
        future_time = now + timedelta(minutes=minutes)
        
        # Calculate active IOB at this future time point
        iob_val = 0.0
        for inj in injections:
            if inj.injection_type != "bolus":
                continue
            inj_time = inj.timestamp.replace(tzinfo=timezone.utc) if inj.timestamp.tzinfo is None else inj.timestamp
            elapsed_hours = (future_time - inj_time).total_seconds() / 3600.0
            if 0 <= elapsed_hours <= dia_hours:
                iob_val += inj.dosage * insulin_remaining(elapsed_hours, dia_hours)
                
        # Calculate active COB at this future time point
        cob_val = carbs_active_at(meals, future_time, carbs_per_hour)

        # Calculate cumulative acted insulin and absorbed carbs in the [0, t] interval
        insulin_acted = max(0.0, initial_iob - iob_val)
        carbs_absorbed = max(0.0, initial_cob - cob_val)
        
        bg_increase = carbs_absorbed * (isf / icr) if icr > 0 else 0.0
        bg_decrease = insulin_acted * isf
        
        predicted_bg = current_glucose + bg_increase - bg_decrease
        predicted_bg = max(2.0, predicted_bg)
        
        timeline.append({
            "timestamp": future_time.isoformat(),
            "glucose": round(predicted_bg, 1)
        })
        
    return timeline


def suggest_bolus(
    carbs: float,
    current_glucose: float,
    icr: float,
    isf: float,
    target_bg: float,
    current_iob: float,
    protein: float = 0.0,
    fat: float = 0.0,
    weight: float | None = None,
    insulin_type: str | None = None
) -> Dict[str, Any]:
    """
    Calculates suggestions for meal and correction boluses.
    Formula:
    Meal Bolus = Carbs / ICR
    Correction Bolus = (Current Glucose - Target Glucose) / ISF (supports negative correction if below target)
    Total Suggested = Meal Bolus + Correction Bolus - Current IOB
    """
    # Meal component
    meal_bolus = carbs / icr if icr > 0 else 0.0

    # Correction component (supports reverse correction for below target)
    bg_delta = current_glucose - target_bg
    correction_bolus = bg_delta / isf if isf > 0 else 0.0

    # Suggested dosage
    suggested_dose = meal_bolus + correction_bolus - current_iob

    # Guardrails: prevent negative dose and apply max dose clamp
    suggested_dose = max(0.0, suggested_dose)

    is_hypo = current_glucose < 3.9
    is_severe_hypo = current_glucose < 3.0
    # Low fat/protein relative to the carbs present signals rapid absorption (soda, juice,
    # candy) — reuses the same macros already driving the FPU calc below, no new AI call needed.
    is_fast_acting = carbs > 0 and (protein + fat) < 5.0
    hypo_carb_target = calculate_hypo_carb_target(weight, is_severe_hypo)

    # Safety: If blood glucose is dangerously low (hypoglycemia range < 3.9 mmol/L), force suggested dose to 0
    if is_hypo:
        suggested_dose = 0.0

    suggested_dose = min(suggested_dose, MAX_SINGLE_DOSE)

    # FPU calculations
    fpu_data = calculate_fpu(protein, fat, icr)

    # Situational timing hint (Option A - Deterministic logic)
    if is_hypo:
        if is_severe_hypo:
            # Below 3.0 mmol/L: skip carb-counting nuance entirely — speed and certainty matter
            # more than precision when this low. Flat dose regardless of what's already logged.
            timing_hint = (
                f"🆘 <b>Критично низький рівень цукру!</b> Негайно прийміть {hypo_carb_target:g}г швидких вуглеводів "
                f"(сік, цукор, глюкозні таблетки). Якщо не можете ковтати — покличте когось на допомогу, потрібен "
                f"глюкагон. Зачекайте 15 хвилин і перевірте цукор знову. Інсулін НЕ вводьте!"
            )
        # Food-aware: if what's being logged IS the fast-carb hypo treatment, say so instead
        # of generic "go get Xg of carbs" boilerplate that ignores what's already in hand.
        elif is_fast_acting and carbs >= hypo_carb_target:
            timing_hint = (
                f"🚨 <b>Низький рівень цукру!</b> Ці {carbs}г швидких вуглеводів (без істотного вмісту жирів/білків) "
                f"вже покривають потрібну дозу для корекції гіпоглікемії ({hypo_carb_target:g}г) — окремо шукати "
                f"вуглеводи не потрібно. Зачекайте 15 хвилин і повторно перевірте цукор. Інсулін зараз не вводьте."
            )
        elif is_fast_acting:
            missing = round(hypo_carb_target - carbs, 1)
            timing_hint = (
                f"🚨 <b>Низький рівень цукру!</b> {carbs}г швидких вуглеводів — цього недостатньо для повної корекції "
                f"(потрібно ~{hypo_carb_target:g}г). Додайте ще ~{missing}г швидких вуглеводів, зачекайте 15 хвилин "
                f"і перевірте цукор знову. Інсулін зараз не вводьте."
            )
        elif carbs <= 0:
            # No food logged at all (e.g. a plain glucose check) — nothing to reference, fall
            # back to the original generic hypo-treatment instruction.
            timing_hint = (
                f"🚨 <b>Низький рівень цукру!</b> Спочатку купіруйте гіпоглікемію ({hypo_carb_target:g}г швидких "
                f"вуглеводів, наприклад, сік або цукор) і зачекайте 15 хвилин. НЕ вводьте інсулін!"
            )
        else:
            timing_hint = (
                f"🚨 <b>Низький рівень цукру!</b> Ця страва містить жири/білки і засвоюється повільно — вона НЕ "
                f"підходить для швидкої корекції гіпоглікемії. Спочатку прийміть {hypo_carb_target:g}г ШВИДКИХ "
                f"вуглеводів (сік, цукор, мед), зачекайте 15 хвилин, перевірте цукор — і лише потім їжте цю страву. "
                f"Інсулін зараз не вводьте."
            )
    elif suggested_dose <= 0.0:
        if current_glucose > target_bg:
            timing_hint = f"⚠️ <b>Цукор підвищений, але у вашому тілі ще діє активний інсулін (IOB: {current_iob} ОД).</b> Додаткове введення інсуліну не потрібне (0.0 ОД) для запобігання гіпоглікемії. Зачекайте, поки активний інсулін знизить цукор."
        else:
            timing_hint = "✅ <b>Цукор в нормі.</b> Додатковий інсулін не потрібен (0.0 ОД). Можна приймати їжу."
    else:
        prebolus_min = calculate_prebolus_minutes(insulin_type, protein, fat, bg_delta, current_glucose)
        if bg_delta > 2.0:
            lead_in = "Цукор підвищений."
        elif bg_delta < -1.0:
            lead_in = "Цукор трохи знижений."
        else:
            lead_in = "Цукор в нормі."

        if prebolus_min > 0:
            timing_hint = f"💉 <b>{lead_in}</b> Введіть рекомендовану дозу інсуліну ({suggested_dose} ОД) і зачекайте {prebolus_min} хв. перед їжею."
        elif prebolus_min == 0:
            timing_hint = f"💉 <b>{lead_in}</b> Введіть рекомендовану дозу інсуліну ({suggested_dose} ОД) прямо перед початком прийому їжі."
        else:
            timing_hint = f"💉 <b>{lead_in}</b> Введіть рекомендовану дозу інсуліну ({suggested_dose} ОД) під час їжі або одразу після початку прийому їжі (приблизно через {abs(prebolus_min)} хв. після початку)."

    # Skip the delayed-extra-dose note during hypo — telling the user to plan a future insulin
    # dose right after telling them to withhold insulin entirely would be contradictory.
    if not is_hypo and fpu_data["fpu"] > 0 and fpu_data["dose"] > 0:
        timing_hint += f"\n\n🥩 <b>Виявлено білки/жири:</b> рекомендовано ввести додатково <b>{fpu_data['dose']} ОД швидкого інсуліну</b> через <b>{fpu_data['delay_hours']} години</b> після їжі для компенсації білків та жирів."

    disclaimer = (
        "⚠️ <i>Дисклеймер: Розрахунок є орієнтовним. Завжди зважайте на своє самопочуття "
        "та за потреби консультуйтеся з лікарем.</i>"
    )

    return {
        "iob": round(current_iob, 2),
        "meal_bolus": round(meal_bolus, 2),
        "correction_bolus": round(correction_bolus, 2),
        "total_suggested_dose": round(suggested_dose, 2),
        "fpu": fpu_data,
        "timing_hint": timing_hint,
        "disclaimer": disclaimer
    }
