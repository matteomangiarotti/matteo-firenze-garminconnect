#!/usr/bin/env python3
"""
scarica_garmin.py
-----------------
Scarica le attività di corsa da Garmin Connect e aggiorna data/activities.json.

Funziona sia in locale (con variabili d'ambiente o .env) che su GitHub Actions
(con GitHub Secrets passati come env vars).

Variabili d'ambiente richieste:
  GARMIN_EMAIL     – email dell'account Garmin Connect
  GARMIN_PASSWORD  – password dell'account Garmin Connect

Uso:
  python scripts/scarica_garmin.py
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Dipendenze ──────────────────────────────────────────────────────────────
# pip install garminconnect

try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    raise SystemExit(
        "Installa la libreria: pip install garminconnect"
    )

# ── Configurazione ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Percorsi (relativi alla root del repository)
REPO_ROOT   = Path(__file__).resolve().parent.parent
DATA_DIR    = REPO_ROOT / "data"
OUTPUT_FILE = DATA_DIR / "activities.json"

# Quante attività recenti chiedere a Garmin ad ogni esecuzione
FETCH_LIMIT = 20

# Tipo attività Garmin da includere (running + trail)
RUNNING_TYPES = {"running", "trail_running", "treadmill_running"}


# ── Helper ──────────────────────────────────────────────────────────────────

def load_existing() -> dict:
    """Carica il JSON esistente oppure restituisce una struttura vuota."""
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": None, "activities": []}


def existing_ids(data: dict) -> set:
    """Restituisce l'insieme degli ID Garmin già salvati."""
    return {a["garmin_id"] for a in data.get("activities", [])}


def fmt_pace(seconds_per_km: float) -> str:
    """Converte sec/km → stringa 'M:SS'."""
    if not seconds_per_km or seconds_per_km <= 0:
        return "—"
    m = int(seconds_per_km // 60)
    s = int(seconds_per_km % 60)
    return f"{m}:{s:02d}"


def activity_date(act: dict) -> str:
    """Estrae la data locale YYYY-MM-DD dall'attività Garmin."""
    raw = act.get("startTimeLocal") or act.get("startTimeGMT", "")
    return raw[:10] if raw else ""


def parse_laps(laps_raw: list) -> list:
    """Elabora i lap grezzi di Garmin in un formato pulito."""
    parsed = []
    for i, lap in enumerate(laps_raw or [], start=1):
        dist   = lap.get("distance") or 0          # metri
        dur    = lap.get("duration") or lap.get("elapsedDuration") or 0  # secondi
        avg_hr = lap.get("averageHR") or lap.get("averageHeartRate")
        max_hr = lap.get("maxHR") or lap.get("maxHeartRate")
        pace   = (dur / (dist / 1000)) if dist > 50 else None

        parsed.append({
            "lap_index":      i,
            "distance_m":     round(dist),
            "duration_s":     round(dur),
            "avg_pace_s_km":  round(pace) if pace else None,
            "avg_pace_fmt":   fmt_pace(pace) if pace else "—",
            "avg_hr":         round(avg_hr) if avg_hr else None,
            "max_hr":         round(max_hr) if max_hr else None,
        })
    return parsed


def build_activity(act: dict, laps_raw: list) -> dict:
    """Costruisce il dizionario finale per un'attività."""
    dist   = act.get("distance") or 0          # metri
    dur    = act.get("duration") or act.get("movingDuration") or 0  # secondi
    avg_hr = act.get("averageHR") or act.get("averageHeartRate")
    max_hr = act.get("maxHR") or act.get("maxHeartRate")
    pace   = (dur / (dist / 1000)) if dist > 50 else None

    return {
        "garmin_id":      act.get("activityId"),
        "date":           activity_date(act),
        "start_time":     act.get("startTimeLocal", "")[:19],
        "name":           act.get("activityName", "Corsa"),
        "type":           act.get("activityType", {}).get("typeKey", "running"),
        "distance_m":     round(dist),
        "distance_km":    round(dist / 1000, 2),
        "duration_s":     round(dur),
        "avg_hr":         round(avg_hr) if avg_hr else None,
        "max_hr":         round(max_hr) if max_hr else None,
        "avg_pace_s_km":  round(pace) if pace else None,
        "avg_pace_fmt":   fmt_pace(pace) if pace else "—",
        "calories":       act.get("calories"),
        "elevation_gain": act.get("elevationGain"),
        "laps":           parse_laps(laps_raw),
        "fetched_at":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    # 1. Credenziali da variabili d'ambiente
    email    = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()

    if not email or not password:
        raise SystemExit(
            "❌  Imposta le variabili d'ambiente GARMIN_EMAIL e GARMIN_PASSWORD"
        )

    # 2. Carica dati esistenti
    DATA_DIR.mkdir(exist_ok=True)
    data     = load_existing()
    seen_ids = existing_ids(data)
    log.info("Attività già salvate: %d", len(seen_ids))

    # 3. Login Garmin
    log.info("Login Garmin Connect come %s …", email)
    try:
        client = Garmin(email, password)
        client.login()
    except GarminConnectAuthenticationError as e:
        raise SystemExit(f"❌  Autenticazione fallita: {e}")

    # 4. Scarica lista attività recenti
    log.info("Scarico le ultime %d attività …", FETCH_LIMIT)
    try:
        activities = client.get_activities(0, FETCH_LIMIT)
    except Exception as e:
        raise SystemExit(f"❌  Errore nel recupero attività: {e}")

    # 5. Filtra solo running non ancora salvate
    new_acts = [
        a for a in activities
        if a.get("activityType", {}).get("typeKey", "") in RUNNING_TYPES
        and a.get("activityId") not in seen_ids
    ]
    log.info("Nuove attività running da processare: %d", len(new_acts))

    if not new_acts:
        log.info("Nessuna novità — activities.json invariato.")
        # Aggiorna comunque il timestamp
        data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    # 6. Per ogni nuova attività scarica i lap e costruisci il record
    added = 0
    for act in new_acts:
        act_id   = act.get("activityId")
        act_date = activity_date(act)
        log.info("  Processo attività %s  (%s) …", act_id, act_date)

        try:
            laps_raw = client.get_activity_splits(act_id).get("lapDTOs", [])
        except Exception as e:
            log.warning("    Lap non disponibili per %s: %s", act_id, e)
            laps_raw = []

        record = build_activity(act, laps_raw)
        data["activities"].append(record)
        added += 1

        # Piccola pausa per non sovraccaricare le API Garmin
        time.sleep(1)

    # 7. Ordina per data decrescente e salva
    data["activities"].sort(key=lambda x: x.get("start_time", ""), reverse=True)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("✅  Aggiunte %d nuove attività → %s", added, OUTPUT_FILE)


if __name__ == "__main__":
    main()
