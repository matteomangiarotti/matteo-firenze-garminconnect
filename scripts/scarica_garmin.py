#!/usr/bin/env python3
"""
scarica_garmin.py — versione completa con scoring integrato
────────────────────────────────────────────────────────────
1. Legge piano.ics dal repository
2. Scarica nuove attivita da Garmin Connect
3. Abbina ogni attivita al piano del giorno
4. Calcola il voto automatico con il motore di scoring
5. Salva tutto in data/activities.json
6. Git push su GitHub
"""

import os, sys, json, time, logging, subprocess, re
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
import math

# ── Logging ───────────────────────────────────────────────────────────────────
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

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 1 — CARICA .env
# ══════════════════════════════════════════════════════════════════════════════

def load_env():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        log.error(".env non trovato in %s", env_file.parent)
        sys.exit(1)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 2 — PARSER ICS
# ══════════════════════════════════════════════════════════════════════════════

def parse_ics(path):
    """Parsa piano.ics e restituisce lista di eventi con tutti i campi."""
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r'\r?\n[ \t]', '', text)
    events = []
    blocks = text.split("BEGIN:VEVENT")
    for block in blocks[1:]:
        def get(k):
            m = re.search(k + r'[^:]*:([^\r\n]+)', block)
            if not m: return ""
            v = m.group(1).strip()
            v = v.replace(r'\n', '\n').replace(r'\,', ',').replace(r'\;', ';')
            return v

        dtraw = get("DTSTART")
        if not dtraw: continue
        dtclean = re.sub(r'T.*$', '', dtraw)
        if len(dtclean) < 8: continue
        date_str = dtclean[:4] + "-" + dtclean[4:6] + "-" + dtclean[6:8]
        summary  = get("SUMMARY")
        desc     = get("DESCRIPTION")

        def field_val(key):
            m = re.search(key + r':\s*([^\n\\]+)', desc)
            return m.group(1).strip() if m else ""

        tipo      = field_val("Tipo").lower()
        ritmo     = field_val("Ritmo")
        fc_raw    = field_val("FC")
        struttura = field_val("Struttura")

        # Tipo normalizzato
        if "riposo" in summary.lower():
            wtype = "rest"
        elif "recovery" in tipo or "recupero" in tipo:
            wtype = "recovery"
        elif "lungo" in tipo:
            wtype = "lungo"
        elif any(x in tipo for x in ["qualit", "soglia", "medio", "ripetute", "progressivo"]):
            wtype = "qualita"
        elif "facile" in tipo or "easy" in tipo:
            wtype = "easy"
        else:
            wtype = "easy"

        # Passo target
        pace_min_s = pace_max_s = None
        pm = re.search(r'(\d):(\d{2})[–\-](\d):(\d{2})', ritmo)
        if pm:
            pace_min_s = int(pm.group(1))*60 + int(pm.group(2))
            pace_max_s = int(pm.group(3))*60 + int(pm.group(4))

        # FC target
        hr_min = hr_max = None
        hm = re.search(r'(\d{2,3})[–\-](\d{2,3})\s*bpm', fc_raw)
        if hm:
            hr_min = int(hm.group(1))
            hr_max = int(hm.group(2))

        # Distanza dal titolo
        dist_km = None
        dm = re.search(r'(\d+)\s*km', summary, re.IGNORECASE)
        if dm: dist_km = float(dm.group(1))

        events.append({
            "date":       date_str,
            "summary":    summary,
            "type":       wtype,
            "pace_min_s": pace_min_s,
            "pace_max_s": pace_max_s,
            "hr_min":     hr_min,
            "hr_max":     hr_max,
            "distance_km": dist_km,
            "struttura":  struttura,
        })

    events.sort(key=lambda x: x["date"])
    return events

def build_plan_index(events):
    """Indice data -> evento piano."""
    idx = {}
    for e in events:
        idx[e["date"]] = e
    return idx

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 3 — PARSER STRUTTURA
# ══════════════════════════════════════════════════════════════════════════════

