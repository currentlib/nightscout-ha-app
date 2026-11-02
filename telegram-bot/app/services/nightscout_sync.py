"""Two-way sync between the bot's database and the patient's Nightscout.

Nothing here runs inside a handler, so logging in the bot keeps working when Nightscout is
down: a row with no ns_id just waits for a later cycle. That also makes the first run the
history import, since every existing row starts without an ns_id.

Treatments created by the bot carry enteredBy=BOT and are never created again on the way
back. Rows are deleted or changed when Nightscout says so, and the bot has no edit or
delete of its own, so Nightscout always wins.
"""
import datetime
import logging

from sqlalchemy import select

from app.config import settings
from app.database import InsulinInjection, LogEntry, User, async_session_local
from app.services import alerts, nightscout

logger = logging.getLogger(__name__)

BOT = "telegram-bot"
WINDOW = datetime.timedelta(days=7)
# Rows this close to the edge of WINDOW may just have fallen out of the fetch, so an old
# row missing from the response isn't proof it was deleted.
DELETE_MARGIN = datetime.timedelta(hours=1)
PUSH_BATCH = 200
PROFILE_EVERY_CYCLES = 10
# If Nightscout suddenly reports nothing while we hold this many synced rows, assume its
# database was replaced rather than that someone deleted everything.
EMPTY_RESPONSE_GUARD = 3

_cycle = 0
_seen_reading_at = None


# --- Treatment mapping -----------------------------------------------------

def log_to_treatment(row: LogEntry) -> dict:
    treatment = {
        "created_at": nightscout.to_iso(row.timestamp),
        "enteredBy": BOT,
        "eventType": "BG Check" if row.carbs is None else "Meal Bolus",
    }
    if row.carbs is not None:
        treatment.update(carbs=row.carbs, protein=row.protein, fat=row.fat)
    if row.glucose_level is not None:
        treatment.update(glucose=row.glucose_level, glucoseType="Finger", units="mmol")
    if row.meal_description:
        treatment["notes"] = row.meal_description
    return treatment


def injection_to_treatment(row: InsulinInjection) -> dict:
    treatment = {"created_at": nightscout.to_iso(row.timestamp), "enteredBy": BOT}
    if row.injection_type == "bolus":
        return {**treatment, "eventType": "Correction Bolus", "insulin": row.dosage}
    # Long-acting insulin must not have an `insulin` field: Nightscout counts every one
    # of those as bolus insulin on board.
    return {**treatment, "eventType": "Note", "notes": f"Базальний інсулін: {row.dosage} ОД", "basal_units": row.dosage}


def treatment_glucose(treatment: dict) -> float | None:
    glucose = treatment.get("glucose")
    if glucose in (None, ""):
        return None
    glucose = float(glucose)
    return round(glucose / nightscout.MG_PER_MMOL, 1) if "mg" in str(treatment.get("units", "")).lower() else glucose


# --- Sync steps ------------------------------------------------------------

async def push_treatments() -> None:
    """Sends rows Nightscout doesn't have yet, oldest first. Includes the old history."""
    async with async_session_local() as session:
        for model, convert in ((LogEntry, log_to_treatment), (InsulinInjection, injection_to_treatment)):
            rows = (await session.execute(
                select(model)
                .filter(model.telegram_id == settings.PATIENT_TELEGRAM_ID, model.ns_id.is_(None))
                .order_by(model.timestamp.asc())
                .limit(PUSH_BATCH)
            )).scalars().all()
            for row in rows:
                row.ns_id = await nightscout.create_treatment(convert(row))
                await session.commit()


def _apply_log(row: LogEntry, treatment: dict) -> None:
    # Nightscout drops carbs of 0, so null can't be allowed to erase a zero-carb meal.
    if treatment.get("carbs") is not None or row.carbs:
        row.carbs = treatment.get("carbs")
    row.glucose_level = treatment_glucose(treatment)
    row.timestamp = nightscout.parse_iso(treatment["created_at"])


def _apply_injection(row: InsulinInjection, treatment: dict) -> None:
    dosage = treatment.get("insulin") or treatment.get("basal_units")
    if dosage:
        row.dosage = dosage
    row.timestamp = nightscout.parse_iso(treatment["created_at"])


async def pull_treatments(now: datetime.datetime) -> None:
    """Mirrors Nightscout's last WINDOW of treatments: new, changed and deleted."""
    since = now - WINDOW
    treatments = await nightscout.treatments_since(since)
    by_id = {t["_id"]: t for t in treatments}
    patient = settings.PATIENT_TELEGRAM_ID

    async with async_session_local() as session:
        logs = (await session.execute(select(LogEntry).filter(
            LogEntry.telegram_id == patient, LogEntry.ns_id.isnot(None), LogEntry.timestamp >= since,
        ))).scalars().all()
        injections = (await session.execute(select(InsulinInjection).filter(
            InsulinInjection.telegram_id == patient, InsulinInjection.ns_id.isnot(None),
            InsulinInjection.timestamp >= since,
        ))).scalars().all()

        if not treatments and len(logs) + len(injections) >= EMPTY_RESPONSE_GUARD:
            logger.warning("Nightscout returned no treatments, keeping local rows")
            return

        for rows, apply in ((logs, _apply_log), (injections, _apply_injection)):
            for row in rows:
                treatment = by_id.get(row.ns_id)
                if treatment:
                    apply(row, treatment)
                elif row.timestamp >= since + DELETE_MARGIN:
                    await session.delete(row)

        known_logs = {row.ns_id for row in logs}
        known_injections = {row.ns_id for row in injections}
        for treatment in treatments:
            if treatment.get("enteredBy") == BOT:
                continue
            id_ = treatment["_id"]
            when = nightscout.parse_iso(treatment["created_at"])
            if id_ not in known_logs and (treatment.get("carbs") or treatment_glucose(treatment) is not None):
                session.add(LogEntry(
                    telegram_id=patient, ns_id=id_, timestamp=when, carbs=treatment.get("carbs"),
                    protein=treatment.get("protein"), fat=treatment.get("fat"),
                    glucose_level=treatment_glucose(treatment), meal_description=treatment.get("notes"),
                ))
            if id_ not in known_injections and treatment.get("insulin"):
                session.add(InsulinInjection(
                    telegram_id=patient, ns_id=id_, timestamp=when, dosage=treatment["insulin"], injection_type="bolus",
                ))
        await session.commit()


