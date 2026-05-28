#!/usr/bin/env python3
"""
scarica_garmin.py
-----------------
Scarica le attività di corsa da Garmin Connect usando cookie di sessione
e aggiorna data/activities.json.

Variabili d'ambiente richieste (GitHub Secrets):
  GARMIN_SSO_GUID   – cookie GARMIN-SSO-CUST-GUID
  GARMIN_SSO_GUID2  – cookie GARMIN-SSO-GUID
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

REPO_ROOT   = Path(__file__).resolve().parent.parent
DATA_DIR    = REPO_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "activities.json"
FETCH_LIMIT = 20
RUNNING_TYPES = {"running", "trail_running", "treadmill_running"}

def get_session(cust_guid: str, sso_guid: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "NK": "NT",
        "X-App-Ver": "5.25.0.30a",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Referer": "https://connect.garmin.com/modern/activities",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://connect.garmin.com",
    })
    session.cookies.set("GARMIN-SSO", "1", domain=".garmin.com")
    session.cookies.set("GARMIN-SSO-CUST-GUID", cust_guid, domain=".garmin.com")
    session.cookies.set("GARMIN-SSO-GUID", sso_guid, domain=".garmin.com")
    return session

def fetch_activities(session: requests.Session) -> list:
    # Prova endpoint in ordine
    endpoints = [
        "https://connect.garmin.com/proxy/activitylist-service/activities/search/activities",
        "https://connect.garmin.com/activitylist-service/activities/search/activities",
        "https://connect.garmin.com/modern/proxy/activitylist-service/activities/search/activities",
    ]
    params = {"start": 0, "limit": FETCH_LIMIT}
    for url in endpoints:
        try:
            log.info("Provo: %s", url)
            r = session.get(url, params=params, timeout=30)
            log.info("  → Status %s | Content-Type: %s", r.status_code, r.headers.get("Content-Type",""))
            if r.status_code == 200:
                try:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data
                    elif isinstance(data, dict):
                        # Alcuni endpoint wrappano in un oggetto
                        for key in ["activityList", "activities", "data"]:
                            if key in data and isinstance(data[key], list):
                                return data[key]
                        log.warning("  → JSON vuoto o struttura inattesa: %s", str(data)[:200])
                    else:
                        log.warning("  → Lista vuota o formato inatteso")
                except Exception as e:
                    log.warning("  → Errore parsing JSON: %s | Body: %s", e, r.text[:200])
            else:
                log.warning("  → HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("  → Eccezione: %s", e)
    raise SystemExit("❌  Nessun endpoint funzionante")

def fetch_laps(session: requests.Session, act_id: int) -> list:
    endpoints = [
        f"https://connect.garmin.com/proxy/activity-service/activity/{act_id}/splits",
        f"https://connect.garmin.com/activity-service/activity/{act_id}/splits",
    ]
    for url in endpoints:
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json().get("lapDTOs", [])
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

def fmt_pace(spk):
    if not spk or spk <= 0: return "—"
    return f"{int(spk//60)}:{int(spk%60):02d}"

def activity_date(act):
    raw = act.get("startTimeLocal") or act.get("startTimeGMT", "")
    return raw[:10] if raw else ""

def parse_laps(laps_raw):
    parsed = []
    for i, lap in enumerate(laps_raw or [], start=1):
        dist = lap.get("distance") or 0
        dur  = lap.get("duration") or lap.get("elapsedDuration") or 0
        hr   = lap.get("averageHR") or lap.get("averageHeartRate")
        pace = (dur / (dist / 1000)) if dist > 50 else None
        parsed.append({
            "lap_index":     i,
            "distance_m":    round(dist),
            "duration_s":    round(dur),
            "avg_pace_s_km": round(pace) if pace else None,
            "avg_pace_fmt":  fmt_pace(pace),
            "avg_hr":        round(hr) if hr else None,
        })
    return parsed

def build_activity(act, laps_raw):
    dist  = act.get("distance") or 0
    dur   = act.get("duration") or act.get("movingDuration") or 0
    hr    = act.get("averageHR") or act.get("averageHeartRate")
    pace  = (dur / (dist / 1000)) if dist > 50 else None
    return {
        "garmin_id":      act.get("activityId"),
        "date":           activity_date(act),
        "start_time":     act.get("startTimeLocal", "")[:19],
        "name":           act.get("activityName", "Corsa"),
        "type":           act.get("activityType", {}).get("typeKey", "running"),
        "distance_m":     round(dist),
        "distance_km":    round(dist / 1000, 2),
        "duration_s":     round(dur),
        "avg_hr":         round(hr) if hr else None,
        "avg_pace_s_km":  round(pace) if pace else None,
        "avg_pace_fmt":   fmt_pace(pace),
        "calories":       act.get("calories"),
        "elevation_gain": act.get("elevationGain"),
        "laps":           parse_laps(laps_raw),
        "fetched_at":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

def main():
    cust_guid = os.environ.get("GARMIN_SSO_GUID", "").strip()
    sso_guid  = os.environ.get("GARMIN_SSO_GUID2", "").strip()

    if not cust_guid or not sso_guid:
        raise SystemExit("❌  Imposta GARMIN_SSO_GUID e GARMIN_SSO_GUID2 come secrets")

    DATA_DIR.mkdir(exist_ok=True)
    data     = load_existing()
    seen_ids = existing_ids(data)
    log.info("Attività già salvate: %d", len(seen_ids))

    session = get_session(cust_guid, sso_guid)
    log.info("Scarico le ultime %d attività...", FETCH_LIMIT)
    activities = fetch_activities(session)
    log.info("Attività ricevute: %d", len(activities))

    new_acts = [
        a for a in activities
        if a.get("activityType", {}).get("typeKey", "") in RUNNING_TYPES
        and a.get("activityId") not in seen_ids
    ]
    log.info("Nuove attività running: %d", len(new_acts))

    if not new_acts:
        log.info("Nessuna novità — aggiorno timestamp.")
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    for act in new_acts:
        act_id = act.get("activityId")
        log.info("  Processo %s (%s)...", act_id, activity_date(act))
        laps_raw = fetch_laps(session, act_id)
        data["activities"].append(build_activity(act, laps_raw))
        time.sleep(1)

    data["activities"].sort(key=lambda x: x.get("start_time", ""), reverse=True)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("✅  Aggiunte %d nuove attività → %s", len(new_acts), OUTPUT_FILE)

if __name__ == "__main__":
    main()
