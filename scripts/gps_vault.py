#!/usr/bin/env python3
"""
gps_vault.py — tiene i dati di posizione fuori dal repository pubblico
──────────────────────────────────────────────────────────────────────
I campi di posizione vengono tolti da data/activities.json e salvati cifrati
in data/gps.enc.json (AES-256-GCM, chiave derivata da MAP_PASSWORD con
PBKDF2-SHA256). La dashboard li decifra nel browser dopo aver chiesto la
password: i parametri qui devono restare allineati a decryptGpsVault() in
index.html.

Migrazione una tantum del file esistente:
    python gps_vault.py
"""

import base64
import json
import os
import sys
import unicodedata
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

VAULT_RELATIVE_PATH = Path("data") / "gps.enc.json"
PBKDF2_ITERATIONS = 600_000
SENSITIVE_FIELDS = ("gps_polyline", "start_lat", "start_lon")


class VaultError(Exception):
    """Vault illeggibile o password sbagliata."""


def _derive_key(password, salt, iterations):
    normalized = unicodedata.normalize("NFKC", password).encode("utf-8")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(normalized)


def _encrypt(payload, password):
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive_key(password, salt, PBKDF2_ITERATIONS)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return {
        "version": 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "cipher": "AES-256-GCM",
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _decrypt(vault, password):
    try:
        key = _derive_key(password, base64.b64decode(vault["salt"]), vault["iterations"])
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(vault["iv"]), base64.b64decode(vault["ciphertext"]), None
        )
    except InvalidTag as e:
        raise VaultError("MAP_PASSWORD non corrisponde a quella usata per data/gps.enc.json") from e
    except (KeyError, TypeError, ValueError) as e:
        raise VaultError(f"data/gps.enc.json non valido: {e}") from e
    return json.loads(plaintext.decode("utf-8"))


def load_payload(path, password):
    if not path.exists():
        return None
    try:
        vault = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise VaultError(f"data/gps.enc.json non valido: {e}") from e
    return _decrypt(vault, password)


def write_payload(path, payload, password):
    """Scrive il file cifrato e controlla che si rilegga uguale."""
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_encrypt(payload, password), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    if load_payload(path, password) != payload:
        raise VaultError(f"verifica di {path.name} fallita dopo la scrittura")


def restore(data, repo_path, password):
    """Reinserisce in memoria i dati di posizione nelle attivita'."""
    payload = load_payload(Path(repo_path) / VAULT_RELATIVE_PATH, password) or {}
    for record in data.get("activities", []):
        for field, value in payload.get(str(record["garmin_id"]), {}).items():
            if record.get(field) in (None, []):
                record[field] = value


def seal(data, repo_path, password):
    """Cifra i dati di posizione in gps.enc.json e li toglie da `data`.

    Il vault viene riscritto solo se il contenuto cambia, per non generare un
    commit a ogni sincronizzazione. I campi vengono tolti solo dopo aver
    riletto e verificato il file scritto.
    """
    path = Path(repo_path) / VAULT_RELATIVE_PATH
    previous = load_payload(path, password)
    payload = dict(previous or {})
    for record in data.get("activities", []):
        entry = {f: record[f] for f in SENSITIVE_FIELDS if record.get(f) not in (None, [])}
        if entry:
            payload[str(record["garmin_id"])] = entry

    if payload != previous:
        write_payload(path, payload, password)

    for record in data.get("activities", []):
        polyline = payload.get(str(record["garmin_id"]), {}).get("gps_polyline") or []
        for field in SENSITIVE_FIELDS:
            record.pop(field, None)
        record["has_gps"] = len(polyline) > 1


def main():
    import scarica_garmin as sg

    sg.load_env()
    password = os.environ.get("MAP_PASSWORD", "")
    repo_path = os.environ.get("REPO_PATH", "").strip()
    if not password:
        sys.exit("MAP_PASSWORD manca nel file .env")
    if not repo_path or not Path(repo_path).exists():
        sys.exit("REPO_PATH non valido, controlla il file .env")

    output_file = Path(repo_path) / "data" / "activities.json"
    with open(output_file, encoding="utf-8") as f:
        data = json.load(f)
    seal(data, repo_path, password)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Dati di posizione cifrati per {sum(r['has_gps'] for r in data['activities'])} attivita.")


if __name__ == "__main__":
    main()
