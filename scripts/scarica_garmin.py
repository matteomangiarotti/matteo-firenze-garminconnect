#!/usr/bin/env python3
"""
scarica_garmin.py
-----------------
Scarica le attività di corsa da Garmin Connect usando cookie di sessione
e aggiorna data/activities.json.

Variabili d'ambiente richieste (GitHub Secrets):
  GARMIN_JWT_FGP    – cookie JWT_FGP da connect.garmin.com
  GARMIN_SSO_GUID   – cookie GARMIN-SSO-CUST-GUID da connect.garmin.com
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Configurazione ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_ROOT   = Path(__file__).resolve().parent.parent
DATA_DIR    = REPO_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "activities.json"

FETCH_LIMIT  = 20
RUNNING_TYPES = {"running", "trail_running", "treadmill_running"}

BASE_URL = "https://connectapi.garmin.com"
ACTIVITY_LIST_URL = f"{BASE_URL}/activity-service/activity/search/activities"
ACTIVITY_LAPS_URL = f"{BASE_URL}/activity-service/activity/{{act_id}}/splits"

# ── Helper ──────────────────────────────────────────────────────────────────

def get_session(jwt_fgp: str, sso_guid: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "GCM-iOS-5.7.2.1 (com.garmin.connect.mobile; build:5.7.2.1; iOS 17.0) Alamofire/5.7.1",
        "NK": "NT",
        "X-app-ver": "4.70.1.0",
        "X-lang": "it-IT",
    })
    session.cookies.set("JWT_FGP", jwt_fgp, domain=".garmin.com")
    session.cookies.set("GARMIN-SSO-CUST-GUID", sso_guid, domain=".garmin.com")
    return session


def fetch_activities(session: requests.Session) -> list:
    params = {
        "start": 0,
        "limit": FETCH_LIMIT,
        "activityType": "running",
    }
    r = session.get(ACTIVITY_LIST_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_laps(session: requests.Session, act_id: int) -> list:
    url = ACTIVITY_LAPS_URL.format(act_id=act_id)
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("lapDTOs", [])
    except Exception as e:
        log.warning("Lap non disponibili per %s: %s", act_id, e)
        return []


def load_existing() -> dict:
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": None, "activities": []}


def existing_ids(data: dict) -> set:
    return {a["garmin_id"] for a in data.get("activities", [])}


def fmt_pace(seconds_per_km: float) -> str:
    if not seconds_per_km or seconds_per_km <= 0:
        return "—"
    m = int(seconds_per_km // 60)
    s = int(seconds_per_km % 60)
    return f"{m}:{s:02d}"


def activity_date(act: dict) -> str:
    raw = act.get("startTimeLocal") or act.get("startTimeGMT", "")
    return raw[:10] if raw else ""


def parse_laps(laps_raw: list) -> list:
    parsed = []
    for i, lap in enumerate(laps_raw or [], start=1):
        dist   = lap.get("distance") or 0
        dur    = lap.get("duration") or lap.get("elapsedDuration") or 0
        avg_hr = lap.get("averageHR") or lap.get("averageHeartRate")
        max_hr = lap.get("maxHR") or lap.get("maxHeartRate")
        pace   = (dur / (dist / 1000)) if dist > 50 else None
        parsed.append({
            "lap_index":     i,
            "distance_m":    round(dist),
            "duration_s":    round(dur),
            "avg_pace_s_km": round(pace) if pace else None,
            "avg_pace_fmt":  fmt_pace(pace) if pace else "—",
            "avg_hr":        round(avg_hr) if avg_hr else None,
            "max_hr":        round(max_hr) if max_hr else None,
        })
    return parsed


def build_activity(act: dict, laps_raw: list) -> dict:
    dist  = act.get("distance") or 0
    dur   = act.get("duration") or act.get("movingDuration") or 0
    avg_hr = act.get("averageHR") or act.get("averageHeartRate")
    max_hr = act.get("maxHR") or act.get("maxHeartRate")
    pace  = (dur / (dist / 1000)) if dist > 50 else None
    return {
        "garmin_id":     act.get("activityId"),
        "date":          activity_date(act),
        "start_time":    act.get("startTimeLocal", "")[:19],
        "name":          act.get("activityName", "Corsa"),
        "type":          act.get("activityType", {}).get("typeKey", "running"),
        "distance_m":    round(dist),
        "distance_km":   round(dist / 1000, 2),
        "duration_s":    round(dur),
        "avg_hr":        round(avg_hr) if avg_hr else None,
        "max_hr":        round(max_hr) if max_hr else None,
        "avg_pace_s_km": round(pace) if pace else None,
        "avg_pace_fmt":  fmt_pace(pace) if pace else "—",
        "calories":      act.get("calories"),
        "elevation_gain": act.get("elevationGain"),
        "laps":          parse_laps(laps_raw),
        "fetched_at":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    jwt_fgp  = os.environ.get("GARMIN_JWT_FGP", "").strip()
    sso_guid = os.environ.get("GARMIN_SSO_GUID", "").strip()

    if not jwt_fgp or not sso_guid:
        raise SystemExit("❌  Imposta GARMIN_JWT_FGP e GARMIN_SSO_GUID come secrets")

    DATA_DIR.mkdir(exist_ok=True)
    data     = load_existing()
    seen_ids = existing_ids(data)
    log.info("Attività già salvate: %d", len(seen_ids))

    log.info("Connessione a Garmin Connect...")
    session = get_session(jwt_fgp, sso_guid)

    log.info("Scarico le ultime %d attività...", FETCH_LIMIT)
    try:
        activities = fetch_activities(session)
    except requests.HTTPError as e:
        raise SystemExit(f"❌  Errore API Garmin: {e}")

    if not isinstance(activities, list):
        raise SystemExit(f"❌  Risposta inattesa da Garmin: {activities}")

    new_acts = [
        a for a in activities
        if a.get("activityType", {}).get("typeKey", "") in RUNNING_TYPES
        and a.get("activityId") not in seen_ids
    ]
    log.info("Nuove attività running da processare: %d", len(new_acts))

    if not new_acts:
        log.info("Nessuna novità — aggiorno solo il timestamp.")
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    added = 0
    for act in new_acts:
        act_id   = act.get("activityId")
        act_date = activity_date(act)
        log.info("  Processo %s (%s)...", act_id, act_date)
        laps_raw = fetch_laps(session, act_id)
        record   = build_activity(act, laps_raw)
        data["activities"].append(record)
        added += 1
        time.sleep(1)

    data["activities"].sort(key=lambda x: x.get("start_time", ""), reverse=True)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("✅  Aggiunte %d nuove attività → %s", added, OUTPUT_FILE)


if __name__ == "__main__":
    main()