def parse_struttura(raw):
    if not raw:
        return {}
    s = raw.strip()
    result = {"raw": s, "reps": 0, "rep_min": None, "rep_km": None,
              "rec_min": None, "rec_km": None, "wu_min": 0.0, "cd_min": 0.0,
              "is_prog": False}

    # WU
    m = re.search(r'WU(\d+(?:\.\d+)?)(km|m)?', s, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "km": result["wu_min"] = val / (5.5/60)
        elif unit == "m": result["wu_min"] = (val/1000) / (5.5/60)
        else: result["wu_min"] = val

    # CD
    m = re.search(r'CD(\d+(?:\.\d+)?)(km|m)?', s, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "km": result["cd_min"] = val / (5.5/60)
        elif unit == "m": result["cd_min"] = (val/1000) / (5.5/60)
        else: result["cd_min"] = val

    # PROG
    m = re.search(r'PROG(\d+(?:\.\d+)?)(km|m|\'|min)?', s, re.IGNORECASE)
    if m:
        result["is_prog"] = True
        val = float(m.group(1))
        unit = (m.group(2) or "").lower().replace("'", "")
        if unit == "km": result["rep_km"] = val
        elif unit == "m": result["rep_km"] = val/1000
        else: result["rep_min"] = val
        result["reps"] = 1
        return result

    # NxDURATA
    m = re.search(r'(\d+)x(\d+(?:\.\d+)?)(km|m|\'|min|s)?', s, re.IGNORECASE)
    if m:
        result["reps"] = int(m.group(1))
        val = float(m.group(2))
        unit = (m.group(3) or "").lower().replace("'", "").replace("\u2019", "")
        if unit == "km": result["rep_km"] = val
        elif unit == "m": result["rep_km"] = val/1000
        elif unit == "s": result["rep_min"] = val/60
        else: result["rep_min"] = val

    # REC
    m = re.search(r'REC(\d+(?:\.\d+)?)(km|m|s)?', s, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "km": result["rec_km"] = val
        elif unit == "m": result["rec_km"] = val/1000
        elif unit == "s": result["rec_min"] = val/60
        else: result["rec_min"] = val

    return result

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 4 — MOTORE DI SCORING
# ══════════════════════════════════════════════════════════════════════════════

def clamp(v, lo, hi): return max(lo, min(hi, v))

def fmt_pace(s):
    if not s or s <= 0: return "---"
    return f"{int(s//60)}:{int(s%60):02d}/km"

def score_hr(hr, hr_min, hr_max, flags, notes):
    if not hr or not hr_min or not hr_max: return 7.0
    delta_up   = max(0, hr - hr_max)
    delta_down = max(0, hr_min - hr)
    if delta_up == 0 and delta_down == 0:
        return 10.0
    elif delta_up > 0:
        s = clamp(10 - delta_up * 0.35, 1, 10)
        if delta_up >= 10:
            flags.append("HR_HIGH")
            notes.append(f"FC media {hr:.0f} bpm supera il target di {delta_up:.0f} bpm.")
        return round(s, 2)
    else:
        s = clamp(10 - delta_down * 0.08, 7, 10)
        if delta_down >= 7:
            flags.append("HR_LOW")
            notes.append(f"FC {hr:.0f} bpm sotto target di {delta_down:.0f} bpm — controlla le soglie.")
        return round(s, 2)

def score_pace(pace_s, pace_min_s, pace_max_s, flags, notes, wtype="easy"):
    if not pace_s or not pace_min_s or not pace_max_s: return 7.0
    delta_fast = max(0, pace_min_s - pace_s)
    delta_slow = max(0, pace_s - pace_max_s)
    if delta_fast == 0 and delta_slow == 0: return 10.0
    elif delta_fast > 0:
        mult = 0.30 if wtype in ("recovery", "lungo") else 0.20
        s = clamp(10 - delta_fast * mult, 3, 10)
        if delta_fast >= 20:
            flags.append("TOO_FAST")
            notes.append(f"Passo {fmt_pace(pace_s)} piu veloce del target ({fmt_pace(pace_min_s)}).")
        return round(s, 2)
    else:
        return round(clamp(10 - delta_slow * 0.20, 4, 10), 2)

def score_distance(actual_km, target_km, flags, notes):
    if not target_km or target_km <= 0: return 7.0
    ratio = actual_km / target_km
    if ratio >= 0.97:   s = 10.0
    elif ratio >= 0.90: s = 7.0 + (ratio - 0.90) * 30
    elif ratio >= 0.70: s = 4.0 + (ratio - 0.70) * 15
    else:
        s = max(1.0, ratio * 6)
        flags.append("INCOMPLETE")
        notes.append(f"Completato {ratio*100:.0f}% della distanza ({actual_km:.1f}/{target_km:.1f} km).")
    return round(clamp(s, 1, 10), 2)

def score_cardiac_drift(laps, flags, notes):
    if len(laps) < 4: return 8.0
    mid = len(laps) // 2
    h1 = [l.get("avg_hr") for l in laps[:mid] if l.get("avg_hr")]
    h2 = [l.get("avg_hr") for l in laps[mid:] if l.get("avg_hr")]
    if not h1 or not h2: return 8.0
    drift = sum(h2)/len(h2) - sum(h1)/len(h1)
    if drift <= 5:   return 10.0
    elif drift <= 8: return 8.0
    elif drift <= 12:
        flags.append("CARDIAC_DRIFT")
        notes.append(f"Deriva cardiaca di {drift:.0f} bpm nella seconda meta.")
        return 6.0
    else:
        flags.append("CARDIAC_DRIFT")
        notes.append(f"Deriva cardiaca elevata ({drift:.0f} bpm).")
        return 4.0

def segment_laps(laps, plan):
    """Separa lap in fast/slow/warmup/cooldown."""
    if not laps: return [], [], [], []
    threshold = (plan.get("pace_min_s") or 280) + 15
    fast, slow = [], []
    for lap in laps:
        pace = lap.get("avg_pace_s_km")
        dist = lap.get("distance_m", 0)
        if not pace or dist < 100: continue
        if pace < threshold: fast.append(lap)
        else: slow.append(lap)
    first_fast = laps.index(fast[0]) if fast else len(laps)
    last_fast  = laps.index(fast[-1]) if fast else 0
    wu = [l for l in laps[:first_fast] if l.get("avg_pace_s_km", 0) >= threshold]
    cd = [l for l in laps[last_fast+1:] if l.get("avg_pace_s_km", 0) >= threshold]
    return fast, slow, wu, cd

def score_fast_blocks(fast_laps, plan, st, flags, notes):
    if not fast_laps:
        flags.append("NO_FAST_BLOCKS")
        notes.append("Nessun blocco veloce rilevato.")
        return 2.0
    pace_min = plan.get("pace_min_s") or 260
    pace_max = plan.get("pace_max_s") or 280
    target_mid = (pace_min + pace_max) / 2
    target_reps = st.get("reps") or len(fast_laps)
    paces = [l.get("avg_pace_s_km") for l in fast_laps if l.get("avg_pace_s_km")]
    if not paces: return 3.0
    lap_scores = [clamp(10 - abs(p - target_mid) * 0.30, 1, 10) for p in paces]
    score_vic = sum(lap_scores) / len(lap_scores)
    if len(paces) > 1:
        mean_p = sum(paces)/len(paces)
        std_p  = math.sqrt(sum((p-mean_p)**2 for p in paces)/len(paces))
        score_reg = clamp(10 - std_p * 0.4, 1, 10)
    else:
        score_reg = 8.0
    pen_fade = 0
    if len(paces) >= 4:
        mid = len(paces)//2
        p1 = sum(paces[:mid])/mid
        p2 = sum(paces[mid:])/(len(paces)-mid)
        if p2 - p1 > 10:
            flags.append("FADE")
            notes.append(f"Calo progressivo: prima meta {fmt_pace(p1)}, seconda {fmt_pace(p2)}.")
            pen_fade = 1.5
    if target_reps > 0 and len(fast_laps)/target_reps < 0.70:
        notes.append(f"Completate {len(fast_laps)} su {target_reps} ripetute.")
    s = 0.70*score_vic + 0.30*score_reg - pen_fade
    return round(clamp(s, 1, 10), 2)

def score_recoveries(slow_laps, plan, st, flags, notes):
    if not slow_laps:
        if (st.get("reps") or 0) > 1:
            flags.append("SHORT_RECOVERY")
            notes.append("Recuperi non rilevati tra le ripetute.")
            return 3.0
        return 7.0
    target_rec_s = (st.get("rec_min") or 2.0) * 60
    scores = []
    for lap in slow_laps:
        dur  = lap.get("duration_s") or 0
        pace = lap.get("avg_pace_s_km") or 999
        ratio = dur/target_rec_s if target_rec_s > 0 else 1.0
        s_dur = 10.0 if ratio >= 0.80 else (6.0 if ratio >= 0.50 else 3.0)
        if ratio < 0.50: flags.append("SHORT_RECOVERY")
        s_int = clamp(10 - max(0, 480-pace)*0.05, 1, 10)
        scores.append(0.50*s_dur + 0.50*s_int)
    return round(clamp(sum(scores)/len(scores), 1, 10), 2)

def score_wucd(wu_laps, cd_laps, plan, st, flags, notes):
    wu_dur = sum(l.get("duration_s",0) for l in wu_laps)
    cd_dur = sum(l.get("duration_s",0) for l in cd_laps)
    t_wu = (st.get("wu_min") or 10.0) * 60
    t_cd = (st.get("cd_min") or 10.0) * 60
    if wu_dur >= t_wu*0.80:   s_wu = 10.0
    elif wu_dur >= t_wu*0.40: s_wu = 6.0
    else:
        s_wu = 2.0
        flags.append("MISSING_WARMUP")
        notes.append("Warm-up assente o molto breve.")
    if cd_dur >= t_cd*0.80:   s_cd = 10.0
    elif cd_dur >= t_cd*0.40: s_cd = 6.0
    else:
        s_cd = 3.0
        flags.append("MISSING_COOLDOWN")
        notes.append("Cooldown assente o molto breve.")
    return round((s_wu + s_cd)/2, 2)

def apply_caps(score, plan, activity, fast_laps, flags):
    cap, reason = 10.0, None
    hr    = activity.get("avg_hr") or 0
    pace  = activity.get("avg_pace_s_km") or 999
    dist  = activity.get("distance_km") or 0
    t_dist = plan.get("distance_km") or 0
    t_reps = plan.get("struttura_parsed", {}).get("reps") or 0
    wtype = plan.get("type","")
    if wtype == "recovery":
        if hr > (plan.get("hr_max") or 147) + 15 and 6.5 < cap:
            cap, reason = 6.5, "Recovery: FC molto sopra il range"
        if plan.get("pace_min_s") and pace < plan["pace_min_s"] - 20 and 7.0 < cap:
            cap, reason = 7.0, "Recovery corsa troppo veloce"
    elif wtype == "qualita":
        if len(fast_laps) < 2 and 6.0 < cap:
            cap, reason = 6.0, "Qualita: nessun blocco veloce"
        if t_reps > 0 and len(fast_laps)/t_reps < 0.70 and 7.0 < cap:
            cap, reason = 7.0, "Qualita: meno del 70% delle ripetute"
        if "MISSING_WARMUP" in flags and "MISSING_COOLDOWN" in flags:
            # Cap solo se WU/CD erano previsti nel piano
            st_check = plan.get("struttura_parsed") or {}
            if (st_check.get("wu_min") or 0) > 0 or (st_check.get("cd_min") or 0) > 0:
                if 8.5 < cap:
                    cap, reason = 8.5, "Qualita: warm-up e cooldown assenti (previsti nel piano)"
    elif wtype == "lungo":
        if t_dist > 0 and dist/t_dist < 0.70 and 6.0 < cap:
            cap, reason = 6.0, "Lungo: meno del 70% della distanza"
        if hr > (plan.get("hr_max") or 160) + 10 and 7.5 < cap:
            cap, reason = 7.5, "Lungo trasformato in medio tirato"
    return round(min(score, cap), 1), reason

def auto_score(plan, activity):
    """Calcola il voto automatico per un'attivita dato il piano."""
    if not plan or plan.get("type") == "rest":
        return None
    flags, notes = [], []
    laps  = activity.get("laps") or []
    st    = plan.get("struttura_parsed") or {}
    wtype = plan.get("type","easy")

    # WU/CD valutati solo se esplicitamente previsti nella Struttura
    has_wu = (st.get("wu_min") or 0) > 0
    has_cd = (st.get("cd_min") or 0) > 0

    if wtype == "recovery":
        # Recovery: no WU/CD — solo FC, passo, distanza
        s_hr   = score_hr(activity.get("avg_hr"), plan.get("hr_min") or 129, plan.get("hr_max") or 147, flags, notes)
        s_pace = score_pace(activity.get("avg_pace_s_km"), plan.get("pace_min_s"), plan.get("pace_max_s"), flags, notes, "recovery")
        s_dist = score_distance(activity.get("distance_km",0), plan.get("distance_km"), flags, notes)
        subscores = {"hr": s_hr, "pace": s_pace, "distance": s_dist}
        weighted  = 0.50*s_hr + 0.20*s_pace + 0.30*s_dist
        fast_laps = []

    elif wtype == "easy":
        # Easy: no WU/CD — FC, passo, distanza
        s_hr   = score_hr(activity.get("avg_hr"), plan.get("hr_min") or 147, plan.get("hr_max") or 160, flags, notes)
        s_pace = score_pace(activity.get("avg_pace_s_km"), plan.get("pace_min_s"), plan.get("pace_max_s"), flags, notes, "easy")
        s_dist = score_distance(activity.get("distance_km",0), plan.get("distance_km"), flags, notes)
        subscores = {"hr": s_hr, "pace": s_pace, "distance": s_dist}
        weighted  = 0.35*s_hr + 0.35*s_pace + 0.30*s_dist
        fast_laps = []

    elif wtype == "lungo":
        s_hr    = score_hr(activity.get("avg_hr"), plan.get("hr_min") or 147, plan.get("hr_max") or 160, flags, notes)
        s_pace  = score_pace(activity.get("avg_pace_s_km"), plan.get("pace_min_s"), plan.get("pace_max_s"), flags, notes, "lungo")
        s_dist  = score_distance(activity.get("distance_km",0), plan.get("distance_km"), flags, notes)
        s_drift = score_cardiac_drift(laps, flags, notes)
        s_hr_d  = (s_hr + s_drift)/2
        if has_wu or has_cd:
            wu_l   = laps[:2]; cd_l = laps[-2:]
            s_wucd = score_wucd(wu_l, cd_l, plan, st, flags, notes)
            subscores = {"hr_drift": s_hr_d, "pace": s_pace, "distance": s_dist, "wu_cd": s_wucd}
            weighted  = 0.22*s_hr_d + 0.23*s_pace + 0.35*s_dist + 0.20*s_wucd
        else:
            subscores = {"hr_drift": s_hr_d, "pace": s_pace, "distance": s_dist}
            weighted  = 0.25*s_hr_d + 0.25*s_pace + 0.50*s_dist
        fast_laps = []

    elif wtype == "qualita":
        fast_laps, slow_laps, wu_l, cd_l = segment_laps(laps, plan)
        s_blocks = score_fast_blocks(fast_laps, plan, st, flags, notes)
        s_recov  = score_recoveries(slow_laps, plan, st, flags, notes)
        s_hr     = score_hr(activity.get("avg_hr"), plan.get("hr_min") or 161, plan.get("hr_max") or 174, flags, notes)
        s_dist   = score_distance(activity.get("distance_km",0), plan.get("distance_km"), flags, notes)
        if has_wu or has_cd:
            s_wucd   = score_wucd(wu_l, cd_l, plan, st, flags, notes)
            s_struct = (s_recov + s_wucd)/2
            subscores = {"fast_blocks": s_blocks, "structure": s_struct, "recoveries": s_recov,
                         "wu_cd": s_wucd, "hr": s_hr, "distance": s_dist}
            weighted  = 0.40*s_blocks + 0.25*s_struct + 0.10*s_recov + 0.10*s_wucd + 0.10*s_hr + 0.05*s_dist
        else:
            # Senza WU/CD: ridistribuisce il peso su blocchi veloci e recuperi
            s_struct = s_recov
            subscores = {"fast_blocks": s_blocks, "recoveries": s_recov, "hr": s_hr, "distance": s_dist}
            weighted  = 0.55*s_blocks + 0.25*s_recov + 0.12*s_hr + 0.08*s_dist
    else:
        s_hr   = score_hr(activity.get("avg_hr"), plan.get("hr_min") or 140, plan.get("hr_max") or 165, flags, notes)
        s_pace = score_pace(activity.get("avg_pace_s_km"), plan.get("pace_min_s"), plan.get("pace_max_s"), flags, notes)
        s_dist = score_distance(activity.get("distance_km",0), plan.get("distance_km"), flags, notes)
        subscores = {"hr": s_hr, "pace": s_pace, "distance": s_dist}
        weighted  = 0.33*s_hr + 0.33*s_pace + 0.34*s_dist
        fast_laps = []

    subscores = {k: round(v,2) for k,v in subscores.items()}
    final, cap_reason = apply_caps(weighted, plan, activity, fast_laps, flags)
    if cap_reason:
        notes.append(f"Cap: {cap_reason}.")

    return {
        "score":       final,
        "subscores":   subscores,
        "flags":       list(set(flags)),
        "notes":       notes,
        "cap_applied": cap_reason,
    }

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 5 — GARMIN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_pace(spk):
    if not spk or spk <= 0: return "---"
    return f"{int(spk//60)}:{int(spk%60):02d}"

def activity_date(act):
    raw = act.get("startTimeLocal") or act.get("startTimeGMT","")
    return raw[:10] if raw else ""

def parse_laps(laps_raw):
    parsed = []
    for i, lap in enumerate(laps_raw or [], start=1):
        dist = lap.get("distance") or 0
        dur  = lap.get("duration") or lap.get("elapsedDuration") or 0
        hr   = lap.get("averageHR") or lap.get("averageHeartRate")
        pace = (dur/(dist/1000)) if dist > 50 else None
        parsed.append({
            "lap_index":     i,
            "distance_m":    round(dist),
            "duration_s":    round(dur),
            "avg_pace_s_km": round(pace) if pace else None,
            "avg_pace_fmt":  fmt_pace(pace),
            "avg_hr":        round(hr) if hr else None,
        })
    return parsed

def extract_gear(act_detail):
    """Estrae nome e km scarpa dai dettagli attivita Garmin."""
    if not act_detail:
        return None, None
    # Il gear sta in metadataDTO.associatedWorkoutId o in activityDetail
    # Garmin lo mette in act_detail come lista 'metadataDTO' o 'gear'
    gear = act_detail.get("metadataDTO", {}).get("associatedCourseId")  # non questo
    # Percorso corretto: act_detail["activityDetail"]["measurements"] no
    # Garmin usa: act_detail -> "gear" lista oppure dentro "metadataDTO"
    gear_list = act_detail.get("gear") or []
    if not gear_list:
        # Prova dentro metadataDTO
        meta = act_detail.get("metadataDTO") or {}
        gear_list = meta.get("gear") or []
    if gear_list:
        g = gear_list[0] if isinstance(gear_list, list) else gear_list
        name = g.get("displayName") or g.get("customMakeModel") or g.get("makeModel")
        km   = g.get("totalDistanceMeters")
        km   = round(km/1000, 0) if km else None
        return name, km
    return None, None

def build_activity(act, laps_raw, gear_name=None, gear_km=None):
    dist = act.get("distance") or 0
    dur  = act.get("duration") or act.get("movingDuration") or 0
    hr   = act.get("averageHR") or act.get("averageHeartRate")
    pace = (dur/(dist/1000)) if dist > 50 else None
    return {
        "garmin_id":      act.get("activityId"),
        "date":           activity_date(act),
        "start_time":     act.get("startTimeLocal","")[:19],
        "name":           act.get("activityName","Corsa"),
        "type":           act.get("activityType",{}).get("typeKey","running"),
        "distance_m":     round(dist),
        "distance_km":    round(dist/1000, 2),
        "duration_s":     round(dur),
        "avg_hr":         round(hr) if hr else None,
        "avg_pace_s_km":  round(pace) if pace else None,
        "avg_pace_fmt":   fmt_pace(pace),
        "calories":       act.get("calories"),
        "elevation_gain": act.get("elevationGain"),
        "gear_name":      gear_name,
        "gear_km":        gear_km,
        "laps":           parse_laps(laps_raw),
        "fetched_at":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score":          None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 5b — STRAVA GEAR
# ══════════════════════════════════════════════════════════════════════════════

def get_strava_token():
    """Ottieni access token Strava tramite refresh token."""
    import urllib.request, urllib.parse
    client_id     = os.environ.get("STRAVA_CLIENT_ID","").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET","").strip()
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN","").strip()
    if not all([client_id, client_secret, refresh_token]):
        return None
    try:
        import urllib.request, urllib.parse, json as _json
        data = urllib.parse.urlencode({
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        }).encode()
        req  = urllib.request.Request("https://www.strava.com/oauth/token", data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        return _json.loads(resp.read()).get("access_token")
    except Exception as e:
        log.warning("Strava token fallito: %s", e)
        return None

def fetch_strava_gear(token):
    """Scarica lista attivita Strava con gear_id e abbina per data."""
    if not token:
        return {}
    try:
        import urllib.request, json as _json
        url = "https://www.strava.com/api/v3/athlete/activities?per_page=200"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer "+token})
        resp = urllib.request.urlopen(req, timeout=15)
        acts = _json.loads(resp.read())
        # Indice data -> gear_id
        gear_by_date = {}
        gear_names   = {}
        for a in acts:
            date = (a.get("start_date_local") or "")[:10]
            gid  = a.get("gear_id")
            if date and gid:
                gear_by_date[date] = gid
        return gear_by_date, gear_names
    except Exception as e:
        log.warning("Strava activities fallito: %s", e)
        return {}, {}

def fetch_strava_gear_name(token, gear_id):
    """Recupera nome scarpa da Strava."""
    if not token or not gear_id:
        return None
    try:
        import urllib.request, json as _json
        url = f"https://www.strava.com/api/v3/gear/{gear_id}"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer "+token})
        resp = urllib.request.urlopen(req, timeout=10)
        data = _json.loads(resp.read())
        return data.get("name") or data.get("description")
    except Exception as e:
        log.warning("Strava gear name fallito per %s: %s", gear_id, e)
        return None

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 6 — GIT PUSH
# ══════════════════════════════════════════════════════════════════════════════

def git_push(repo_path):
    try:
        subprocess.run(["git","-C",repo_path,"add","data/activities.json"], check=True)
        r = subprocess.run(["git","-C",repo_path,"diff","--cached","--quiet"], capture_output=True)
        if r.returncode == 0:
            log.info("Nessuna modifica da committare.")
            return
        subprocess.run(["git","-C",repo_path,"commit","-m",
                        "chore: aggiorna activities.json [skip ci]"], check=True)
        subprocess.run(["git","-C",repo_path,"push","origin","main"], check=True)
        log.info("Push su GitHub completato.")
    except subprocess.CalledProcessError as e:
        log.error("Errore git: %s", e)

# ══════════════════════════════════════════════════════════════════════════════
# SEZIONE 7 — MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    load_env()
    email     = os.environ.get("GARMIN_EMAIL","").strip()
    password  = os.environ.get("GARMIN_PASSWORD","").strip()
    repo_path = os.environ.get("REPO_PATH","").strip()

    if not email or not password:
        log.error("GARMIN_EMAIL e GARMIN_PASSWORD mancano nel .env")
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

    seen_ids = {a["garmin_id"] for a in data.get("activities",[])}
    log.info("Attivita gia salvate: %d", len(seen_ids))

    # Carica piano ICS
    ics_path = Path(repo_path) / "piano.ics"
    plan_index = {}
    if ics_path.exists():
        try:
            events = parse_ics(ics_path)
            # Aggiungi struttura parsata agli eventi qualita
            for e in events:
                if e["type"] == "qualita" and e.get("struttura"):
                    e["struttura_parsed"] = parse_struttura(e["struttura"])
                else:
                    e["struttura_parsed"] = {}
            plan_index = build_plan_index(events)
            log.info("Piano ICS caricato: %d eventi", len(events))
        except Exception as ex:
            log.warning("Errore parsing ICS: %s", ex)
    else:
        log.warning("piano.ics non trovato — scoring disabilitato.")

    # Login Garmin
    try:
        from garminconnect import Garmin
    except ImportError:
        log.error("pip install garminconnect")
        sys.exit(1)

    log.info("Login Garmin Connect come %s...", email)
    try:
        client = Garmin(email, password)
        client.login()
        log.info("Login riuscito.")
    except Exception as e:
        log.error("Login fallito: %s", e)
        sys.exit(1)

    log.info("Scarico le ultime %d attivita...", FETCH_LIMIT)
    try:
        activities = client.get_activities(0, FETCH_LIMIT)
    except Exception as e:
        log.error("Errore recupero attivita: %s", e)
        sys.exit(1)

    new_acts = [
        a for a in activities
        if a.get("activityType",{}).get("typeKey","") in RUNNING_TYPES
        and a.get("activityId") not in seen_ids
    ]
    log.info("Nuove attivita running: %d", len(new_acts))

    if not new_acts:
        log.info("Nessuna nuova attivita — procedo con backfill gear se necessario.")

    added = 0
    for act in new_acts:
        act_id   = act.get("activityId")
        act_date = activity_date(act)
        log.info("  Processo %s (%s)...", act_id, act_date)

        # Lap
        try:
            laps_raw = client.get_activity_splits(act_id).get("lapDTOs",[])
        except Exception as e:
            log.warning("    Lap non disponibili: %s", e)
            laps_raw = []

        # Gear
        gear_name = gear_km = None
        try:
            act_detail = client.get_activity(act_id)
            gear_name, gear_km = extract_gear(act_detail)
            if gear_name:
                log.info("    Gear: %s (%s km totali)", gear_name, gear_km)
        except Exception as e:
            log.warning("    Gear non disponibile: %s", e)

        record = build_activity(act, laps_raw, gear_name, gear_km)

        # Scoring
        plan = plan_index.get(act_date)
        if plan:
            try:
                sc = auto_score(plan, record)
                if sc:
                    record["score"] = sc
                    log.info("    Voto: %s | Flag: %s", sc["score"], sc["flags"])
            except Exception as e:
                log.warning("    Errore scoring: %s", e)

        data["activities"].append(record)
        added += 1
        time.sleep(1.5)

    # Ricalcola scoring per tutte le attivita (forza aggiornamento formato)
    for record in data.get("activities", []):
        sv = record.get("score")
        # Ricalcola se score mancante O se non ha subscores (vecchio formato)
        if sv is None or not isinstance(sv, dict) or "subscores" not in sv:
            record["score"] = None  # reset
        if record.get("score") is None:
            plan = plan_index.get(record.get("date",""))
            if plan:
                try:
                    sc = auto_score(plan, record)
                    if sc:
                        record["score"] = sc
                        log.info("  Backfill voto %s: %s", record["date"], sc["score"])
                except Exception as e:
                    log.warning("  Errore backfill scoring %s: %s", record.get("date"), e)

    # Backfill gear da Strava per attivita senza gear
    no_gear = [r for r in data.get("activities", []) if r.get("gear_name") is None]
    if no_gear:
        log.info("Recupero gear da Strava per %d attivita...", len(no_gear))
        strava_token = get_strava_token()
        if strava_token:
            gear_by_date, _ = fetch_strava_gear(strava_token)
            gear_name_cache = {}
            for record in no_gear:
                date   = record.get("date","")
                gear_id = gear_by_date.get(date)
                if gear_id:
                    if gear_id not in gear_name_cache:
                        gear_name_cache[gear_id] = fetch_strava_gear_name(strava_token, gear_id)
                    gear_name = gear_name_cache[gear_id]
                    record["gear_name"] = gear_name
                    record["gear_km"]   = None
                    log.info("  Gear %s: %s", date, gear_name)
                else:
                    record["gear_name"] = None
                    record["gear_km"]   = None
        else:
            log.warning("Token Strava non disponibile — gear non recuperato.")

    data["activities"].sort(key=lambda x: x.get("start_time",""), reverse=True)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(output_file,"w",encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    log.info("Aggiunte %d nuove attivita.", added)
    git_push(repo_path)

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Garmin Sync + Scoring -- %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 60)
    main()
