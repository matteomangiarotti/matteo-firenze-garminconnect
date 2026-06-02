#!/usr/bin/env python3
"""
scarica_garmin_actions.py
--------------------------
Versione per GitHub Actions — usa token OAuth invece di email/password.
Evita il blocco 429 di Garmin su IP esterni.

Secrets richiesti:
  GARMIN_DI_TOKEN         — JWT access token
  GARMIN_DI_REFRESH_TOKEN — refresh token
  STRAVA_CLIENT_ID        — per gear
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN
"""

import os, sys, json, time, logging, subprocess, re, math, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

FETCH_LIMIT   = 10
RUNNING_TYPES = {"running", "trail_running", "treadmill_running"}
REPO_ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR      = REPO_ROOT / "data"
OUTPUT_FILE   = DATA_DIR / "activities.json"

# ── Garmin API con token ──────────────────────────────────────────────────────

def get_garmin_session(di_token, di_refresh_token):
    """Crea sessione HTTP con token Garmin senza login."""
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": "GCM-iOS-5.7.2.1 (com.garmin.connect.mobile; build:5.7.2.1; iOS 17.0)",
        "Authorization": f"Bearer {di_token}",
        "DI-Backend": "connectapi.garmin.com",
        "NK": "NT",
        "X-app-ver": "5.25.0.30a",
    })
    s.cookies.set("DI-Backend", "connectapi.garmin.com", domain=".garmin.com")
    return s

def fetch_garmin_activities(session, limit=10):
    """Scarica lista attivita recenti da Garmin."""
    import requests
    urls = [
        f"https://connectapi.garmin.com/activitylist-service/activities/search/activities?start=0&limit={limit}",
        f"https://connectapi.garmin.com/activity-service/activity/search/activities?start=0&limit={limit}",
        f"https://connect.garmin.com/proxy/activitylist-service/activities/search/activities?start=0&limit={limit}",
        f"https://connect.garmin.com/activitylist-service/activities/search/activities?start=0&limit={limit}",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=30)
            log.info("GET %s -> %s | body: %s", url.split("/")[-1].split("?")[0], r.status_code, r.text[:100])
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    return data
                elif isinstance(data, dict):
                    for k in ["activityList","activities","data"]:
                        if k in data and isinstance(data[k], list) and data[k]:
                            return data[k]
                    log.info("  Dict keys: %s", list(data.keys()))
        except Exception as e:
            log.warning("  Errore: %s", e)
    return []

def fetch_garmin_laps(session, act_id):
    """Scarica i lap di un'attivita."""
    urls = [
        f"https://connectapi.garmin.com/activity-service/activity/{act_id}/splits",
        f"https://connect.garmin.com/proxy/activity-service/activity/{act_id}/splits",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json().get("lapDTOs", [])
        except Exception as e:
            log.warning("  Lap %s errore: %s", act_id, e)
    return []

# ── Strava gear ───────────────────────────────────────────────────────────────

def get_strava_token():
    client_id     = os.environ.get("STRAVA_CLIENT_ID","").strip()
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET","").strip()
    refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN","").strip()
    if not all([client_id, client_secret, refresh_token]):
        return None
    try:
        data = urllib.parse.urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        }).encode()
        req  = urllib.request.Request("https://www.strava.com/oauth/token", data=data, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()).get("access_token")
    except Exception as e:
        log.warning("Strava token: %s", e)
        return None

def fetch_strava_gear_map(token):
    if not token: return {}
    try:
        req  = urllib.request.Request(
            "https://www.strava.com/api/v3/athlete/activities?per_page=50",
            headers={"Authorization": "Bearer "+token}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        acts = json.loads(resp.read())
        return {(a.get("start_date_local") or "")[:10]: a.get("gear_id") for a in acts if a.get("gear_id")}
    except Exception as e:
        log.warning("Strava activities: %s", e)
        return {}

def fetch_strava_gear_name(token, gear_id):
    if not token or not gear_id: return None
    try:
        req  = urllib.request.Request(
            f"https://www.strava.com/api/v3/gear/{gear_id}",
            headers={"Authorization": "Bearer "+token}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("name") or data.get("description")
    except Exception as e:
        log.warning("Strava gear: %s", e)
        return None

# ── Helpers comuni ────────────────────────────────────────────────────────────

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
            "lap_index": i, "distance_m": round(dist), "duration_s": round(dur),
            "avg_pace_s_km": round(pace) if pace else None,
            "avg_pace_fmt": fmt_pace(pace), "avg_hr": round(hr) if hr else None,
        })
    return parsed

