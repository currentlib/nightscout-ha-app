"""Sync, alert and insulin-curve behaviour, against an in-memory stand-in for Nightscout."""
import asyncio
import datetime
import json

import pytest
from sqlalchemy import delete, select

from app import bootstrap
from app.database import InsulinInjection, LogEntry, User, async_session_local, init_db
from app.services import alerts, calculations, nightscout, nightscout_sync as sync
from app.services.iob_engine import calculate_cob, carbs_active_at, insulin_remaining, predict_glucose_timeline

NOW = datetime.datetime(2026, 11, 2, 12, 0, 0)


class FakeNightscout:
    def __init__(self):
        self.treatments = {}
        self.profiles = []

    async def create_treatment(self, treatment):
        id_ = f"ns{len(self.treatments)}-{treatment['created_at']}"
        self.treatments[id_] = {**treatment, "_id": id_}
        return id_

    async def treatments_since(self, moment):
        return list(self.treatments.values())

    async def latest_profile(self):
        return self.profiles[-1] if self.profiles else None

    async def create_profile(self, profile):
        self.profiles.append(profile)


@pytest.fixture
def ns(monkeypatch):
    fake = FakeNightscout()
    for name in ("create_treatment", "treatments_since", "latest_profile", "create_profile"):
        monkeypatch.setattr(nightscout, name, getattr(fake, name))

    async def reset():
        await init_db()
        async with async_session_local() as session:
            for model in (LogEntry, InsulinInjection, User):
                await session.execute(delete(model))
            session.add(User(telegram_id=1, clinical_setup_complete=True))
            session.add(User(telegram_id=2))
            await session.commit()

    asyncio.run(reset())
    return fake


def run(coroutine):
    return asyncio.run(coroutine)


async def add(*rows):
    async with async_session_local() as session:
        session.add_all(rows)
        await session.commit()


async def rows(model):
    async with async_session_local() as session:
        return (await session.execute(select(model))).scalars().all()


def test_history_is_pushed_once_and_only_for_the_patient(ns):
    run(add(
        LogEntry(telegram_id=1, carbs=40.0, meal_description="Борщ", timestamp=NOW),
        LogEntry(telegram_id=1, glucose_level=5.8, timestamp=NOW),
        LogEntry(telegram_id=2, carbs=99.0, timestamp=NOW),
        InsulinInjection(telegram_id=1, dosage=4.0, injection_type="bolus", timestamp=NOW),
    ))
    run(sync.push_treatments())
    run(sync.push_treatments())
    events = sorted(t["eventType"] for t in ns.treatments.values())
    assert events == ["BG Check", "Correction Bolus", "Meal Bolus"]
    meal = next(t for t in ns.treatments.values() if t["eventType"] == "Meal Bolus")
    assert meal["carbs"] == 40.0 and meal["notes"] == "Борщ" and meal["created_at"].endswith("Z")


def test_long_acting_insulin_never_reaches_nightscout_as_insulin(ns):
    run(add(InsulinInjection(telegram_id=1, dosage=20.0, injection_type="basal", timestamp=NOW)))
    run(sync.push_treatments())
    treatment = next(iter(ns.treatments.values()))
    assert "insulin" not in treatment and treatment["eventType"] == "Note"
    run(sync.pull_treatments(NOW))  # and it survives coming back
    assert (run(rows(InsulinInjection)))[0].dosage == 20.0


def test_treatment_from_nightscout_is_added_once(ns):
    ns.treatments["a"] = {
        "_id": "a", "eventType": "Meal Bolus", "created_at": "2026-11-02T11:00:00.000Z",
        "carbs": 30, "insulin": 3, "enteredBy": "careportal",
    }
    run(sync.pull_treatments(NOW))
    run(sync.pull_treatments(NOW))
    logs, injections = run(rows(LogEntry)), run(rows(InsulinInjection))
    assert [row.carbs for row in logs] == [30]
    assert [row.dosage for row in injections] == [3]


def test_bot_treatment_is_not_duplicated_on_the_way_back(ns):
    run(add(LogEntry(telegram_id=1, carbs=40.0, timestamp=NOW)))
    run(sync.push_treatments())
    run(sync.pull_treatments(NOW))
    assert len(run(rows(LogEntry))) == 1


