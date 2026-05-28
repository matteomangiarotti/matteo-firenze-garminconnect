#!/usr/bin/env python3
"""
scarica_garmin.py — versione automatica per Windows Task Scheduler
─────────────────────────────────────────────────────────────────
Gira sul PC di casa ogni giorno, legge credenziali da .env locale,
scarica le nuove attività Garmin, aggiorna data/activities.json
e fa git push verso GitHub.

Setup una-tantum:
  1. Crea il file .env accanto a questo script con:
       GARMIN_EMAIL=tuaemail@gmail.com
       GARMIN_PASSWORD=tuapassword
       REPO_PATH=C:\Users\utente\matteo-firenze-garminconnect
  2. pip install garminconnect
  3. Aggiungi questo script al Task Scheduler di Windows
"""

import os, sys, json, time, logging, subprocess
from datetime import datetime, timezone
from pathlib import Path

# ── Logging su file (così puoi controllare cosa è successo) ──────────────────
LOG_FILE = Path(__file__).parent / "garmin_sync.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

FETCH_LIMIT   = 30
RUNNING_TYPES = {"running", "trail_running", "treadmill_running"}

# ── Carica .env ───────────────────────────────────────────────────────────────
def load_env():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        log.error(".env non trovato in %s", env_file.parent)
        log.error("Crea il file .env con GARMIN_EMAIL, GARMIN_PASSWORD, REPO_PATH")
        sys.exit(1)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── Helper ────────────────────────────────────────────────────────────────────
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
    dist = act.get("distance") or 0
    dur  = act.get("duration") or act.get("movingDuration") or 0
    hr   = act.get("averageHR") or act.get("averageHeartRate")
    pace = (dur / (dist / 1000)) if dist > 50 else None
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

# ── Git push ──────────────────────────────────────────────────────────────────
def git_push(repo_path):
    try:
        subprocess.run(["git", "-C", repo_path, "add", "data/activities.json"], check=True)
        result = subprocess.run(
            ["git", "-C", repo_path, "diff", "--cached", "--quiet"],
            capture_output=True
        )
        if result.returncode == 0:
            log.info("Nessuna modifica da committare.")
            return
        subprocess.run([
            "git", "-C", repo_path, "commit",
            "-m", f"chore: aggiorna activities.json [skip ci]"
        ], check=True)
        subprocess.run(["git", "-C", repo_path, "push", "origin", "main"], check=True)
        log.info("✅ Push su GitHub completato.")
    except subprocess.CalledProcessError as e:
        log.error("Errore git: %s", e)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    load_env()

    email     = os.environ.get("GARMIN_EMAIL", "").strip()
    password  = os.environ.get("GARMIN_PASSWORD", "").strip()
    repo_path = os.environ.get("REPO_PATH", "").strip()

    if not email or not password:
        log.error("GARMIN_EMAIL e GARMIN_PASSWORD devono essere nel file .env")
        sys.exit(1)
    if not repo_path or not Path(repo_path).exists():
        log.error("REPO_PATH non valido: %s", repo_path)
        sys.exit(1)

    output_file = Path(repo_path) / "data" / "activities.json"
    output_file.parent.mkdir(exist_ok=True)

    # Carica JSON esistente
    if output_file.exists():
        with open(output_file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"last_updated": None, "activities": []}

    seen_ids = {a["garmin_id"] for a in data.get("activities", [])}
    log.info("Attività già salvate: %d", len(seen_ids))

    # Login Garmin
    try:
        from garminconnect import Garmin
    except ImportError:
        log.error("Installa la libreria: pip install garminconnect")
        sys.exit(1)

    log.info("Login Garmin Connect come %s...", email)
    try:
        client = Garmin(email, password)
        client.login()
        log.info("Login riuscito.")
    except Exception as e:
        log.error("Login fallito: %s", e)
        sys.exit(1)

    # Scarica lista attività recenti
    log.info("Scarico le ultime %d attività...", FETCH_LIMIT)
    try:
        activities = client.get_activities(0, FETCH_LIMIT)
    except Exception as e:
        log.error("Errore nel recupero attività: %s", e)
        sys.exit(1)

    # Filtra solo running non ancora salvate
    new_acts = [
        a for a in activities
        if a.get("activityType", {}).get("typeKey", "") in RUNNING_TYPES
        and a.get("activityId") not in seen_ids
    ]
    log.info("Nuove attività running da processare: %d", len(new_acts))

    if not new_acts:
        log.info("Nessuna novità — aggiorno solo il timestamp.")
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    # Scarica lap per ogni nuova attività
    added = 0
    for act in new_acts:
        act_id   = act.get("activityId")
        act_date = activity_date(act)
        log.info("  Processo %s (%s)...", act_id, act_date)
        try:
            laps_raw = client.get_activity_splits(act_id).get("lapDTOs", [])
        except Exception as e:
            log.warning("    Lap non disponibili: %s", e)
            laps_raw = []
        data["activities"].append(build_activity(act, laps_raw))
        added += 1
        time.sleep(1.5)

    data["activities"].sort(key=lambda x: x.get("start_time", ""), reverse=True)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("✅ Aggiunte %d nuove attività → %s", added, output_file)

    # Push su GitHub
    git_push(repo_path)

if __name__ == "__main__":
    log.info("═" * 50)
    log.info("Garmin Sync — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("═" * 50)
    main()