def build_activity(act, laps_raw, gear_name=None):
    dist = act.get("distance") or 0
    dur  = act.get("duration") or act.get("movingDuration") or 0
    hr   = act.get("averageHR") or act.get("averageHeartRate")
    pace = (dur/(dist/1000)) if dist > 50 else None
    return {
        "garmin_id":     act.get("activityId"),
        "date":          activity_date(act),
        "start_time":    act.get("startTimeLocal","")[:19],
        "name":          act.get("activityName","Corsa"),
        "type":          act.get("activityType",{}).get("typeKey","running"),
        "distance_m":    round(dist), "distance_km": round(dist/1000, 2),
        "duration_s":    round(dur), "avg_hr": round(hr) if hr else None,
        "avg_pace_s_km": round(pace) if pace else None, "avg_pace_fmt": fmt_pace(pace),
        "calories":      act.get("calories"), "elevation_gain": act.get("elevationGain"),
        "gear_name":     gear_name, "gear_km": None,
        "laps":          parse_laps(laps_raw),
        "fetched_at":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score":         None,
    }

# ── ICS parser (semplificato) ─────────────────────────────────────────────────

def parse_ics(path):
    text = Path(path).read_text(encoding="utf-8")
    text = re.sub(r'\r?\n[ \t]', '', text)
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        def get(k):
            m = re.search(k+r'[^:]*:([^\r\n]+)', block)
            return m.group(1).strip().replace(r'\n','\n').replace(r'\,',',') if m else ""
        def field(desc, key):
            m = re.search(key+r':\s*([^\n\\]+)', desc)
            return m.group(1).strip() if m else ""
        dtraw = get("DTSTART")
        if not dtraw: continue
        dtclean = re.sub(r'T.*$','',dtraw)
        if len(dtclean) < 8: continue
        date_str = dtclean[:4]+"-"+dtclean[4:6]+"-"+dtclean[6:8]
        summary  = get("SUMMARY")
        desc     = get("DESCRIPTION")
        tipo     = field(desc,"Tipo").lower()
        ritmo    = field(desc,"Ritmo")
        fc_raw   = field(desc,"FC")
        struttura= field(desc,"Struttura")
        wtype = "rest" if "riposo" in summary.lower() else \
                "recovery" if "recovery" in tipo or "recupero" in tipo else \
                "lungo" if "lungo" in tipo else \
                "qualita" if any(x in tipo for x in ["qualit","soglia","medio","ripetute","progressivo"]) else "easy"
        pace_min_s = pace_max_s = None
        pm = re.search(r'(\d):(\d{2})[–\-](\d):(\d{2})', ritmo)
        if pm:
            pace_min_s = int(pm.group(1))*60+int(pm.group(2))
            pace_max_s = int(pm.group(3))*60+int(pm.group(4))
        hr_min = hr_max = None
        hm = re.search(r'(\d{2,3})[–\-](\d{2,3})\s*bpm', fc_raw)
        if hm: hr_min,hr_max = int(hm.group(1)),int(hm.group(2))
        dist_km = None
        dm = re.search(r'(\d+)\s*km', summary, re.IGNORECASE)
        if dm: dist_km = float(dm.group(1))
        st = parse_struttura(struttura) if struttura else {}
        events.append({"date":date_str,"summary":summary,"type":wtype,
                        "pace_min_s":pace_min_s,"pace_max_s":pace_max_s,
                        "hr_min":hr_min,"hr_max":hr_max,"distance_km":dist_km,
                        "struttura_parsed":st})
    events.sort(key=lambda x: x["date"])
    return {e["date"]:e for e in events}