def test_edit_and_delete_in_nightscout_reach_the_bot(ns):
    run(add(
        LogEntry(telegram_id=1, carbs=40.0, timestamp=NOW),
        InsulinInjection(telegram_id=1, dosage=4.0, injection_type="bolus", timestamp=NOW),
    ))
    run(sync.push_treatments())
    meal = next(t for t in ns.treatments.values() if t.get("carbs"))
    meal["carbs"] = 25
    del ns.treatments[next(t["_id"] for t in ns.treatments.values() if t.get("insulin"))]
    run(sync.pull_treatments(NOW))
    assert [row.carbs for row in run(rows(LogEntry))] == [25]
    assert run(rows(InsulinInjection)) == []


def test_zero_carb_meal_survives_nightscout_dropping_the_field(ns):
    run(add(LogEntry(telegram_id=1, carbs=0.0, meal_description="Кава", timestamp=NOW)))
    run(sync.push_treatments())
    next(iter(ns.treatments.values())).pop("carbs")
    run(sync.pull_treatments(NOW))
    assert [row.carbs for row in run(rows(LogEntry))] == [0.0]


def test_empty_nightscout_does_not_wipe_the_bot(ns):
    run(add(*(LogEntry(telegram_id=1, carbs=float(n), ns_id=f"x{n}", timestamp=NOW) for n in range(3))))
    run(sync.pull_treatments(NOW))
    assert len(run(rows(LogEntry))) == 3


def test_old_rows_outside_the_window_are_left_alone(ns):
    old = NOW - sync.WINDOW - datetime.timedelta(days=1)
    run(add(LogEntry(telegram_id=1, carbs=1.0, ns_id="old", timestamp=old)))
    run(sync.pull_treatments(NOW))
    assert len(run(rows(LogEntry))) == 1


async def patient():
    async with async_session_local() as session:
        return (await session.execute(select(User).filter(User.telegram_id == 1))).scalars().first()


def test_profile_goes_to_nightscout_then_comes_back_when_edited_there(ns):
    run(sync.sync_profile(NOW))
    assert len(ns.profiles) == 1
    run(sync.sync_profile(NOW))
    assert len(ns.profiles) == 1  # no change, no new profile

    store = ns.profiles[-1]["store"]["Default"]
    store["carbratio"] = [{"time": "00:00", "value": 8, "timeAsSeconds": 0}]
    store["dia"] = 5
    run(sync.sync_profile(NOW))
    user = run(patient())
    assert user.icr == 8 and user.dia_hours == 5
    assert len(ns.profiles) == 1


def test_mg_profile_is_converted():
    now = datetime.datetime(2026, 1, 1, 12)
    flat = lambda value: [{"time": "00:00", "value": value, "timeAsSeconds": 0}]
    profile = {"defaultProfile": "D", "units": "mg/dl", "store": {"D": {
        "dia": 4, "carbratio": flat(10), "sens": flat(54), "target_low": flat(90), "target_high": flat(110),
    }}}
    assert sync.read_profile(profile, now) == {"icr": 10, "isf": 3.0, "target_bg": 5.6, "dia_hours": 4.0}


def test_schedule_uses_the_value_active_now():
    schedule = [{"value": 1, "timeAsSeconds": 0}, {"value": 2, "timeAsSeconds": 21600}, {"value": 3, "timeAsSeconds": "43200"}]
    assert sync._active_value(schedule, datetime.datetime(2026, 1, 1, 5, 59)) == 1
    assert sync._active_value(schedule, datetime.datetime(2026, 1, 1, 6, 0)) == 2
    assert sync._active_value(schedule, datetime.datetime(2026, 1, 1, 23, 0)) == 3


def test_iob_curve_matches_nightscout():
    # Values computed with lib/plugins/iob.js for 4 U and a 4 hour DIA.
    for minutes, expected in ((30, 3.82), (60, 3.33), (120, 1.62), (180, 0.38)):
        assert round(4 * insulin_remaining(minutes / 60, 4), 2) == expected
    assert insulin_remaining(-0.1, 4) == 0
    assert insulin_remaining(4.1, 4) == 0


