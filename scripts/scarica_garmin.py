"""
Scarica tutte le attività di CORSA da Garmin Connect in formato .fit
Richiede: pip install garminconnect
"""

import os
import sys
import time
import getpass
from datetime import datetime
from garminconnect import Garmin

# ── Configurazione ──────────────────────────────────────────────────────────
CARTELLA_OUTPUT = "fit_garmin"          # cartella dove salvare i file
TIPO_ATTIVITA   = "running"             # filtra solo corsa
PAUSA_TRA_DOWNLOAD = 1.5                # secondi tra un download e l'altro
# ────────────────────────────────────────────────────────────────────────────


def login():
    print("=" * 50)
    print("  Download attività Garmin Connect")
    print("=" * 50)
    email    = input("\nEmail Garmin: ").strip()
    password = getpass.getpass("Password Garmin (nascosta): ")

    print("\nConnessione in corso...")
    try:
        client = Garmin(email, password)
        client.login()
        print("✓ Login riuscito!\n")
        return client
    except Exception as e:
        print(f"\n✗ Errore di login: {e}")
        print("  Controlla email/password e riprova.")
        sys.exit(1)


def scarica_attivita(client):
    os.makedirs(CARTELLA_OUTPUT, exist_ok=True)

    print("Recupero lista attività...")
    try:
        # Scarica fino a 1000 attività (puoi aumentare se ne hai di più)
        attivita = client.get_activities_by_date(
            startdate="2000-01-01",
            enddate=datetime.today().strftime("%Y-%m-%d"),
            activitytype=TIPO_ATTIVITA
        )
    except Exception as e:
        print(f"✗ Errore nel recupero attività: {e}")
        sys.exit(1)

    totale = len(attivita)
    if totale == 0:
        print("Nessuna attività di corsa trovata.")
        return

    print(f"Trovate {totale} attività di corsa.\n")

    scaricati = 0
    saltati   = 0
    errori    = 0

    for i, a in enumerate(attivita, 1):
        activity_id   = a["activityId"]
        data_str      = a.get("startTimeLocal", "data_sconosciuta")[:10]
        nome          = a.get("activityName", "corsa").replace("/", "-")
        nome_file     = f"{data_str}_{activity_id}_{nome}.fit"
        percorso      = os.path.join(CARTELLA_OUTPUT, nome_file)

        # Salta se già scaricato
        if os.path.exists(percorso):
            print(f"  [{i}/{totale}] Già presente: {nome_file}")
            saltati += 1
            continue

        try:
            print(f"  [{i}/{totale}] Scarico: {nome_file} ...", end=" ", flush=True)
            dati_fit = client.download_activity(
                activity_id,
                dl_fmt=client.ActivityDownloadFormat.ORIGINAL
            )
            with open(percorso, "wb") as f:
                f.write(dati_fit)
            print("✓")
            scaricati += 1
            time.sleep(PAUSA_TRA_DOWNLOAD)   # gentile con i server Garmin
        except Exception as e:
            print(f"✗ ({e})")
            errori += 1

    print(f"\n{'='*50}")
    print(f"  Completato!")
    print(f"  Scaricati : {scaricati}")
    print(f"  Già presenti: {saltati}")
    print(f"  Errori    : {errori}")
    print(f"  Cartella  : {os.path.abspath(CARTELLA_OUTPUT)}")
    print(f"{'='*50}")
    print("\nOra carica i file .fit qui su Claude per l'analisi!")


if __name__ == "__main__":
    client = login()
    scarica_attivita(client)