def parse_struttura(raw):
    if not raw: return {}
    s = raw.strip()
    r = {"wu_min":0.0,"cd_min":0.0,"reps":0,"rep_min":None,"rep_km":None,
         "rec_min":None,"rec_km":None,"is_prog":False}
    m = re.search(r'WU(\d+(?:\.\d+)?)(km|m)?', s, re.I)
    if m:
        v,u = float(m.group(1)),(m.group(2) or "").lower()
        r["wu_min"] = v/(5.5/60) if u=="km" else (v/1000)/(5.5/60) if u=="m" else v
    m = re.search(r'CD(\d+(?:\.\d+)?)(km|m)?', s, re.I)
    if m:
        v,u = float(m.group(1)),(m.group(2) or "").lower()
        r["cd_min"] = v/(5.5/60) if u=="km" else (v/1000)/(5.5/60) if u=="m" else v
    m = re.search(r'PROG(\d+(?:\.\d+)?)(km|m|\'|min)?', s, re.I)
    if m:
        r["is_prog"]=True; v,u=float(m.group(1)),(m.group(2) or "").lower().replace("'","")
        if u=="km": r["rep_km"]=v
        elif u=="m": r["rep_km"]=v/1000
        else: r["rep_min"]=v
        r["reps"]=1; return r
    m = re.search(r'(\d+)x(\d+(?:\.\d+)?)(km|m|\'|min|s)?', s, re.I)
    if m:
        r["reps"]=int(m.group(1)); v,u=float(m.group(2)),(m.group(3) or "").lower().replace("'","").replace("\u2019","")
        if u=="km": r["rep_km"]=v
        elif u=="m": r["rep_km"]=v/1000
        elif u=="s": r["rep_min"]=v/60
        else: r["rep_min"]=v
    m = re.search(r'REC(\d+(?:\.\d+)?)(km|m|s)?', s, re.I)
    if m:
        v,u=float(m.group(1)),(m.group(2) or "").lower()
        if u=="km": r["rec_km"]=v
        elif u=="m": r["rec_km"]=v/1000
        elif u=="s": r["rec_min"]=v/60
        else: r["rec_min"]=v
    return r

# ── Scoring (stesso del PC) ───────────────────────────────────────────────────

def clamp(v,lo,hi): return max(lo,min(hi,v))

def score_hr(hr,hrn,hrx,flags,notes):
    if not hr: return 7.0
    du=max(0,hr-hrx); dd=max(0,hrn-hr)
    if du==0 and dd==0: return 10.0
    elif du>0:
        s=clamp(10-du*0.35,1,10)
        if du>=10: flags.append("HR_HIGH"); notes.append(f"FC {hr:.0f} bpm supera target di {du:.0f} bpm.")
        return round(s,2)
    else:
        s=clamp(10-dd*0.08,7,10)
        if dd>=7: flags.append("HR_LOW"); notes.append(f"FC {hr:.0f} bpm sotto target di {dd:.0f} bpm.")
        return round(s,2)

def score_pace(pace,pn,px,flags,notes,wt="easy"):
    if not pace or not pn or not px: return 7.0
    df=max(0,pn-pace); ds=max(0,pace-px)
    if df==0 and ds==0: return 10.0
    elif df>0:
        m=0.30 if wt in("recovery","lungo") else 0.20
        s=clamp(10-df*m,3,10)
        if df>=20: flags.append("TOO_FAST"); notes.append(f"Passo {fmt_pace(pace)} piu veloce del target.")
        return round(s,2)
    else: return round(clamp(10-ds*0.20,4,10),2)

def score_distance(actual,target,flags,notes):
    if not target or target<=0: return 7.0
    ratio=actual/target
    if ratio>=0.97: s=10.0
    elif ratio>=0.90: s=7.0+(ratio-0.90)*30
    elif ratio>=0.70: s=4.0+(ratio-0.70)*15
    else:
        s=max(1.0,ratio*6); flags.append("INCOMPLETE")
        notes.append(f"Completato {ratio*100:.0f}% della distanza.")
    return round(clamp(s,1,10),2)

def score_cardiac_drift(laps,flags,notes):
    if len(laps)<4: return 8.0
    mid=len(laps)//2
    h1=[l.get("avg_hr") for l in laps[:mid] if l.get("avg_hr")]
    h2=[l.get("avg_hr") for l in laps[mid:] if l.get("avg_hr")]
    if not h1 or not h2: return 8.0
    drift=sum(h2)/len(h2)-sum(h1)/len(h1)
    if drift<=5: return 10.0
    elif drift<=8: return 8.0
    elif drift<=12: flags.append("CARDIAC_DRIFT"); notes.append(f"Deriva cardiaca {drift:.0f} bpm."); return 6.0
    else: flags.append("CARDIAC_DRIFT"); notes.append(f"Deriva cardiaca elevata {drift:.0f} bpm."); return 4.0

