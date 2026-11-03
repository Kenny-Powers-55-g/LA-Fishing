#!/usr/bin/env python3
"""
Louisiana fishing conditions: ingestion worker (prototype)

Pulls public data for each spot, normalizes it, stores history in SQLite and
writes latest.json in the shape the scoring page expects.

Sources
  NOAA CO-OPS   water temp, conductivity (-> salinity), wind, tide predictions
  USGS NWIS     water temp, specific conductance (-> salinity), turbidity
  Open-Meteo    wind, cloud cover, surface pressure (-> 3 hr trend)

Usage (Python 3.9+, standard library only)
  python ingest.py discover     # list what each configured station really offers
  python ingest.py once         # one pull, writes fishing.db + latest.json
  python ingest.py loop 30      # repeat every 30 minutes
  or run "once" from cron every 30 min.

Set CONTACT below. Check each API's terms (Open-Meteo's free tier is for
non-commercial use; USGS is migrating NWIS services, so verify the endpoint).
"""
import json, logging, math, os, sqlite3, sys, time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONTACT = "you@example.com"
APP = "la-fishing-prototype"
DB, OUT = "fishing.db", "latest.json"
STALE_MIN = 180          # readings older than this are treated as missing
UTC = timezone.utc
log = logging.getLogger(APP)

# Water sources are tried in order, per metric. Station IDs were checked
# against NOAA/USGS listings; which products each one offers is NOT assumed:
# run `discover`, then trim this list.
SPOTS = [
    {"id": "calc", "name": "Calcasieu Lake", "lat": 29.88, "lon": -93.30,
     "water": [("coops", "8768094")], "tide": "8768094"},
    {"id": "barat", "name": "Barataria Bay (Grand Isle)", "lat": 29.27, "lon": -89.95,
     "water": [("coops", "8761724")], "tide": "8761724"},
    {"id": "venice", "name": "Venice / Delta Marsh", "lat": 29.28, "lon": -89.35,
     "water": [("coops", "8760721"), ("coops", "8760922")], "tide": "8760922"},
    {"id": "pont", "name": "Lake Pontchartrain", "lat": 30.15, "lon": -90.10,
     "water": [("coops", "8761927"), ("coops", "8761305")], "tide": "8761927"},
    {"id": "atch", "name": "Atchafalaya Basin", "lat": 30.10, "lon": -91.60,
     "water": [("usgs", "07381515")], "tide": None},
    {"id": "verret", "name": "Lake Verret", "lat": 29.95, "lon": -91.15,
     "water": [("usgs", "295610091110000"), ("usgs", "07381515")], "tide": None},
]

# ---------------------------------------------------------------- HTTP
def get_json(url, params=None, tries=3):
    if params:
        url += "?" + urlencode(params)
    for i in range(tries):
        try:
            req = Request(url, headers={"User-Agent": f"{APP} ({CONTACT})"})
            with urlopen(req, timeout=20) as r:
                return json.load(r)
        except HTTPError as e:
            log.warning("HTTP %s for %s", e.code, url)
            if e.code < 500:
                return None
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("fetch failed (%s): %s", e, url)
        time.sleep(2 ** i)
    return None

def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def parse_gmt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)

# ---------------------------------------------------------------- conversions
def f_from_c(c): return c * 9 / 5 + 32

def psu(cond_ms_cm, temp_c):
    """Practical salinity (PSS-78) from conductivity at temp_c, 1 atm."""
    R = cond_ms_cm / 42.914
    rt = 0.6766097 + temp_c * (0.0200564 + temp_c * (1.104259e-4 + temp_c * (-6.9698e-7 + temp_c * 1.0031e-9)))
    Rt = R / rt
    if Rt <= 0:
        return 0.0
    a = [0.008, -0.1692, 25.3851, 14.0941, -7.0261, 2.7081]
    b = [0.0005, -0.0056, -0.0066, -0.0375, 0.0636, -0.0144]
    A = sum(a[i] * Rt ** (i / 2) for i in range(6))
    B = sum(b[i] * Rt ** (i / 2) for i in range(6))
    return max(0.0, A + (temp_c - 15) / (1 + 0.0162 * (temp_c - 15)) * B)

# ---------------------------------------------------------------- CO-OPS
COOPS = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/%s/products.json"

def coops_latest(station, product):
    j = get_json(COOPS, dict(station=station, product=product, date="latest",
                             units="english", time_zone="gmt", format="json", application=APP))
    if not j or not j.get("data"):
        return None
    return j["data"][-1]