def profile_values(user: User) -> str:
    return f"{user.icr}|{user.isf}|{user.target_bg}|{user.dia_hours}"


def _active_value(schedule: list[dict], now_local: datetime.datetime) -> float:
    """The value of a Nightscout time-of-day schedule at this moment."""
    seconds = now_local.hour * 3600 + now_local.minute * 60
    active = schedule[0]
    for item in schedule:
        if int(item.get("timeAsSeconds", 0)) <= seconds:
            active = item
    return float(active["value"])


def read_profile(profile: dict, now_local: datetime.datetime) -> dict:
    """ICR, ISF, target and DIA from a Nightscout profile, in mmol/L."""
    store = profile["store"][profile["defaultProfile"]]
    in_mg = "mg" in str(store.get("units") or profile.get("units", "")).lower()
    mmol = (lambda value: round(value / nightscout.MG_PER_MMOL, 1)) if in_mg else (lambda value: value)
    target_low = _active_value(store["target_low"], now_local)
    target_high = _active_value(store["target_high"], now_local)
    return {
        "icr": _active_value(store["carbratio"], now_local),
        "isf": mmol(_active_value(store["sens"], now_local)),
        "target_bg": mmol((target_low + target_high) / 2),
        "dia_hours": float(store["dia"]),
    }


def build_profile(user: User, previous: dict | None, now: datetime.datetime) -> dict:
    """A new Nightscout profile holding the bot's settings. Keeps anything else already there."""
    flat = lambda value: [{"time": "00:00", "value": value, "timeAsSeconds": 0}]
    previous = previous or {}
    name = previous.get("defaultProfile", "Default")
    store = dict((previous.get("store") or {}).get(name, {}))
    store.setdefault("basal", flat(0))
    store.setdefault("carbs_hr", 20)
    store.setdefault("delay", 20)
    store.setdefault("timezone", settings.TIMEZONE)
    store.update(
        dia=user.dia_hours, carbratio=flat(user.icr), sens=flat(user.isf),
        target_low=flat(user.target_bg), target_high=flat(user.target_bg), units="mmol",
    )
    return {
        "defaultProfile": name, "store": {name: store}, "units": "mmol",
        "startDate": nightscout.to_iso(now), "created_at": nightscout.to_iso(now),
    }


async def sync_profile(now: datetime.datetime) -> None:
    """Whichever side changed since the last sync wins. Nightscout wins a tie, since its
    Profile Editor is the only place to change these after onboarding."""
    remote = await nightscout.latest_profile()
    async with async_session_local() as session:
        user = (await session.execute(
            select(User).filter(User.telegram_id == settings.PATIENT_TELEGRAM_ID)
        )).scalars().first()
        if not user or not user.clinical_setup_complete:
            return
        values = None
        if remote and remote.get("store"):
            values = read_profile(remote, nightscout.local_now(now))
            incoming = f"{values['icr']}|{values['isf']}|{values['target_bg']}|{values['dia_hours']}"
        if values and incoming != user.ns_profile_synced:
            for field, value in values.items():
                setattr(user, field, value)
            user.ns_profile_synced = incoming
            logger.info("Took clinical settings from Nightscout")
        elif profile_values(user) != user.ns_profile_synced:
            await nightscout.create_profile(build_profile(user, remote, now))
            user.ns_profile_synced = profile_values(user)
            logger.info("Sent clinical settings to Nightscout")
        await session.commit()


async def poll_glucose(now: datetime.datetime) -> None:
    global _seen_reading_at
    entry = await nightscout.latest_entry()
    if not entry:
        return
    nightscout.latest = nightscout.reading_from(entry)
    if nightscout.latest.at == _seen_reading_at:
        return
    _seen_reading_at = nightscout.latest.at
    await alerts.on_new_reading(nightscout.latest, now)
    async with async_session_local() as session:
        await alerts.check_and_trigger_glucose_alert(settings.PATIENT_TELEGRAM_ID, session)
        await session.commit()


async def poll_calculations(now: datetime.datetime) -> None:
    properties = await nightscout.calculations()
    cob = properties.get("cob") or {}
    nightscout.carbs_per_hour = cob.get("carbs_hr") or nightscout.carbs_per_hour
    nightscout.calculated = nightscout.Calculated(properties["iob"]["iob"], cob.get("cob"), now)


async def sync_cycle() -> None:
    """One pass. Each step fails alone, so a problem pushing can't block a glucose alert."""
    if not nightscout.enabled():
        return
    global _cycle
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    steps = [poll_glucose(now)]
    if _cycle % PROFILE_EVERY_CYCLES == 0:
        steps.append(sync_profile(now))
    steps += [push_treatments(), pull_treatments(now), poll_calculations(now)]
    _cycle += 1
    for step in steps:
        try:
            await step
        except Exception as error:
            logger.warning(f"Nightscout sync step failed: {error!r}")
    await alerts.check_no_data(nightscout.latest, now)