def segment_laps(laps,plan):
    if not laps: return [],[],[],[]
    thr=(plan.get("pace_min_s") or 280)+15
    fast=[l for l in laps if l.get("avg_pace_s_km") and l.get("distance_m",0)>100 and l["avg_pace_s_km"]<thr]
    slow=[l for l in laps if l.get("avg_pace_s_km") and l.get("distance_m",0)>100 and l["avg_pace_s_km"]>=thr]
    fi=laps.index(fast[0]) if fast else len(laps)
    li=laps.index(fast[-1]) if fast else 0
    wu=[l for l in laps[:fi] if l.get("avg_pace_s_km",0)>=thr]
    cd=[l for l in laps[li+1:] if l.get("avg_pace_s_km",0)>=thr]
    return fast,slow,wu,cd

def score_fast_blocks(fast,plan,st,flags,notes):
    if not fast: flags.append("NO_FAST_BLOCKS"); notes.append("Nessun blocco veloce."); return 2.0
    tm=((plan.get("pace_min_s") or 260)+(plan.get("pace_max_s") or 280))/2
    tr=st.get("reps") or len(fast)
    paces=[l.get("avg_pace_s_km") for l in fast if l.get("avg_pace_s_km")]
    if not paces: return 3.0
    sv=sum(clamp(10-abs(p-tm)*0.30,1,10) for p in paces)/len(paces)
    if len(paces)>1:
        mp=sum(paces)/len(paces)
        std=math.sqrt(sum((p-mp)**2 for p in paces)/len(paces))
        sr=clamp(10-std*0.4,1,10)
    else: sr=8.0
    pf=0
    if len(paces)>=4:
        mid=len(paces)//2; p1=sum(paces[:mid])/mid; p2=sum(paces[mid:])/(len(paces)-mid)
        if p2-p1>10: flags.append("FADE"); notes.append(f"Calo progressivo: {fmt_pace(p1)} → {fmt_pace(p2)}."); pf=1.5
    if tr>0 and len(fast)/tr<0.70: notes.append(f"Completate {len(fast)} su {tr} ripetute.")
    return round(clamp(0.70*sv+0.30*sr-pf,1,10),2)

def score_recoveries(slow,plan,st,flags,notes):
    if not slow:
        if (st.get("reps") or 0)>1: flags.append("SHORT_RECOVERY"); notes.append("Recuperi non rilevati."); return 3.0
        return 7.0
    tr=(st.get("rec_min") or 2.0)*60
    scores=[]
    for lap in slow:
        dur=lap.get("duration_s") or 0; pace=lap.get("avg_pace_s_km") or 999
        ratio=dur/tr if tr>0 else 1.0
        sd=10.0 if ratio>=0.80 else 6.0 if ratio>=0.50 else 3.0
        if ratio<0.50: flags.append("SHORT_RECOVERY")
        si=clamp(10-max(0,480-pace)*0.05,1,10)
        scores.append(0.50*sd+0.50*si)
    return round(clamp(sum(scores)/len(scores),1,10),2)

def score_wucd(wu,cd,plan,st,flags,notes):
    twu=(st.get("wu_min") or 10)*60; tcd=(st.get("cd_min") or 10)*60
    wdur=sum(l.get("duration_s",0) for l in wu); cdur=sum(l.get("duration_s",0) for l in cd)
    swu=10.0 if wdur>=twu*0.80 else 6.0 if wdur>=twu*0.40 else 2.0
    scd=10.0 if cdur>=tcd*0.80 else 6.0 if cdur>=tcd*0.40 else 3.0
    if swu<4: flags.append("MISSING_WARMUP"); notes.append("Warm-up assente o breve.")
    if scd<4: flags.append("MISSING_COOLDOWN"); notes.append("Cooldown assente o breve.")
    return round((swu+scd)/2,2)

def apply_caps(score,plan,act,fast,flags):
    cap,reason=10.0,None
    hr=act.get("avg_hr") or 0; dist=act.get("distance_km") or 0
    tdist=plan.get("distance_km") or 0; wt=plan.get("type","")
    tr=(plan.get("struttura_parsed") or {}).get("reps") or 0
    if wt=="recovery":
        if hr>(plan.get("hr_max") or 147)+15 and 6.5<cap: cap,reason=6.5,"Recovery: FC alta"
        if plan.get("pace_min_s") and (act.get("avg_pace_s_km") or 999)<plan["pace_min_s"]-20 and 7.0<cap: cap,reason=7.0,"Recovery troppo veloce"
    elif wt=="qualita":
        if len(fast)<2 and 6.0<cap: cap,reason=6.0,"Qualita: nessun blocco veloce"
        if tr>0 and len(fast)/tr<0.70 and 7.0<cap: cap,reason=7.0,"Qualita: poche ripetute"
        if "MISSING_WARMUP" in flags and "MISSING_COOLDOWN" in flags:
            st=plan.get("struttura_parsed") or {}
            if ((st.get("wu_min") or 0)>0 or (st.get("cd_min") or 0)>0) and 8.5<cap:
                cap,reason=8.5,"Qualita: WU/CD assenti"
    elif wt=="lungo":
        if tdist>0 and dist/tdist<0.70 and 6.0<cap: cap,reason=6.0,"Lungo: distanza bassa"
        if hr>(plan.get("hr_max") or 160)+10 and 7.5<cap: cap,reason=7.5,"Lungo: FC alta"
    return round(min(score,cap),1),reason

