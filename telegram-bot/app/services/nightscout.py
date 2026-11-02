"""Thin client for the patient's Nightscout, plus the latest CGM reading."""
import datetime
import hashlib
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import aiohttp

from app.config import settings

MG_PER_MMOL = 18.01559  # Nightscout's MMOL_TO_MGDL
TIMEOUT = aiohttp.ClientTimeout(total=20)


@dataclass
class Reading:
    mmol: float
    at: datetime.datetime  # naive UTC, like every timestamp in the bot's database
    arrow: str = ""


@dataclass
class Calculated:
    iob: float
    cob: float | None  # None until Nightscout has a profile
    fetched: datetime.datetime  # naive UTC


# Updated by nightscout_sync once per cycle, read by status, bolus and alert code.
latest: Reading | None = None
calculated: Calculated | None = None
carbs_per_hour: float = 20.0

# The sync runs every 30 seconds, so this means several cycles failed in a row.
CALCULATED_FRESH_FOR = datetime.timedelta(minutes=3)

ARROWS = {
    "DoubleUp": "⇈", "SingleUp": "↑", "FortyFiveUp": "↗", "Flat": "→",
    "FortyFiveDown": "↘", "SingleDown": "↓", "DoubleDown": "⇊",
}


def enabled() -> bool:
    return bool(settings.NIGHTSCOUT_URL and settings.NIGHTSCOUT_TOKEN and settings.PATIENT_TELEGRAM_ID)


def naive_utc(milliseconds: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(milliseconds / 1000, datetime.timezone.utc).replace(tzinfo=None)


def to_iso(moment: datetime.datetime) -> str:
    """Bot timestamps are naive UTC. Nightscout wants them explicit."""
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def local_now(now_utc: datetime.datetime) -> datetime.datetime:
    return now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(settings.TIMEZONE))


def parse_iso(text: str) -> datetime.datetime:
    moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def fresh_calculated() -> Calculated | None:
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if calculated and now - calculated.fetched < CALCULATED_FRESH_FOR:
        return calculated
    return None


def reading_from(entry: dict) -> Reading:
    return Reading(
        mmol=round(entry["sgv"] / MG_PER_MMOL, 1),
        at=naive_utc(entry["date"]),
        arrow=ARROWS.get(entry.get("direction"), ""),
    )


def newest_glucose(telegram_id: int, manual_value: float | None, manual_at: datetime.datetime | None):
    """The most recent of a manual reading and the CGM, as (mmol, naive UTC time).

    Returns (None, None) when there's neither. Only the patient has a CGM.
    """
    cgm = latest if telegram_id == settings.PATIENT_TELEGRAM_ID else None
    manual_at = manual_at.replace(tzinfo=None) if manual_at else None
    if cgm and (manual_at is None or cgm.at >= manual_at):
        return cgm.mmol, cgm.at
    return manual_value, manual_at


async def _request(method: str, path: str, **kwargs):
    # A hashed access token and a hashed API secret go in the same header, so
    # NIGHTSCOUT_TOKEN can be either.
    headers = {"api-secret": hashlib.sha1(settings.NIGHTSCOUT_TOKEN.encode()).hexdigest()}
    async with aiohttp.ClientSession(timeout=TIMEOUT, headers=headers) as session:
        async with session.request(method, settings.NIGHTSCOUT_URL.rstrip("/") + path, **kwargs) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


async def latest_entry() -> dict | None:
    entries = await _request("GET", "/api/v1/entries/sgv.json", params={"count": "1"})
    return entries[0] if entries else None


async def calculations() -> dict:
    return await _request("GET", "/api/v2/properties/iob,cob")


async def create_treatment(treatment: dict) -> str:
    created = await _request("POST", "/api/v1/treatments.json", json=treatment)
    return created[0]["_id"]


async def treatments_since(moment: datetime.datetime) -> list[dict]:
    return await _request(
        "GET", "/api/v1/treatments.json",
        params={"count": "2000", "find[created_at][$gte]": to_iso(moment)},
    )


async def latest_profile() -> dict | None:
    profiles = await _request("GET", "/api/v1/profile.json", params={"count": "1"})
    return profiles[0] if profiles else None


async def create_profile(profile: dict) -> None:
    await _request("POST", "/api/v1/profile.json", json=profile)