def coops_source(station):
    out = {}
    d = coops_latest(station, "water_temperature")
    if d and fnum(d.get("v")) is not None:
        out["temp_f"] = (fnum(d["v"]), parse_gmt(d["t"]), None)
    d = coops_latest(station, "conductivity")            # mS/cm
    if d and fnum(d.get("v")) is not None:
        out["cond_ms"] = (fnum(d["v"]), parse_gmt(d["t"]), None)   # temp-correct later
    d = coops_latest(station, "wind")                    # knots with units=english
    if d and fnum(d.get("s")) is not None:
        out["wind_mph"] = (fnum(d["s"]) * 1.15078, parse_gmt(d["t"]), None)
    return out

def tide_stage(station, now):
    j = get_json(COOPS, dict(station=station, product="predictions", datum="MLLW", interval="hilo",
                             begin_date=(now - timedelta(days=1)).strftime("%Y%m%d"),
                             end_date=(now + timedelta(days=1)).strftime("%Y%m%d"),
                             units="english", time_zone="gmt", format="json", application=APP))
    pts = []
    for p in (j or {}).get("predictions", []):
        v = fnum(p.get("v"))
        if v is not None:
            pts.append((parse_gmt(p["t"]), v, p["type"]))
    prev = [p for p in pts if p[0] <= now]
    nxt = [p for p in pts if p[0] > now]
    if not prev or not nxt:
        return None
    pv, nx = prev[-1], nxt[0]
    near_turn = min(now - pv[0], nx[0] - now) < timedelta(minutes=30)
    weak = abs(nx[1] - pv[1]) < 0.3      # tiny range = effectively no current
    if near_turn or weak:
        return "Slack"
    return "Incoming" if nx[2] == "H" else "Outgoing"

# ---------------------------------------------------------------- USGS
USGS = "https://waterservices.usgs.gov/nwis/iv/"

def usgs_source(site):
    j = get_json(USGS, dict(format="json", sites=site, siteStatus="all",
                            parameterCd="00010,00095,63680"))
    out = {}
    try:
        for ts in j["value"]["timeSeries"]:
            code = ts["variable"]["variableCode"][0]["value"]
            nodata = ts["variable"]["noDataValue"]
            vals = ts["values"][0]["value"]
            if not vals:
                continue
            x, t = fnum(vals[-1]["value"]), datetime.fromisoformat(vals[-1]["dateTime"]).astimezone(UTC)
            if x is None or x == nodata:
                continue
            if code == "00010":
                out["temp_f"] = (f_from_c(x), t, None)
            elif code == "00095":                        # uS/cm at 25 C
                out["cond_ms"] = (x / 1000.0, t, 25.0)
            elif code == "63680":
                out["turb_fnu"] = (x, t, None)
    except (TypeError, KeyError, ValueError, IndexError) as e:
        log.warning("USGS parse problem for %s: %s", site, e)
    return out

# ---------------------------------------------------------------- weather
def weather(lat, lon):
    j = get_json("https://api.open-meteo.com/v1/forecast", dict(
        latitude=lat, longitude=lon, current="wind_speed_10m,cloud_cover,surface_pressure",
        hourly="surface_pressure", past_hours=6, forecast_hours=1,
        wind_speed_unit="mph", timezone="GMT"))
    try:
        cur = j["current"]
        t = datetime.fromisoformat(cur["time"]).replace(tzinfo=UTC)
        target = t - timedelta(hours=3)
        pairs = [(datetime.fromisoformat(a).replace(tzinfo=UTC), p)
                 for a, p in zip(j["hourly"]["time"], j["hourly"]["surface_pressure"]) if p is not None]
        ref = min(pairs, key=lambda x: abs(x[0] - target), default=None)
        delta = None
        if ref and abs(ref[0] - target) <= timedelta(minutes=90):
            delta = cur["surface_pressure"] - ref[1]
        trend = None if delta is None else ("Falling" if delta <= -1.0 else "Rising" if delta >= 1.0 else "Steady")
        return {"time": t, "wind_mph": cur["wind_speed_10m"], "cloud": cur["cloud_cover"],
                "pres": trend, "pres_delta_hpa": delta}
    except (TypeError, KeyError, ValueError):
        return None

def clarity_label(turb, wind, sal):
    if turb is not None:
        return ("Clear" if turb < 10 else "Stained" if turb < 40 else "Muddy"), "measured"
    if wind is not None and wind >= 20:
        return "Muddy", "estimated"
    if sal is not None and sal >= 20 and (wind or 0) < 12:
        return "Clear", "estimated"
    return "Stained", "estimated"