def auto_score(plan,act):
    if not plan or plan.get("type")=="rest": return None
    flags,notes=[],[]
    laps=act.get("laps") or []; st=plan.get("struttura_parsed") or {}; wt=plan.get("type","easy")
    has_wu=(st.get("wu_min") or 0)>0; has_cd=(st.get("cd_min") or 0)>0
    if wt=="recovery":
        sh=score_hr(act.get("avg_hr"),plan.get("hr_min") or 129,plan.get("hr_max") or 147,flags,notes)
        sp=score_pace(act.get("avg_pace_s_km"),plan.get("pace_min_s"),plan.get("pace_max_s"),flags,notes,"recovery")
        sd=score_distance(act.get("distance_km",0),plan.get("distance_km"),flags,notes)
        subscores={"hr":sh,"pace":sp,"distance":sd}; w=0.50*sh+0.20*sp+0.30*sd; fast=[]
    elif wt=="easy":
        sh=score_hr(act.get("avg_hr"),plan.get("hr_min") or 147,plan.get("hr_max") or 160,flags,notes)
        sp=score_pace(act.get("avg_pace_s_km"),plan.get("pace_min_s"),plan.get("pace_max_s"),flags,notes,"easy")
        sd=score_distance(act.get("distance_km",0),plan.get("distance_km"),flags,notes)
        subscores={"hr":sh,"pace":sp,"distance":sd}; w=0.35*sh+0.35*sp+0.30*sd; fast=[]
    elif wt=="lungo":
        sh=score_hr(act.get("avg_hr"),plan.get("hr_min") or 147,plan.get("hr_max") or 160,flags,notes)
        sp=score_pace(act.get("avg_pace_s_km"),plan.get("pace_min_s"),plan.get("pace_max_s"),flags,notes,"lungo")
        sd=score_distance(act.get("distance_km",0),plan.get("distance_km"),flags,notes)
        sdr=score_cardiac_drift(laps,flags,notes); shd=(sh+sdr)/2
        if has_wu or has_cd:
            wu,_,wul,cdl=segment_laps(laps,plan); swucd=score_wucd(wul,cdl,plan,st,flags,notes)
            subscores={"hr_drift":shd,"pace":sp,"distance":sd,"wu_cd":swucd}; w=0.22*shd+0.23*sp+0.35*sd+0.20*swucd
        else:
            subscores={"hr_drift":shd,"pace":sp,"distance":sd}; w=0.25*shd+0.25*sp+0.50*sd
        fast=[]
    elif wt=="qualita":
        fast,slow,wul,cdl=segment_laps(laps,plan)
        sb=score_fast_blocks(fast,plan,st,flags,notes); sr=score_recoveries(slow,plan,st,flags,notes)
        sh=score_hr(act.get("avg_hr"),plan.get("hr_min") or 161,plan.get("hr_max") or 174,flags,notes)
        sd=score_distance(act.get("distance_km",0),plan.get("distance_km"),flags,notes)
        if has_wu or has_cd:
            swucd=score_wucd(wul,cdl,plan,st,flags,notes); sst=(sr+swucd)/2
            subscores={"fast_blocks":sb,"structure":sst,"recoveries":sr,"wu_cd":swucd,"hr":sh,"distance":sd}
            w=0.40*sb+0.25*sst+0.10*sr+0.10*swucd+0.10*sh+0.05*sd
        else:
            subscores={"fast_blocks":sb,"recoveries":sr,"hr":sh,"distance":sd}; w=0.55*sb+0.25*sr+0.12*sh+0.08*sd
    else:
        sh=score_hr(act.get("avg_hr"),plan.get("hr_min") or 140,plan.get("hr_max") or 165,flags,notes)
        sp=score_pace(act.get("avg_pace_s_km"),plan.get("pace_min_s"),plan.get("pace_max_s"),flags,notes)
        sd=score_distance(act.get("distance_km",0),plan.get("distance_km"),flags,notes)
        subscores={"hr":sh,"pace":sp,"distance":sd}; w=0.33*sh+0.33*sp+0.34*sd; fast=[]
    subscores={k:round(v,2) for k,v in subscores.items()}
    final,cap_reason=apply_caps(w,plan,act,fast,flags)
    if cap_reason: notes.append(f"Cap: {cap_reason}.")
    return {"score":final,"subscores":subscores,"flags":list(set(flags)),"notes":notes,"cap_applied":cap_reason}