def test_newest_glucose_prefers_the_later_reading(monkeypatch):
    monkeypatch.setattr(nightscout, "latest", nightscout.Reading(6.0, NOW))
    later = NOW + datetime.timedelta(minutes=1)
    earlier = NOW - datetime.timedelta(minutes=1)
    assert nightscout.newest_glucose(1, 5.0, earlier) == (6.0, NOW)
    assert nightscout.newest_glucose(1, 5.0, later) == (5.0, later)
    assert nightscout.newest_glucose(1, None, None) == (6.0, NOW)
    assert nightscout.newest_glucose(2, 5.0, earlier) == (5.0, earlier)  # not the patient
    assert nightscout.newest_glucose(2, None, None) == (None, None)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def fresh_snapshot(iob=1.0, cob=10.0):
    return nightscout.Calculated(iob, cob, utc_now())


def dose(units, minutes_ago, ns_id=None):
    return InsulinInjection(
        dosage=units, injection_type="bolus", ns_id=ns_id,
        timestamp=utc_now() - datetime.timedelta(minutes=minutes_ago),
    )


def meal(carbs, minutes_ago, ns_id="synced"):
    return LogEntry(carbs=carbs, ns_id=ns_id, timestamp=utc_now() - datetime.timedelta(minutes=minutes_ago))


def test_nightscout_iob_is_used_when_available(monkeypatch):
    monkeypatch.setattr(nightscout, "calculated", fresh_snapshot(iob=2.5))
    assert calculations.insulin_on_board(1, [], 4.0) == 2.5


def test_dose_not_yet_in_nightscout_still_counts(monkeypatch):
    monkeypatch.setattr(nightscout, "calculated", fresh_snapshot(iob=0.0))
    assert calculations.insulin_on_board(1, [dose(6.0, 0)], 4.0) > 5.9


def test_stale_nightscout_falls_back_to_the_bot(monkeypatch):
    old = NOW - datetime.timedelta(days=1)
    monkeypatch.setattr(nightscout, "calculated", nightscout.Calculated(9.0, 9.0, old))
    assert calculations.using_nightscout(1) is False
    assert calculations.insulin_on_board(1, [], 4.0) == 0
    assert calculations.carbs_on_board(1, []) == 0


def test_other_people_never_get_the_patients_numbers(monkeypatch):
    monkeypatch.setattr(nightscout, "calculated", fresh_snapshot(iob=9.0, cob=99.0))
    assert calculations.insulin_on_board(2, [], 4.0) == 0
    assert calculations.carbs_on_board(2, []) == 0


def test_nightscout_cob_adds_only_unsynced_meals(monkeypatch):
    monkeypatch.setattr(nightscout, "calculated", fresh_snapshot(cob=10.0))
    assert calculations.carbs_on_board(1, [meal(40, 5, ns_id="synced")]) == 10.0
    assert calculations.carbs_on_board(1, [meal(40, 5, ns_id=None)]) == 50.0


def test_cob_without_a_nightscout_profile_falls_back(monkeypatch):
    monkeypatch.setattr(nightscout, "calculated", fresh_snapshot(cob=None))
    assert calculations.carbs_on_board(1, [meal(40, 5)]) == 40.0


def test_carbs_wait_then_absorb_at_a_fixed_rate():
    assert calculate_cob([meal(40, 10)]) == 40  # still inside the 20 minute delay
    assert 29.9 < calculate_cob([meal(40, 50)]) < 30.1  # 30 minutes of absorbing at 20 g/h
    assert calculate_cob([meal(40, 141)]) == 0  # 20 minutes + 2 hours


def test_overlapping_meals_queue():
    moment = NOW.replace(tzinfo=datetime.timezone.utc)
    meals = [
        LogEntry(carbs=40, timestamp=NOW - datetime.timedelta(minutes=60)),
        LogEntry(carbs=20, timestamp=NOW - datetime.timedelta(minutes=30)),
    ]
    # The first ends 80 minutes from now, leaving 26.7 g. The second queues behind it and
    # ends at 140, so its whole 20 g is still left.
    assert round(carbs_active_at(meals, moment, 20), 1) == round(26.7 + 20, 1)


