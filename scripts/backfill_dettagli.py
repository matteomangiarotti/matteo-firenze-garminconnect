#!/usr/bin/env python3
"""
backfill_dettagli.py
──────────────────────────────────────────────────────────────────
Forza il download di GPS, zone FC, cadenza, dislivello, training
effect per le attività già presenti in data/activities.json degli
ultimi N giorni (default 7) — senza aspettare la prossima corsa.

Riusa le funzioni gia' presenti in scarica_garmin.py (stesso file,
stessa cartella), quindi va lanciato da dentro scripts/.

USO:
    python backfill_dettagli.py           # ultimi 7 giorni
    python backfill_dettagli.py 14        # ultimi 14 giorni
    python backfill_dettagli.py all       # TUTTE le attivita da sempre
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime, timedelta

# Importa le funzioni dal main script (stessa cartella)
sys.path.insert(0, str(Path(__file__).parent))
import scarica_garmin as sg


def main():
    days_arg = sys.argv[1] if len(sys.argv) > 1 else "7"

    sg.load_env()
    email     = os.environ.get("GARMIN_EMAIL", "").strip()
    password  = os.environ.get("GARMIN_PASSWORD", "").strip()
    repo_path = os.environ.get("REPO_PATH", "").strip()
    if not repo_path or not Path(repo_path).exists():
        print("REPO_PATH non valido, controlla il file .env")
        sys.exit(1)

    # Carica FC_MAX_HISTORY aggiornata (inclusi eventuali valori aggiunti
    # dalla dashboard), altrimenti le zone FC userebbero solo il fallback.
    sg.FC_MAX_HISTORY = sg.load_fc_max_history(repo_path)

    output_file = Path(repo_path) / "data" / "activities.json"
    if not output_file.exists():
        print("activities.json non trovato.")
        sys.exit(1)

    with open(output_file, encoding="utf-8") as f:
        data = json.load(f)

    cutoff = "0000-00-00" if days_arg.lower() == "all" else (datetime.now() - timedelta(days=int(days_arg))).strftime("%Y-%m-%d")
    targets = [a for a in data["activities"] if a.get("date", "") >= cutoff]

    if not targets:
        print(f"Nessuna attivita trovata.")
        return

    label = "TUTTE le attivita" if days_arg.lower() == "all" else f"ultimi {days_arg} giorni"
    print(f"Trovate {len(targets)} attivita ({label}). Login Garmin...")

    from garminconnect import Garmin
    client = Garmin(email, password)
    client.login()
    print("Login riuscito.\n")

    for record in targets:
        act_id = record.get("garmin_id")
        if not act_id:
            continue
        print(f"→ {record.get('date')}  {record.get('name')}  (id: {act_id})")

        # Splits aggiornati (cadenza, dislivello, potenza per lap)
        laps_raw = []
        try:
            laps_raw = client.get_activity_splits(act_id).get("lapDTOs", [])
            record["laps"] = sg.parse_laps(laps_raw)
            print(f"    Split: {len(record['laps'])} lap aggiornati")
        except Exception as e:
            print(f"    Split non disponibili: {e}")

        # Dettagli attivita (dislivello totale, training effect)
        try:
            act_detail = client.get_activity(act_id)
            if act_detail:
                eg = act_detail.get("elevationGain")
                el = act_detail.get("elevationLoss")
                cad = act_detail.get("averageRunningCadenceInStepsPerMinute") or act_detail.get("averageRunCadence")
                if eg is not None: record["elevation_gain"] = round(eg)
                if el is not None: record["elevation_loss"] = round(el)
                if cad: record["avg_cadence"] = round(cad)
                if act_detail.get("aerobicTrainingEffect"):
                    record["training_effect_aerobic"] = act_detail["aerobicTrainingEffect"]
                if act_detail.get("anaerobicTrainingEffect"):
                    record["training_effect_anaerobic"] = act_detail["anaerobicTrainingEffect"]
                print(f"    Dettagli: dislivello +{record.get('elevation_gain')}/-{record.get('elevation_loss')} m, cadenza {record.get('avg_cadence')} spm")
        except Exception as e:
            print(f"    Dettagli non disponibili: {e}")

        # GPS
        gps = sg.fetch_gps_polyline(client, act_id)
        record["gps_polyline"] = gps
        print(f"    GPS: {len(gps)} punti")

        # Zone FC — fino al 4 settembre 2026 (incluso) calcoliamo noi dai lap
        # (le nostre soglie); dal 5 settembre in poi Garmin e' stato sistemato
        # quindi usiamo direttamente i suoi dati nativi.
        act_date = record.get("date", "")
        if act_date <= "2026-09-04":
            hrz = sg.compute_hr_zone_time_from_laps(laps_raw, sg.get_fc_max_for_date(act_date))
        else:
            hrz = sg.fetch_hr_zone_time(client, act_id)
        record["hr_zones"] = hrz
        print(f"    Zone FC: {hrz}")

        print()

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Fatto. File aggiornato: {output_file}")
    print("Ora fai commit + push come al solito.")


if __name__ == "__main__":
    main()