# ── Git push ──────────────────────────────────────────────────────────────────

def git_push():
    try:
        subprocess.run(["git","add","data/activities.json"], check=True, cwd=REPO_ROOT)
        r=subprocess.run(["git","diff","--cached","--quiet"], capture_output=True, cwd=REPO_ROOT)
        if r.returncode==0: log.info("Nessuna modifica."); return
        subprocess.run(["git","commit","-m","chore: aggiorna activities.json [skip ci]"], check=True, cwd=REPO_ROOT)
        subprocess.run(["git","push","origin","main"], check=True, cwd=REPO_ROOT)
        log.info("Push completato.")
    except subprocess.CalledProcessError as e:
        log.error("Git error: %s", e)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    di_token         = os.environ.get("GARMIN_DI_TOKEN","").strip()
    di_refresh_token = os.environ.get("GARMIN_DI_REFRESH_TOKEN","").strip()
    if not di_token or not di_refresh_token:
        raise SystemExit("Imposta GARMIN_DI_TOKEN e GARMIN_DI_REFRESH_TOKEN")

    DATA_DIR.mkdir(exist_ok=True)
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE,encoding="utf-8") as f: data=json.load(f)
    else:
        data={"last_updated":None,"activities":[]}

    seen_ids={a["garmin_id"] for a in data.get("activities",[])}
    log.info("Attivita gia salvate: %d", len(seen_ids))

    # Piano ICS
    ics_path=REPO_ROOT/"piano.ics"
    plan_index={}
    if ics_path.exists():
        plan_index=parse_ics(ics_path)
        log.info("Piano ICS: %d eventi", len(plan_index))

    # Garmin session con token
    import requests
    session=get_garmin_session(di_token, di_refresh_token)

    log.info("Scarico attivita da Garmin...")
    activities=fetch_garmin_activities(session, FETCH_LIMIT)
    log.info("Attivita ricevute: %d", len(activities))

    new_acts=[a for a in activities
              if a.get("activityType",{}).get("typeKey","") in RUNNING_TYPES
              and a.get("activityId") not in seen_ids]
    log.info("Nuove running: %d", len(new_acts))

    if not new_acts:
        data["last_updated"]=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(OUTPUT_FILE,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
        log.info("Nessuna novita.")
        return

    # Strava gear
    strava_token=get_strava_token()
    gear_by_date=fetch_strava_gear_map(strava_token) if strava_token else {}
    gear_name_cache={}

    added=0
    for act in new_acts:
        act_id=act.get("activityId"); act_date=activity_date(act)
        log.info("  Processo %s (%s)...", act_id, act_date)
        laps_raw=fetch_garmin_laps(session, act_id)
        gear_id=gear_by_date.get(act_date)
        if gear_id:
            if gear_id not in gear_name_cache:
                gear_name_cache[gear_id]=fetch_strava_gear_name(strava_token,gear_id)
            gear_name=gear_name_cache[gear_id]
        else:
            gear_name=None
        record=build_activity(act,laps_raw,gear_name)
        plan=plan_index.get(act_date)
        if plan:
            sc=auto_score(plan,record)
            if sc:
                record["score"]=sc
                log.info("    Voto: %s | Flag: %s", sc["score"], sc["flags"])
        data["activities"].append(record)
        added+=1
        time.sleep(1)

    data["activities"].sort(key=lambda x: x.get("start_time",""),reverse=True)
    data["last_updated"]=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUTPUT_FILE,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
    log.info("Aggiunte %d attivita.", added)
    git_push()

if __name__=="__main__":
    log.info("="*60)
    log.info("Garmin Sync Actions -- %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("="*60)
    main()
