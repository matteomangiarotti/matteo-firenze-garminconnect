#!/usr/bin/env python3
"""
scarica_garmin.py — usa GARMIN-SSO-CUST-GUID + GARMIN-SSO-GUID
"""
import os, json, time, logging, requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

REPO_ROOT   = Path(__file__).resolve().parent.parent
DATA_DIR    = REPO_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "activities.json"
FETCH_LIMIT = 20
RUNNING_TYPES = {"running", "trail_running", "treadmill_running"}

def get_session(cust_guid, sso_guid):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "NK": "NT",
        "X-App-Ver": "5.25.0.30a",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "it-IT,it;q=0.9",
        "Referer": "https://connect.garmin.com/modern/activities",
        "X-Requested-With": "XMLHttpRequest",
        "Di-Backend": "connectapi.garmin.com",
    })
    s.cookies.set("GARMIN-SSO", "1", domain=".garmin.com")
    s.cookies.set("GARMIN-SSO-CUST-GUID", cust_guid, domain=".garmin.com")
    s.cookies.set("GARMIN-SSO-GUID", sso_guid, domain=".garmin.com")
    return s

def fetch_activities(session):
    # Prova varianti di parametri sul primo endpoint che risponde 200
    url = "https://connect.garmin.com/proxy/activitylist-service/activities/search/activities"
    param_variants = [
        {"start": 0, "limit": FETCH_LIMIT},
        {"start": "0", "limit": str(FETCH_LIMIT), "activityType": "running"},
        {"start": 0, "limit": FETCH_LIMIT, "sortField": "startLocal", "sortOrder": "desc"},
    ]
    for params in param_variants:
        try:
            log.info("Provo params: %s", params)
            r = session.get(url, params=params, timeout=30)
            log.info("  → %s | %s", r.status_code, r.headers.get("Content-Type",""))
            log.info("  → Body: %s", r.text[:300])
            if r.status_code == 200 and r.text.strip():
                data = r.json()
                if isinstance(data, list) and data:
                    log.info("  ✅ Lista con %d elementi", len(data))
                    return data
                elif isinstance(data, dict):
                    for key in ["activityList","activities","data","results"]:
                        if key in data and isinstance(data[key], list) and data[key]:
                            log.info("  ✅ Dict[%s] con %d elementi", key, len(data[key]))
                            return data[key]
                    log.warning("  → Dict senza lista: %s", list(data.keys()))
        except Exception as e:
            log.warning("  → Eccezione: %s", e)

    # Prova endpoint alternativo Garmin moderne API
    url2 = "https://connect.garmin.com/proxy/userstats-service/activities/recentActivities"
    try:
        r2 = session.get(url2, params={"numActivities": FETCH_LIMIT}, timeout=30)
        log.info("recentActivities → %s | %s", r2.status_code, r2.text[:300])
    except Exception as e:
        log.warning("recentActivities → %s", e)

    raise SystemExit("❌ Nessun endpoint funzionante — vedi log sopra")

def fetch_laps(session, act_id):
    url = f"https://connect.garmin.com/proxy/activity-service/activity/{act_id}/splits"
    try:
        r = session.get(url, timeout=30)
        if r.status_code == 200:
            return r.json().get("lapDTOs", [])
    except Exception as e:
        log.warning("Lap %s: %s", act_id, e)
    return []

def load_existing():
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": None, "activities": []}

def existing_ids(data):
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
        parsed.append({"lap_index": i, "distance_m": round(dist), "duration_s": round(dur),
                        "avg_pace_s_km": round(pace) if pace else None, "avg_pace_fmt": fmt_pace(pace),
                        "avg_hr": round(hr) if hr else None})
    return parsed

def build_activity(act, laps_raw):
    dist = act.get("distance") or 0
    dur  = act.get("duration") or act.get("movingDuration") or 0
    hr   = act.get("averageHR") or act.get("averageHeartRate")
    pace = (dur / (dist / 1000)) if dist > 50 else None
    return {"garmin_id": act.get("activityId"), "date": activity_date(act),
            "start_time": act.get("startTimeLocal", "")[:19], "name": act.get("activityName", "Corsa"),
            "type": act.get("activityType", {}).get("typeKey", "running"),
            "distance_m": round(dist), "distance_km": round(dist/1000, 2),
            "duration_s": round(dur), "avg_hr": round(hr) if hr else None,
            "avg_pace_s_km": round(pace) if pace else None, "avg_pace_fmt": fmt_pace(pace),
            "calories": act.get("calories"), "elevation_gain": act.get("elevationGain"),
            "laps": parse_laps(laps_raw),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

def main():
    cust_guid = os.environ.get("GARMIN_SSO_GUID", "").strip()
    sso_guid  = os.environ.get("GARMIN_SSO_GUID2", "").strip()
    if not cust_guid or not sso_guid:
        raise SystemExit("❌  Imposta GARMIN_SSO_GUID e GARMIN_SSO_GUID2")

    DATA_DIR.mkdir(exist_ok=True)
    data     = load_existing()
    seen_ids = existing_ids(data)
    log.info("Attività già salvate: %d", len(seen_ids))

    session    = get_session(cust_guid, sso_guid)
    activities = fetch_activities(session)
    log.info("Totale attività ricevute: %d", len(activities))

    new_acts = [a for a in activities
                if a.get("activityType", {}).get("typeKey", "") in RUNNING_TYPES
                and a.get("activityId") not in seen_ids]
    log.info("Nuove running: %d", len(new_acts))

    if not new_acts:
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("Nessuna novità.")
        return

    for act in new_acts:
        act_id = act.get("activityId")
        log.info("  Processo %s (%s)...", act_id, activity_date(act))
        laps_raw = fetch_laps(session, act_id)
        data["activities"].append(build_activity(act, laps_raw))
        time.sleep(1)

    data["activities"].sort(key=lambda x: x.get("start_time",""), reverse=True)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("✅  Aggiunte %d attività", len(new_acts))

if __name__ == "__main__":
    main()