def test_prediction_rises_after_a_meal_and_falls_after_a_dose():
    rising = predict_glucose_timeline(6.0, [], [meal(40, 30)], isf=3, icr=10, dia_hours=4)
    falling = predict_glucose_timeline(6.0, [dose(3, 5)], [], isf=3, icr=10, dia_hours=4)
    assert rising[4]["glucose"] > 6.0
    assert falling[4]["glucose"] < 6.0


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def send_message(chat_id, text):
        messages.append((chat_id, text))

    monkeypatch.setattr(alerts.bot, "send_message", send_message)
    monkeypatch.setattr(alerts, "_level", None)
    monkeypatch.setattr(alerts, "_level_alerted_at", None)
    monkeypatch.setattr(alerts, "_no_data_alerted_at", None)
    monkeypatch.setattr(alerts.settings, "ALERT_RECIPIENTS", [
        {"chat_id": 10, "urgent_low": True, "low": True, "high": False, "predicted": False, "no_data": False},
        {"chat_id": 20, "urgent_low": True, "low": False, "high": True, "predicted": False, "no_data": True},
    ])
    return messages


def reading(mmol):
    return nightscout.Reading(mmol, NOW)


def minutes(n):
    return NOW + datetime.timedelta(minutes=n)


def test_alert_goes_only_to_recipients_of_that_kind(sent):
    run(alerts.on_new_reading(reading(3.5), NOW))
    assert [chat for chat, _ in sent] == [10]
    sent.clear()
    run(alerts.on_new_reading(reading(15.0), minutes(5)))
    assert [chat for chat, _ in sent] == [20]


def test_alert_repeats_on_schedule_not_on_every_reading(sent):
    run(alerts.on_new_reading(reading(3.5), NOW))
    run(alerts.on_new_reading(reading(3.5), minutes(5)))
    assert len(sent) == 1
    run(alerts.on_new_reading(reading(3.5), minutes(30)))
    assert len(sent) == 2


def test_urgent_low_alerts_immediately_and_repeats_sooner(sent):
    run(alerts.on_new_reading(reading(3.5), NOW))
    run(alerts.on_new_reading(reading(3.0), minutes(1)))
    assert len(sent) == 1 + 2  # both recipients want urgent lows
    run(alerts.on_new_reading(reading(3.0), minutes(11)))
    assert len(sent) == 3 + 2


def test_recovery_is_announced_once(sent):
    run(alerts.on_new_reading(reading(3.5), NOW))
    sent.clear()
    run(alerts.on_new_reading(reading(5.5), minutes(5)))
    run(alerts.on_new_reading(reading(5.6), minutes(10)))
    assert len(sent) == 1 and "норм" in sent[0][1]


def test_no_data_alert_fires_once_then_clears(sent):
    run(alerts.check_no_data(reading(6.0), minutes(29)))
    assert sent == []
    run(alerts.check_no_data(reading(6.0), minutes(31)))
    run(alerts.check_no_data(reading(6.0), minutes(32)))
    assert [chat for chat, _ in sent] == [20]
    sent.clear()
    run(alerts.check_no_data(nightscout.Reading(6.0, minutes(33)), minutes(33)))
    assert len(sent) == 1


def test_failed_recipient_does_not_stop_the_others(sent, monkeypatch):
    async def send_message(chat_id, text):
        if chat_id == 10:
            raise RuntimeError("blocked")
        sent.append((chat_id, text))

    monkeypatch.setattr(alerts.bot, "send_message", send_message)
    run(alerts.on_new_reading(reading(2.5), NOW))
    assert [chat for chat, _ in sent] == [20]


def test_options_become_settings():
    env = bootstrap.environment({
        "bot_token": "t", "patient_telegram_id": 5, "telegram_user_ids": [6], "ai_model_heavy": "m",
        "alert_recipients": [{"chat_id": 7}], "alert_low": 3.9,
    })
    assert env["BOT_TOKEN"] == "t" and env["ALERT_LOW"] == "3.9"
    assert json.loads(env["TELEGRAM_WHITELIST_USER_IDS"]) == [5, 6]
    assert json.loads(env["ALERT_RECIPIENTS"]) == [{"chat_id": 7}]
    assert env["MODEL_UNLIMITED_HEAVY"] == "m"
    assert "DASHBOARD_URL" not in env