# ---------------------------------------------------------------- collection
def collect(spot, now, cache):
    got, src = {}, {}                      # metric -> (value, time, tref), metric -> source label
    def take(metric, rec, label):
        if metric in got or metric not in rec:
            return
        age = (now - rec[metric][1]).total_seconds() / 60
        if age <= STALE_MIN:
            got[metric], src[metric] = rec[metric], {"source": label, "age_min": round(age)}

    for kind, sid in spot["water"]:
        key = (kind, sid)
        if key not in cache:
            cache[key] = coops_source(sid) if kind == "coops" else usgs_source(sid)
        for m in ("temp_f", "cond_ms", "turb_fnu", "wind_mph"):
            take(m, cache[key], f"{kind}:{sid}")

    wx = cache.setdefault(("wx", spot["id"]), weather(spot["lat"], spot["lon"]))
    if wx:
        age = (now - wx["time"]).total_seconds() / 60
        wsrc = {"source": "open-meteo", "age_min": round(age)}
        if "wind_mph" not in got:
            got["wind_mph"], src["wind_mph"] = (wx["wind_mph"], wx["time"], None), wsrc
        got["cloud"], src["cloud"] = (wx["cloud"], wx["time"], None), wsrc
        if wx["pres"]:
            got["pres"], src["pres"] = (wx["pres"], wx["time"], None), wsrc

    tide = tide_stage(spot["tide"], now) if spot["tide"] else None
    if tide:
        src["tide"] = {"source": f"coops-predictions:{spot['tide']}", "age_min": 0}

    sal = None
    if "cond_ms" in got:
        c, _, tref = got["cond_ms"]
        tc = tref if tref is not None else ((got["temp_f"][0] - 32) * 5 / 9 if "temp_f" in got else None)
        if tc is not None:
            sal = round(psu(c, tc), 1)
            src["sal"] = src.pop("cond_ms")
    turb = got["turb_fnu"][0] if "turb_fnu" in got else None
    wind = got["wind_mph"][0] if "wind_mph" in got else None
    clar, clar_kind = clarity_label(turb, wind, sal)

    cond = {"temp": round(got["temp_f"][0]) if "temp_f" in got else None,
            "wind": round(wind) if wind is not None else None,
            "pres": got["pres"][0] if "pres" in got else None,
            "tide": tide if tide else ("Slack" if not spot["tide"] else None),
            "sal": sal, "clar": clar,
            "cloud": round(got["cloud"][0]) if "cloud" in got else None}
    missing = [k for k, v in cond.items() if v is None]
    return {"id": spot["id"], "name": spot["name"], "updated": now.isoformat(),
            "conditions": cond,
            "quality": {"missing": missing, "sources": src,
                        "estimated": ["clar"] if clar_kind == "estimated" else [],
                        "not_applicable": ["tide"] if not spot["tide"] else [],
                        "pressure_delta_3h_hpa": None if not wx or wx["pres_delta_hpa"] is None else round(wx["pres_delta_hpa"], 1),
                        "completeness": round(1 - len(missing) / len(cond), 2)}}

# ---------------------------------------------------------------- storage / run
def save(results, now):
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS readings(
        id INTEGER PRIMARY KEY, spot TEXT, metric TEXT, value REAL, text TEXT,
        source TEXT, fetched_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix ON readings(spot, metric, fetched_at)")
    for r in results:
        for m, v in r["conditions"].items():
            if v is None:
                continue
            s = (r["quality"]["sources"].get(m) or {}).get("source")
            con.execute("INSERT INTO readings(spot,metric,value,text,source,fetched_at) VALUES(?,?,?,?,?,?)",
                        (r["id"], m, v if isinstance(v, (int, float)) else None,
                         v if isinstance(v, str) else None, s, now.isoformat()))
    con.commit(); con.close()

def run_once():
    now, cache, results = datetime.now(UTC), {}, []
    for spot in SPOTS:
        try:
            results.append(collect(spot, now, cache))
        except Exception:                       # one bad spot must not stop the rest
            log.exception("spot %s failed", spot["id"])
    save(results, now)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated": now.isoformat(), "spots": results}, f, indent=2)
    os.replace(tmp, OUT)
    for r in results:
        q = r["quality"]
        log.info("%-8s completeness %.0f%% missing=%s", r["id"], q["completeness"] * 100, q["missing"] or "-")

def discover():
    for spot in SPOTS:
        print(f"\n== {spot['name']}")
        for kind, sid in spot["water"]:
            if kind == "coops":
                j = get_json(MDAPI % sid)
                names = [p["name"] for p in (j or {}).get("products", [])] or ["(lookup failed)"]
                print(f"  CO-OPS {sid}: {', '.join(names)}")
            else:
                j = get_json(USGS, dict(format="json", sites=sid, siteStatus="all"))
                try:
                    names = sorted({t["variable"]["variableName"] for t in j["value"]["timeSeries"]})
                except (TypeError, KeyError):
                    names = ["(lookup failed)"]
                print(f"  USGS {sid}: {'; '.join(names)}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "discover":
        discover()
    elif cmd == "loop":
        mins = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        while True:
            run_once()
            time.sleep(mins * 60)
    else:
        run_once()
