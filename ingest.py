#!/usr/bin/env python3
"""
Louisiana fishing conditions: ingestion worker (prototype)

Pulls public data for each spot, normalizes it, stores history in SQLite and
writes latest.json in the shape the scoring page expects.

Sources
  NOAA CO-OPS   water temp, conductivity (-> salinity), wind, tide predictions
  USGS Water Data APIs (api.waterdata.usgs.gov)
                water temp, specific conductance (-> salinity), turbidity, gage height
  Open-Meteo    wind, cloud cover, surface pressure (-> 3 hr trend), marine model
  NDBC, CoastWatch ERDDAP, NWS alerts
  NWS NWPS      Mississippi and Atchafalaya river stage, forecast crest, flood levels

Usage (Python 3.9+, standard library only)
  python ingest.py discover     # list what each configured station really offers
  python ingest.py check-usgs   # test the USGS API (and your key) on one gauge
  python ingest.py check-map    # test the chart, depth and satellite map sources
  python ingest.py health       # check every data source, write health.md
  python ingest.py validate     # sanity-check latest.json before deploying
  python ingest.py once         # one pull, writes fishing.db + latest.json
  python ingest.py loop 30      # repeat every 30 minutes

USGS: the old waterservices.usgs.gov service slows down from Nov 16 2026, has
outages in Jan 2027 and shuts down Feb 22 2027. This worker uses the new
api.waterdata.usgs.gov service and only falls back to the old one if the new
one returns nothing. The new service limits requests without a key, so get a
free key at https://api.waterdata.usgs.gov/signup and set the environment
variable USGS_API_KEY (in GitHub: a repository secret of the same name).

Set CONTACT below. Check each API's terms (Open-Meteo's free tier is for
non-commercial use).
"""
import json, logging, math, os, sqlite3, sys, threading, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONTACT = "https://github.com/Kenny-Powers-55-g/LA-Fishing"   # a project URL works; no personal email needed
APP = "la-fishing-prototype"
DB, OUT = "fishing.db", "latest.json"
STATUS_OUT = "status.json"
STATION_CACHE = "stations_cache.json"  # persists nearest USGS / buoy discoveries
STALE_MIN = 180          # readings older than this are treated as missing
WORKERS = int(os.environ.get("INGEST_WORKERS", "4"))   # spots fetched in parallel
USGS_KEY = os.environ.get("USGS_API_KEY") or None
UTC = timezone.utc
log = logging.getLogger(APP)

def load_station_cache():
    try:
        with open(STATION_CACHE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

def save_station_cache(cache):
    # only persist the discovery keys (sites / buoys), not live readings
    out = {}
    for k, v in cache.items():
        if isinstance(k, tuple) and k and k[0] in ("sites", "buoys", "depth", "mzone"):
            # JSON keys must be strings
            out["|".join(str(x) for x in k)] = v
        elif k in ("buoys", "deadbuoys"):
            out[k] = v
    try:
        with open(STATION_CACHE + ".tmp", "w") as f:
            json.dump(out, f)
        os.replace(STATION_CACHE + ".tmp", STATION_CACHE)
    except OSError as e:
        log.warning("could not write station cache: %s", e)

def hydrate_station_cache(cache, disk):
    for k, v in (disk or {}).items():
        if k in ("buoys", "deadbuoys"):
            cache.setdefault(k, v)
            continue
        if "|" in k:
            parts = k.split("|")
            # ("sites", spot_id, pcode) or similar
            key = tuple(parts)
            cache.setdefault(key, [tuple(x) if isinstance(x, list) else x for x in v] if isinstance(v, list) else v)


# Water sources are tried in order, per metric. Station IDs were checked
# against NOAA/USGS listings; which products each one offers is NOT assumed:
# run `discover`, then trim this list.
SPOTS = [
    {"id": "calc", "name": "Calcasieu Lake", "lat": 29.88, "lon": -93.30,
     "water": [("coops", "8768094")], "tide": "8768094"},
    {"id": "barat", "name": "Barataria Bay (Grand Isle)", "lat": 29.27, "lon": -89.95,
     "water": [("coops", "8761724")], "tide": "8761724"},
    {"id": "venice", "river": "miss", "name": "Venice / Delta Marsh", "lat": 29.28, "lon": -89.35,
     "water": [("coops", "8760721"), ("coops", "8760922")], "tide": "8760922"},
    {"id": "pont", "river": "miss", "name": "Lake Pontchartrain", "lat": 30.15, "lon": -90.10,
     "water": [("coops", "8761927"), ("coops", "8761305")], "tide": "8761927"},
    {"id": "atch", "river": "atch", "name": "Atchafalaya Basin", "lat": 30.10, "lon": -91.60,
     "water": [("usgs", "07381515")], "tide": None},
    {"id": "verret", "river": "atch", "name": "Lake Verret", "lat": 29.95, "lon": -91.15,
     "water": [("usgs", "295610091110000"), ("usgs", "07381515")], "tide": None},
    # Houma area. Coordinates are approximate; inland spots have no listed gauge, so the
    # worker searches for the nearest USGS temperature and salinity stations by itself.
    {"id": "cocodrie", "name": "Cocodrie / Terrebonne Bay", "lat": 29.26, "lon": -90.66,
     "water": [("coops", "8762928")], "tide": "8762928"},
    {"id": "dulac", "name": "Dulac / Houma Navigation Canal", "lat": 29.39, "lon": -90.72,
     "water": [("coops", "8762928")], "tide": "8762928"},
    {"id": "fourchon", "name": "Port Fourchon / Belle Pass", "lat": 29.11, "lon": -90.20,
     "water": [("coops", "8762075"), ("coops", "8761724")], "tide": "8762075"},
    {"id": "houma", "river": "atch", "name": "Houma Bayous (Bayou Terrebonne)", "lat": 29.58, "lon": -90.72,
     "water": [], "tide": None},
    {"id": "palourde", "river": "atch", "name": "Lake Palourde", "lat": 29.70, "lon": -91.08,
     "water": [], "tide": None},
    # Offshore zones — approximate open-Gulf points (buoy/model data, not exact wrecks/rigs).
    # South of Houma / Terrebonne
    {"id": "off40", "name": "Offshore 40 mi (south of Houma)", "lat": 28.50, "lon": -90.60,
     "water": [], "tide": None, "marine": True},
    {"id": "off65", "name": "Offshore 65 mi (south of Houma)", "lat": 28.15, "lon": -90.60,
     "water": [], "tide": None, "marine": True},
    {"id": "off90", "name": "Offshore 90 mi (south of Houma)", "lat": 27.80, "lon": -90.60,
     "water": [], "tide": None, "marine": True},
    # West — Calcasieu / Sabine approach
    {"id": "off_west40", "name": "Offshore 40 mi (SW of Calcasieu)", "lat": 29.20, "lon": -93.60,
     "water": [], "tide": None, "marine": True},
    {"id": "off_west70", "name": "Offshore 70 mi (SW of Calcasieu)", "lat": 28.80, "lon": -93.80,
     "water": [], "tide": None, "marine": True},
    # Central — south of Barataria / Grand Isle
    {"id": "off_barat40", "name": "Offshore 40 mi (south of Grand Isle)", "lat": 28.70, "lon": -89.95,
     "water": [], "tide": None, "marine": True},
    {"id": "off_barat70", "name": "Offshore 70 mi (south of Grand Isle)", "lat": 28.25, "lon": -89.90,
     "water": [], "tide": None, "marine": True},
    # East — south of the Delta / Venice
    {"id": "off_delta40", "name": "Offshore 40 mi (south of Venice)", "lat": 28.70, "lon": -89.20,
     "water": [], "tide": None, "marine": True},
    {"id": "off_delta70", "name": "Offshore 70 mi (south of Venice)", "lat": 28.20, "lon": -89.10,
     "water": [], "tide": None, "marine": True},
    # Deep blue — ~100+ mi class for tuna/wahoo runs
    {"id": "off_deep", "name": "Deep blue (~110 mi south)", "lat": 27.40, "lon": -90.40,
     "water": [], "tide": None, "marine": True},
    # Named offshore spots. Positions are published approximate centers (WGS 84); check your
    # chart and depth finder on site. depth_ft, where given, is the published depth and
    # overrides the coarse depth model.
    {"id": "midlump", "name": "Midnight Lump (Sackett Bank)", "lat": 28.6375, "lon": -89.5568,
     "water": [], "tide": None, "marine": True, "depth_ft": 206,
     "note": "Salt-dome lump about 23 mi south-southwest of Southwest Pass. Crest near 206 ft, base near 360 ft. "
             "Known for tuna, wahoo, amberjack and snapper. Heavy ship traffic nearby."},
    {"id": "mc148", "name": "MC-148 Reef (Mississippi Canyon)", "lat": 28.7941, "lon": -89.1755,
     "water": [], "tide": None, "marine": True, "depth_ft": 451,
     "note": "LDWF artificial reef with a fish-attracting buoy, about 30 nautical miles south-southeast of Venice in 451 ft of water."},
    {"id": "bullwinkle", "name": "Bullwinkle (Green Canyon 65)", "lat": 27.8845, "lon": -90.9026,
     "water": [], "tide": None, "marine": True,
     "note": "Deepwater platform about 114 mi from Southwest Pass. Long run; tuna, wahoo and mahi around the structure."},
    {"id": "eflower", "name": "East Flower Garden Bank", "lat": 27.9095, "lon": -93.5974, "water": [],
     "tide": None, "marine": True, "depth_ft": 64,
     "note": "Coral reef bank in Flower Garden Banks National Marine Sanctuary, about 110 mi south of the Louisiana-Texas line. "
             "Crest near 64 ft. Sanctuary fishing rules apply: check them before you go."},
    {"id": "shipshoal", "name": "Ship Shoal (old lighthouse)", "lat": 28.9150, "lon": -91.0712,
     "water": [], "tide": None, "marine": True, "depth_ft": 11,
     "note": "Sand shoal with rigs south of Isles Dernieres. Water around the old lighthouse is 10 to 12 ft. "
             "Cobia, tripletail, king mackerel and specks on the rigs."},
    # Nearshore, scored with inshore species
    {"id": "swpass", "river": "miss", "name": "Southwest Pass jetties", "lat": 28.9056, "lon": -89.4289,
     "water": [("coops", "8760922")], "tide": "8760922",
     "note": "End of the Mississippi River's Southwest Pass. Bull reds and specks along the jetties; strong current and ship traffic."},
]

# ---------------------------------------------------------------- HTTP
# Hosts that time out when hit by several workers at once get a small concurrency limit.
_HOST_GATES = {"open-meteo.com": threading.Semaphore(2)}

# NOAA ERD's ERDDAP is sometimes busy (HTTP 503); upwell is its sister server with the same datasets.
ERDDAP_MIRRORS = ("https://coastwatch.pfeg.noaa.gov/erddap", "https://upwell.pfeg.noaa.gov/erddap")

def erddap_json(path, tries=1, timeout=12):
    """GET an ERDDAP path (e.g. 'griddap/jplMURSST41.json?...') from the first mirror that answers."""
    for base in ERDDAP_MIRRORS:
        j = get_json(f"{base}/{path}", tries=tries, timeout=timeout)
        if j is not None:
            return j
    return None

def get_json(url, params=None, tries=3, timeout=15, headers=None):
    if params:
        url += "?" + urlencode(params)
    # Chlorophyll / busy mirrors: fail faster so one stuck host does not eat the job budget
    if "erddap" in url or "coastwatch" in url:
        timeout = min(timeout, 12)
        tries = min(tries, 1)
    hdr = {"User-Agent": f"{APP} ({CONTACT})", **(headers or {})}
    gate = next((g for host, g in _HOST_GATES.items() if host in url), None)
    for i in range(tries):
        try:
            req = Request(url, headers=hdr)
            if gate:
                gate.acquire()
            try:
                with urlopen(req, timeout=timeout) as r:
                    body = r.read()
                try:
                    return json.loads(body)
                except UnicodeDecodeError:                # some ArcGIS servers send Windows-1252 text
                    return json.loads(body.decode("cp1252", "replace"))
            finally:
                if gate:
                    gate.release()
        except HTTPError as e:
            log.warning("HTTP %s for %s", e.code, url)
            if e.code < 500:
                return None
        except (URLError, TimeoutError, OSError, ValueError) as e:      # ValueError: not JSON / bad bytes
            log.warning("fetch failed (%s): %s", str(e)[:120], url)
        time.sleep(2 ** i)
    return None

def get_text(url, params=None, quiet_404=False):
    if params:
        url += "?" + urlencode(params)
    try:
        with urlopen(Request(url, headers={"User-Agent": f"{APP} ({CONTACT})"}), timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    except HTTPError as e:
        if not (quiet_404 and e.code == 404):
            log.warning("fetch failed (HTTP %s): %s", e.code, url)
    except (URLError, TimeoutError, OSError) as e:
        log.warning("fetch failed (%s): %s", e, url)
        return None

def miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(a))

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

def nearest(points, target, tol):
    """Value of the (time, value) point closest to target, if within tol."""
    best = None
    for t, v in points:
        d = abs(t - target)
        if best is None or d < best[0]:
            best = (d, v)
    return best[1] if best and best[0] <= tol else None

def coops_source(station):
    out = {}
    j = get_json(COOPS, dict(station=station, product="water_temperature", date="recent",
                             units="english", time_zone="gmt", format="json", application=APP))
    pts = [(parse_gmt(d["t"]), fnum(d.get("v"))) for d in (j or {}).get("data", [])]
    pts = [(t, v) for t, v in pts if v is not None]
    if pts:
        t, v = pts[-1]
        out["temp_f"] = (v, t, None)
        ref = nearest(pts, t - timedelta(hours=24), timedelta(hours=1))
        if ref is not None:
            out["temp_d24"] = (v - ref, t, None)           # deg F change over 24 h
    d = coops_latest(station, "conductivity")            # mS/cm
    if d and fnum(d.get("v")) is not None:
        out["cond_ms"] = (fnum(d["v"]), parse_gmt(d["t"]), None)   # temp-correct later
    d = coops_latest(station, "wind")                    # knots with units=english
    if d and fnum(d.get("s")) is not None:
        out["wind_mph"] = (fnum(d["s"]) * 1.15078, parse_gmt(d["t"]), None)
    return out

# ---------------------------------------------------------------- USGS
USGS = "https://waterservices.usgs.gov/nwis/iv/"                     # legacy, retired Feb 22 2027
WD = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/%s/items"  # replacement
USGS_CODES = "00010,00095,63680,00065"   # water temp C, conductance uS/cm@25C, turbidity FNU, gage ft

def wd_items(collection, params):
    """Features from the USGS Water Data API, or None if the request failed."""
    h = {"Accept": "application/json"}
    if USGS_KEY:
        h["X-Api-Key"] = USGS_KEY
    j = get_json(WD % collection, params, tries=2, timeout=25, headers=h)
    if not isinstance(j, dict):
        return None
    return j.get("features") or []

def parse_iso(s):
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)

def near_sites(spot, cache, pcode, radius_mi=50, limit=5):
    """Nearest active USGS sites reporting a parameter (00095 conductance, 00010 water temp)."""
    key = ("sites", spot["id"], pcode)
    if key in cache:
        return cache[key]
    d = 0.8  # ~50 mi box
    # New API: newest value of every series in the box. Recent = active.
    feats = wd_items("latest-continuous", dict(
        bbox=f"{spot['lon']-d:.4f},{spot['lat']-d:.4f},{spot['lon']+d:.4f},{spot['lat']+d:.4f}",
        parameter_code=pcode, limit=500))
    if feats:
        have, found = {sid for k, sid in spot["water"] if k == "usgs"}, {}
        cutoff = datetime.now(UTC) - timedelta(days=3)
        for f in feats:
            p, g = f.get("properties") or {}, f.get("geometry") or {}
            mid = str(p.get("monitoring_location_id") or "")
            sid = mid.split("-", 1)[1] if "-" in mid else None
            xy = g.get("coordinates") or []
            try:
                fresh = parse_iso(p.get("time")) >= cutoff
            except (TypeError, ValueError):
                fresh = False
            if not sid or sid in have or len(xy) < 2 or not fresh:
                continue
            dist = miles(spot["lat"], spot["lon"], fnum(xy[1]), fnum(xy[0]))
            if dist <= radius_mi:
                found[sid] = min(round(dist), found.get(sid, 1e9))
        cache[key] = sorted(found.items(), key=lambda x: x[1])[:limit]
        if cache[key]:
            log.info("%s near USGS %s: %s", spot["id"], pcode, ", ".join(f"{s}({d}mi)" for s, d in cache[key]))
        return cache[key]
    txt = get_text("https://waterservices.usgs.gov/nwis/site/", dict(
        format="rdb", parameterCd=pcode, hasDataTypeCd="iv", siteStatus="active",
        bBox=f"{spot['lon']-d:.4f},{spot['lat']-d:.4f},{spot['lon']+d:.4f},{spot['lat']+d:.4f}"))
    out, have = [], {sid for k, sid in spot["water"] if k == "usgs"}
    rows = [l.split("\t") for l in (txt or "").splitlines() if l and not l.startswith("#")]
    if len(rows) > 2:
        h = rows[0]
        try:
            i_no, i_lat, i_lon = h.index("site_no"), h.index("dec_lat_va"), h.index("dec_long_va")
            for r in rows[2:]:                      # rows[1] is the column-width row
                if len(r) <= max(i_no, i_lat, i_lon):
                    continue
                la, lo = fnum(r[i_lat]), fnum(r[i_lon])
                if la is not None and lo is not None and r[i_no] not in have:
                    dist = miles(spot["lat"], spot["lon"], la, lo)
                    if dist <= radius_mi:
                        out.append((r[i_no], round(dist)))
        except (ValueError, IndexError) as e:
            log.warning("site list parse problem for %s: %s", spot["id"], e)
    cache[key] = sorted(out, key=lambda x: x[1])[:limit]
    if cache[key]:
        log.info("%s near USGS %s: %s", spot["id"], pcode, ", ".join(f"{s}({d}mi)" for s, d in cache[key]))
    return cache[key]

LEVEL_STEP = 0.2   # ft change over ~6 h that counts as Rising/Falling (adjust to taste)

def usgs_points(code, pts, out):
    """Turn one parameter's (time, value) points into readings."""
    if not pts:
        return
    pts.sort()
    t, x = pts[-1]
    if code == "00010":
        out["temp_f"] = (f_from_c(x), t, None)
        ref = nearest(pts, t - timedelta(hours=24), timedelta(hours=2))
        if ref is not None:
            out["temp_d24"] = ((x - ref) * 9 / 5, t, None)
    elif code == "00095":                        # uS/cm at 25 C
        out["cond_ms"] = (x / 1000.0, t, 25.0)
    elif code == "63680":
        out["turb_fnu"] = (x, t, None)
    elif code == "00065":                        # gage height, ft
        ref = nearest(pts, t - timedelta(hours=6), timedelta(hours=1))
        if ref is not None:
            out["level_d6"] = (x - ref, t, None)

def usgs_source(site):
    """Last 24 h from one USGS gauge: new API first, the legacy service as a fallback."""
    out = usgs_source_new(site)
    if out is None:
        out = usgs_source_legacy(site)
        if out:
            log.warning("USGS %s: new API gave nothing, used legacy waterservices (retired Feb 22 2027)", site)
    return out or {}

def usgs_source_new(site):
    feats = wd_items("continuous", dict(monitoring_location_id=f"USGS-{site}", parameter_code=USGS_CODES,
                                        time="P1D", skipGeometry="true", limit=10000))
    if not feats:
        return None
    by_code = {}                                  # code -> series id -> points
    for f in feats:
        p = f.get("properties") or {}
        if p.get("statistic_id") not in (None, "", "00011"):     # 00011 = instantaneous
            continue
        x = fnum(p.get("value"))
        try:
            t = parse_iso(p.get("time"))
        except (TypeError, ValueError):
            continue
        if x is None:
            continue
        by_code.setdefault(p.get("parameter_code"), {}).setdefault(p.get("time_series_id"), []).append((t, x))
    out = {}
    for code, series in by_code.items():
        # a gauge can have several sensors for one parameter: use the one reporting most recently
        usgs_points(code, max(series.values(), key=lambda s: max(t for t, _ in s)), out)
    return out or None

def usgs_source_legacy(site):
    j = get_json(USGS, dict(format="json", sites=site, siteStatus="all", period="P1D",
                            parameterCd=USGS_CODES), tries=1)
    out = {}
    try:
        for ts in j["value"]["timeSeries"]:
            code = ts["variable"]["variableCode"][0]["value"]
            nodata = ts["variable"]["noDataValue"]
            pts = []
            for v in ts["values"][0]["value"]:
                x = fnum(v["value"])
                if x is not None and x != nodata:
                    pts.append((datetime.fromisoformat(v["dateTime"]).astimezone(UTC), x))
            usgs_points(code, pts, out)
    except (TypeError, KeyError, ValueError, IndexError) as e:
        log.warning("USGS legacy parse problem for %s: %s", site, e)
    return out

# ---------------------------------------------------------------- weather
def weather(lat, lon):
    j = get_json("https://api.open-meteo.com/v1/forecast", dict(
        latitude=lat, longitude=lon,
        current="wind_speed_10m,wind_direction_10m,cloud_cover,surface_pressure,temperature_2m",
        hourly="surface_pressure,temperature_2m,precipitation,wind_speed_10m,wind_direction_10m,cloud_cover",
        past_hours=48, forecast_hours=169,
        wind_speed_unit="mph", temperature_unit="fahrenheit", precipitation_unit="inch",
        timezone="GMT"))
    try:
        cur = j["current"]
        t = datetime.fromisoformat(cur["time"]).replace(tzinfo=UTC)
        H = j["hourly"]
        times = [datetime.fromisoformat(a).replace(tzinfo=UTC) for a in H["time"]]
        def series(key):
            return [(ts, v) for ts, v in zip(times, H[key]) if v is not None]
        ref = nearest(series("surface_pressure"), t - timedelta(hours=3), timedelta(minutes=90))
        delta = None if ref is None else cur["surface_pressure"] - ref
        trend = None if delta is None else ("Falling" if delta <= -1.0 else "Rising" if delta >= 1.0 else "Steady")
        ref_air = nearest(series("temperature_2m"), t - timedelta(hours=24), timedelta(minutes=90))
        air_d24 = None if ref_air is None else cur["temperature_2m"] - ref_air
        rain = [v for ts, v in series("precipitation") if t - timedelta(hours=48) < ts <= t]
        rain48 = sum(rain) if len(rain) >= 40 else None      # need most of the window
        wdir = cur.get("wind_direction_10m")
        return {"time": t, "wind_mph": cur["wind_speed_10m"], "wind_dir": wdir, "cloud": cur["cloud_cover"],
                "pres": trend, "pres_delta_hpa": delta, "air_d24": air_d24, "rain48": rain48,
                "series": {"t": times, "pres": H["surface_pressure"], "air": H["temperature_2m"], "rain": H["precipitation"],
                           "wind": H["wind_speed_10m"], "cloud": H["cloud_cover"]}}
    except (TypeError, KeyError, ValueError):
        return None

def clarity_label(turb, wind, sal, rain=None, chl=None):
    if turb is not None:
        return ("Clear" if turb < 10 else "Stained" if turb < 40 else "Muddy"), "measured"
    # VIIRS chlorophyll-a (mg/m3) as a water-color / productivity proxy when no turbidity gauge
    if chl is not None:
        # Gulf coastal heuristic: low chl often clearer water; very high often green/stained
        if chl < 1.5 and (wind or 0) < 15:
            return "Clear", "satellite"
        if chl >= 8 or (wind or 0) >= 20:
            return "Muddy", "satellite"
        if chl >= 3.5:
            return "Stained", "satellite"
        return "Stained", "satellite"
    if (wind is not None and wind >= 20) or (rain is not None and rain >= 1.5):
        return "Muddy", "estimated"
    if sal is not None and sal >= 20 and (wind or 0) < 12 and (rain or 0) < 0.5:
        return "Clear", "estimated"
    return "Stained", "estimated"

# Shared across spots in one run: one bad ERDDAP stretch should not burn the whole job
_SAT_FAILS = 0
_SAT_DISABLED = False

def sat_chlorophyll(lat, lon, cache):
    """Near-real-time VIIRS chlorophyll-a (mg/m3) from NOAA CoastWatch ERDDAP.
    Uses [last] timestep only (fast). Returns (value, time_iso) or None."""
    global _SAT_FAILS, _SAT_DISABLED
    if _SAT_DISABLED:
        return None
    # Snap to ~4km grid so nearby spots share one request
    key = ("chl", round(lat * 4) / 4, round(lon * 4) / 4)
    if key in cache:
        return cache[key]
    out = None
    # [last] = newest composite available — avoids 404s for missing calendar days
    url = (
        "https://coastwatch.pfeg.noaa.gov/erddap/griddap/nesdisVHNnoaa20chlaDaily.json"
        f"?chlor_a[(last)][(0.0)][({key[1]})][({key[2]})]"
    )
    j = get_json(url, tries=1)
    try:
        rows = (j or {}).get("table", {}).get("rows") or []
        for row in rows:
            val = row[-1] if row else None
            if isinstance(val, (int, float)) and 0 < float(val) < 80:
                out = (round(float(val), 2), row[0])
                break
        if out is None and j is None:
            _SAT_FAILS += 1
        else:
            _SAT_FAILS = 0
    except (TypeError, KeyError, ValueError, IndexError):
        _SAT_FAILS += 1
    if _SAT_FAILS >= 4:
        _SAT_DISABLED = True
        log.warning("satellite chlorophyll disabled for this run after repeated failures")
    cache[key] = out
    return out

def sat_sst(lat, lon, cache):
    """Satellite (MUR) sea temperature around a spot: temp at the spot, strongest temperature
    break (deg F per mile between neighboring ~2 mi cells) and the warm-to-cool range in the box."""
    key = ("sst", round(lat * 4) / 4, round(lon * 4) / 4)
    if key in cache:
        return cache[key]
    d, out = 0.25, None
    j = erddap_json("griddap/jplMURSST41.json"
                    f"?analysed_sst[(last)][({lat-d:.2f}):3:({lat+d:.2f})][({lon-d:.2f}):3:({lon+d:.2f})]")
    if j is None:                              # server didn't answer; get_json already logged it
        cache[key] = None
        return None
    try:
        rows = j["table"]["rows"]
        grid = {(round(r[1], 3), round(r[2], 3)): r[3] for r in rows if r[3] is not None}
        lats, lons = sorted({k[0] for k in grid}), sorted({k[1] for k in grid})
        if len(grid) >= 9:
            best = 0.0
            for (la, lo), v in grid.items():
                for dla, dlo in ((1, 0), (0, 1)):
                    la2 = lats[lats.index(la) + dla] if dla and lats.index(la) + 1 < len(lats) else (la if not dla else None)
                    lo2 = lons[lons.index(lo) + dlo] if dlo and lons.index(lo) + 1 < len(lons) else (lo if not dlo else None)
                    v2 = grid.get((la2, lo2)) if la2 is not None and lo2 is not None else None
                    if v2 is None:
                        continue
                    dist = miles(la, lo, la2, lo2)
                    if dist > 0:
                        best = max(best, abs(v2 - v) * 1.8 / dist)
            near = min(grid, key=lambda k: (k[0] - lat) ** 2 + (k[1] - lon) ** 2)
            vals = list(grid.values())
            out = {"sst_f": f_from_c(grid[near]), "break": best, "range": (max(vals) - min(vals)) * 1.8,
                   "time": rows[0][0]}
    except (TypeError, KeyError, IndexError, ValueError) as e:
        log.warning("satellite SST problem: %s", e)
    cache[key] = out
    return out

# ---------------------------------------------------------------- water visibility
# Satellite water clarity: Kd490 = how fast blue-green light fades with depth (per metre).
# Tried in order. The gap-filled products fill cloudy days but run about a week or more behind.
KD_SETS = ["nesdisVHNkd490Daily",                      # S-NPP VIIRS near real-time, 4 km
           "noaacwNPPN20S3AkdSCIDINEOF2kmDaily",       # VIIRS + OLCI gap-filled, 2 km
           "nesdisNPPN20S3AkdSCIDINEOFDaily"]          # VIIRS + OLCI gap-filled, 9 km
_KD_FAILS = {}            # dataset -> failed requests this run; a dataset that keeps failing is skipped
_KD_LOCK = threading.Lock()

def sat_kd490(lat, lon, cache, now):
    """Newest satellite Kd490 near a spot: (kd, time, dataset) or None.
    Takes the median of the water cells within ~6 mi so a land-masked cell at the shoreline
    doesn't leave a marsh spot without a value."""
    key = ("kd", round(lat * 10) / 10, round(lon * 10) / 10)
    if key in cache:
        return cache[key]
    best, d = None, 0.1
    for ds in KD_SETS:
        if _KD_FAILS.get(ds, 0) >= 3:
            continue
        url = (f"https://coastwatch.pfeg.noaa.gov/erddap/griddap/{ds}.json"
               f"?kd_490[(last)][(0.0)][({lat+d:.3f}):({lat-d:.3f})][({lon-d:.3f}):({lon+d:.3f})]")
        j = get_json(url, tries=1)
        try:
            rows = j["table"]["rows"]
            vals = sorted(float(r[-1]) for r in rows if isinstance(r[-1], (int, float)) and 0.01 < r[-1] < 20)
            t = parse_iso(rows[0][0]) if rows else None
        except (TypeError, KeyError, IndexError, ValueError):
            with _KD_LOCK:
                _KD_FAILS[ds] = _KD_FAILS.get(ds, 0) + 1
                if _KD_FAILS[ds] == 3:
                    log.warning("satellite clarity %s skipped for this run after repeated failures", ds)
            continue
        if not vals or t is None:
            continue
        cand = (vals[len(vals) // 2], t, ds)
        if best is None or cand[1] > best[1]:
            best = cand
        if now - t <= timedelta(days=4):          # fresh enough: stop looking
            break
    cache[key] = best
    return best

# ---------------------------------------------------------------- bottom depth
NCEI_DEM = "https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_global_mosaic/ImageServer/identify"

def depth_m(lat, lon):
    """Elevation in metres at a point (negative = under water) and its source, or (None, None).
    NOAA NCEI's DEM Global Mosaic (ETOPO 2022 plus high-resolution coastal surveys) first,
    then the ETOPO1 copy on CoastWatch ERDDAP."""
    geom = json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}})
    j = get_json(NCEI_DEM, dict(geometry=geom, geometryType="esriGeometryPoint", returnGeometry="false",
                                returnCatalogItems="false", f="json"), tries=2, timeout=20)
    v = fnum((j or {}).get("value"))
    if v is not None:
        return v, "NOAA NCEI bathymetry"
    j = get_json("https://coastwatch.pfeg.noaa.gov/erddap/griddap/etopo180.json"
                 f"?altitude[({lat:.4f})][({lon:.4f})]", tries=2)
    try:
        return float(j["table"]["rows"][0][-1]), "ETOPO1 model"
    except (TypeError, KeyError, IndexError, ValueError):
        return None, None

def bottom_depth(spot, cache):
    """Water depth at a spot in feet and where it came from. A published depth in SPOTS wins;
    otherwise offshore spots are looked up once and kept in the station cache."""
    if spot.get("depth_ft") is not None:
        return spot["depth_ft"], "published"
    if not spot.get("marine"):                 # marsh, bays and bayous: a point value misleads
        return None, None
    key = ("depth", spot["id"])
    if key not in cache:
        m, src = depth_m(spot["lat"], spot["lon"])
        if m is None:
            log.warning("depth lookup failed for %s; will retry next run", spot["id"])
            return None, None
        cache[key] = [round(-m * 3.28084) if m < 0 else 0, src]
    v = cache[key]
    if not isinstance(v, (list, tuple)):        # older cache entries stored only the number
        v = [v, "ETOPO1 model"]
    return (v[0], v[1]) if v[0] else (None, None)

def kdpar_from_kd490(kd):
    """Kd(PAR) from Kd(490) (Morel et al. 2007)."""
    return 0.0864 + 0.884 * kd - 0.00137 / kd

def vis_from_kd490(kd):
    """Secchi depth in feet: about 1.7 / Kd(PAR) (Poole-Atkins)."""
    return min(100.0, 1.7 / max(kdpar_from_kd490(kd), 0.03) * 3.28084)

def vis_from_turbidity(fnu):
    """Secchi depth in feet from turbidity (FNU). Rough estuary relation: Kd(PAR) ~ 0.4 + 0.08 * FNU."""
    return min(15.0, 1.7 / (0.4 + 0.08 * max(fnu, 0)) * 3.28084)

def kd490_from_chl(chl):
    """Open-ocean Kd490 from chlorophyll (Morel et al. 2007). Not valid in muddy coastal water."""
    return 0.0166 + 0.0773 * chl ** 0.6715

def clarity_from_vis(vis_ft):
    return "Clear" if vis_ft >= 4 else "Stained" if vis_ft >= 1.5 else "Muddy"

def color_label(chl):
    """Human-friendly water-color tag from chlorophyll-a."""
    if chl is None:
        return None
    if chl < 1.0:
        return "Blue / clear"
    if chl < 2.5:
        return "Blue-green"
    if chl < 5.0:
        return "Green"
    if chl < 10.0:
        return "Green-brown"
    return "Brown / turbid"

# ---------------------------------------------------------------- offshore + alerts
def marine(lat, lon, now):
    """Modeled sea surface temp, waves and current (Open-Meteo Marine). A model, not a buoy."""
    j = get_json("https://marine-api.open-meteo.com/v1/marine", dict(
        latitude=lat, longitude=lon, past_days=2, forecast_days=8, timezone="GMT",
        hourly="wave_height,wave_period,sea_surface_temperature,ocean_current_velocity"))
    try:
        H = j["hourly"]
        times = [datetime.fromisoformat(a).replace(tzinfo=UTC) for a in H["time"]]
        def col(key, f):
            return [None if x is None else f(x) for x in (H.get(key) or [None] * len(times))]
        out = {"sst": col("sea_surface_temperature", f_from_c), "wave": col("wave_height", lambda m: m * 3.28084),
               "cur": col("ocean_current_velocity", lambda k: k * 0.539957)}   # km/h -> knots
    except (TypeError, KeyError, ValueError):
        return None
    idx = {t: i for i, t in enumerate(times)}
    out["idx"] = idx
    nh = now.replace(minute=0, second=0, microsecond=0)
    at = lambda key, t: (out[key][idx[t]] if t in idx else None)
    s0, s1 = at("sst", nh), at("sst", nh - timedelta(hours=24))
    out["now"] = {"sst": s0, "sst_d24": None if s0 is None or s1 is None else s0 - s1,
                  "wave": at("wave", nh), "cur": at("cur", nh)}
    return out

def near_buoys(spot, cache, radius_mi=75, limit=6):
    """Nearest active NDBC stations with weather sensors (buoys and platforms)."""
    with _BUOY_LOCK:                          # one download of the station list per run
        if "buoys" not in cache:
            cache["buoys"] = buoy_list()
    dead = set(cache.get("deadbuoys") or [])          # stations with no realtime data file
    near = [(i, round(miles(spot["lat"], spot["lon"], la, lo))) for i, la, lo in cache["buoys"] if i not in dead]
    return sorted([x for x in near if x[1] <= radius_mi], key=lambda x: x[1])[:limit]

_BUOY_LOCK = threading.Lock()

def buoy_list():
    st = []
    try:
        for e in ET.fromstring(get_text("https://www.ndbc.noaa.gov/activestations.xml")).iter("station"):
            if e.get("met") == "y" and fnum(e.get("lat")) is not None and fnum(e.get("lon")) is not None:
                st.append((e.get("id"), fnum(e.get("lat")), fnum(e.get("lon"))))
    except (ET.ParseError, TypeError) as e:
        log.warning("NDBC station list problem: %s", e)
    return st

def buoy_obs(sid):
    """Latest sea temp, waves, wind and pressure trend from an NDBC realtime2 file."""
    txt = get_text(f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt", quiet_404=True)
    if txt is None:
        return None                                   # station has no realtime file
    lines = [l.split() for l in txt.splitlines() if l.strip()]
    if len(lines) < 3 or not lines[0][0].startswith("#"):
        return {}
    cols, rows, out = [c.lstrip("#") for c in lines[0]], lines[2:], {}     # newest row first
    def val(r, name):
        i = cols.index(name) if name in cols else None
        return fnum(r[i]) if i is not None and i < len(r) and r[i] != "MM" else None
    def when(r):
        return datetime(int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), tzinfo=UTC)
    def latest(name):
        for r in rows[:48]:
            v = val(r, name)
            if v is not None:
                return when(r), v
        return None
    def series(name):
        return [(when(r), val(r, name)) for r in rows if val(r, name) is not None]
    try:
        w = latest("WTMP")
        if w:
            out["temp_f"] = (f_from_c(w[1]), w[0], None)
            ref = nearest(series("WTMP"), w[0] - timedelta(hours=24), timedelta(minutes=90))
            if ref is not None:
                out["temp_d24"] = ((w[1] - ref) * 9 / 5, w[0], None)
        w = latest("WVHT")
        if w:
            out["wave"] = (w[1] * 3.28084, w[0], None)
        w = latest("WSPD")
        if w:
            out["wind_mph"] = (w[1] * 2.23694, w[0], None)
        w = latest("PRES")
        if w:
            ref = nearest(series("PRES"), w[0] - timedelta(hours=3), timedelta(minutes=45))
            if ref is not None:
                d = w[1] - ref
                out["pres"] = ("Falling" if d <= -1.0 else "Rising" if d >= 1.0 else "Steady", w[0], None)
    except (ValueError, IndexError) as e:
        log.warning("buoy %s parse problem: %s", sid, e)
    return out

# ---------------------------------------------------------------- rivers and water level
# NWS river gauges (National Water Prediction Service). Mississippi stage drives clarity and
# salinity around the delta and, through the Bonnet Carre Spillway, Lake Pontchartrain;
# the Atchafalaya does the same for the basin, Morgan City and Terrebonne.
NWPS = "https://api.water.noaa.gov/nwps/v1/gauges/%s"
RIVERS = {
    "miss": {"lid": "NORL1", "name": "Mississippi River at New Orleans (Carrollton)"},
    "atch": {"lid": "MCGL1", "name": "Atchafalaya River at Morgan City",
             "also": {"lid": "SMML1", "name": "Atchafalaya River at Simmesport"}},
}
SPILLWAY_WATCH_FT = 16.0      # Carrollton stage where the Corps starts preparing the Bonnet Carre Spillway

def river_gauge(lid, now):
    """Latest stage, 24 h change, forecast crest and flood thresholds for one NWS gauge."""
    meta = get_json(NWPS % lid, tries=2)
    sf = get_json(NWPS % lid + "/stageflow", tries=2)
    try:
        obs = (meta or {}).get("status", {}).get("observed", {})
        stage = fnum(obs.get("primary"))
        if stage is None or stage <= -900:
            return None
        cats = (meta.get("flood") or {}).get("categories") or {}
        lvl = {k: fnum((cats.get(k) or {}).get("stage")) for k in ("action", "minor", "moderate", "major")}
        lvl = {k: v for k, v in lvl.items() if v is not None and v > -900}
        def series(kind):
            out = []
            for d in ((sf or {}).get(kind) or {}).get("data") or []:
                v = fnum(d.get("primary"))
                try:
                    t = parse_iso(d.get("validTime"))
                except (TypeError, ValueError):
                    continue
                if v is not None and v > -900:
                    out.append((t, v))
            return sorted(out)
        o, f = series("observed"), series("forecast")
        t_obs = parse_iso(obs.get("validTime")) if obs.get("validTime") else (o[-1][0] if o else now)
        ref = nearest(o, t_obs - timedelta(hours=24), timedelta(hours=3)) if o else None
        d24 = None if ref is None else round(stage - ref, 2)
        future = [(t, v) for t, v in f if t > now]
        crest = max(future, key=lambda x: x[1]) if future else None
        trend = None if d24 is None else "Rising" if d24 >= 0.15 else "Falling" if d24 <= -0.15 else "Steady"
        flow = fnum(obs.get("secondary"))
        return {"lid": lid, "stage": round(stage, 2), "time": t_obs.isoformat(),
                "flow_kcfs": None if flow is None or flow <= -900 else round(flow),
                "category": obs.get("floodCategory"), "d24": d24, "trend": trend, "levels": lvl,
                "crest": None if not crest else {"stage": round(crest[1], 2), "time": crest[0].isoformat()},
                "url": f"https://water.noaa.gov/gauges/{lid.lower()}"}
    except (AttributeError, TypeError, ValueError, KeyError) as e:
        log.warning("river gauge %s problem: %s", lid, e)
        return None

def rivers(now):
    """All river gauges once per run. Adds a Bonnet Carre spillway watch for the Mississippi."""
    out = {}
    for key, r in RIVERS.items():
        g = river_gauge(r["lid"], now)
        if not g:
            continue
        g["name"] = r["name"]
        if "also" in r:
            g2 = river_gauge(r["also"]["lid"], now)
            if g2:
                g2["name"] = r["also"]["name"]
                g["upstream"] = g2
        if key == "miss":
            crest = (g.get("crest") or {}).get("stage") or 0
            g["spillway_watch"] = max(g["stage"], crest) >= SPILLWAY_WATCH_FT
        out[key] = g
    return out

def river_index(g):
    """River level as a share of flood stage (action stage if no flood stage), plus trend."""
    if not g:
        return None
    top = g["levels"].get("minor") or g["levels"].get("action")
    if not top:
        return None
    pct = round(100 * g["stage"] / top)
    return {"pct": pct, "trend": g.get("trend") or "Steady",
            "txt": f'{g["stage"]} ft ({pct}% of flood stage), {(g.get("trend") or "steady").lower()}'}

def water_level_anomaly(station, now, cache):
    """Observed water level minus the predicted tide (ft) at a CO-OPS station: what wind and
    river are doing to the water right now. Positive = higher than the tide tables."""
    key = ("wl", station)
    if key in cache:
        return cache[key]
    out = None
    obs = get_json(COOPS, dict(station=station, product="water_level", datum="MLLW", date="latest",
                               units="english", time_zone="gmt", format="json", application=APP))
    try:
        d = obs["data"][-1]
        t, v = parse_gmt(d["t"]), fnum(d["v"])
        if v is not None and now - t <= timedelta(minutes=STALE_MIN):
            pr = get_json(COOPS, dict(station=station, product="predictions", datum="MLLW", interval="6",
                                      begin_date=(t - timedelta(hours=1)).strftime("%Y%m%d %H:%M"),
                                      end_date=(t + timedelta(hours=1)).strftime("%Y%m%d %H:%M"),
                                      units="english", time_zone="gmt", format="json", application=APP))
            pts = [(parse_gmt(p["t"]), fnum(p["v"])) for p in (pr or {}).get("predictions", [])]
            p = nearest([x for x in pts if x[1] is not None], t, timedelta(minutes=10))
            if p is not None:
                out = (round(v - p, 2), t)
    except (TypeError, KeyError, IndexError, ValueError):
        pass
    cache[key] = out
    return out

def get_alerts(lat, lon):
    """Active National Weather Service alerts at a point (land or marine zone). None = unknown."""
    j = get_json("https://api.weather.gov/alerts/active", dict(point=f"{lat:.4f},{lon:.4f}"), tries=2)
    if j is None:
        return None
    out, seen = [], set()
    for f in j.get("features", []):
        pr = f.get("properties", {})
        if pr.get("event") and pr["event"] not in seen:
            seen.add(pr["event"])
            out.append({"event": pr["event"], "severity": pr.get("severity"), "ends": pr.get("ends") or pr.get("expires")})
    return out[:5]

# ---------------------------------------------------------------- 7-day outlook
def tide_pts(station, now):
    j = get_json(COOPS, dict(station=station, product="predictions", datum="MLLW", interval="hilo",
                             begin_date=(now - timedelta(days=1)).strftime("%Y%m%d"),
                             end_date=(now + timedelta(days=9)).strftime("%Y%m%d"),
                             units="english", time_zone="gmt", format="json", application=APP))
    pts = [(parse_gmt(p["t"]), fnum(p.get("v")), p["type"]) for p in (j or {}).get("predictions", [])]
    return [p for p in pts if p[1] is not None]

def stage_at(pts, t):
    prev = [p for p in pts if p[0] <= t]
    nxt = [p for p in pts if p[0] > t]
    if not prev or not nxt:
        return None, None
    pv, nx = prev[-1], nxt[0]
    rng = abs(nx[1] - pv[1])
    if rng < 0.3 or min(t - pv[0], nx[0] - t) < timedelta(minutes=30):
        return "Slack", rng
    return ("Incoming" if nx[2] == "H" else "Outgoing"), rng

def build_forecast(cond, wx, pts, now, mar=None, bias=None):
    """Hourly conditions for the next 168 h. Wind, cloud, rain, pressure and tide are forecasts;
    water temp is a damped guess from forecast air temp; salinity and level are held at today's value."""
    S = wx["series"]
    idx = {t: i for i, t in enumerate(S["t"])}
    H = lambda n: timedelta(hours=n)
    def at(key, t):
        i = idx.get(t)
        return None if i is None else S[key][i]
    def a24(t):
        v = [x for x in (at("air", t - H(k)) for k in range(24)) if x is not None]
        return sum(v) / len(v) if len(v) >= 18 else None
    now_h = now.replace(minute=0, second=0, microsecond=0)
    base = a24(now_h)
    def wt(t):
        a = a24(t)
        return None if cond["temp"] is None or a is None or base is None else cond["temp"] + 0.3 * (a - base)
    start = now_h + H(1)
    o = {k: [] for k in ("temp", "wind", "cloud", "pres", "tide", "trange", "front", "dtemp", "rain", "clar")}
    for n in range(168):
        t = start + H(n)
        p, p3 = at("pres", t), at("pres", t - H(3))
        d = None if p is None or p3 is None else p - p3
        o["pres"].append(None if d is None else "Falling" if d <= -1 else "Rising" if d >= 1 else "Steady")
        a, a1 = at("air", t), at("air", t - H(24))
        o["front"].append(None if a is None or a1 is None else round(a - a1))
        r = [x for x in (at("rain", t - H(k)) for k in range(48)) if x is not None]
        rain = round(sum(r), 2) if len(r) >= 40 else None
        o["rain"].append(rain)
        w, w1 = wt(t), wt(t - H(24))
        o["temp"].append(None if w is None else round(w))
        o["dtemp"].append(None if w is None or w1 is None else round(w - w1, 1))
        if mar:                                   # offshore: modeled sea temp, waves and current
            mt = lambda key, tt: (mar[key][mar["idx"][tt]] if tt in mar["idx"] else None)
            s_, s1_ = mt("sst", t), mt("sst", t - H(24))
            o["temp"][-1] = None if s_ is None else round(s_ + (bias or 0))
            o["dtemp"][-1] = None if s_ is None or s1_ is None else round(s_ - s1_, 1)
            for key, nm in (("wave", "wave"), ("cur", "cur")):
                v_ = mt(key, t)
                o.setdefault(nm, []).append(None if v_ is None else round(v_, 1))
        wind, cloud = at("wind", t), at("cloud", t)
        o["wind"].append(None if wind is None else round(wind))
        o["cloud"].append(None if cloud is None else round(cloud))
        st, rg = stage_at(pts, t) if pts else (None, None)
        o["tide"].append(st)
        o["trange"].append(None if rg is None else round(rg, 1))
        o["clar"].append(clarity_label(None, wind, cond["sal"], rain)[0])
    o["start"] = start.isoformat()
    return o

# ---------------------------------------------------------------- collection
def collect(spot, now, cache):
    got, src = {}, {}                      # metric -> (value, time, tref), metric -> source label
    def take(metric, rec, label, extra=None):
        if metric in got or metric not in rec:
            return
        age = (now - rec[metric][1]).total_seconds() / 60
        if age <= STALE_MIN:
            got[metric], src[metric] = rec[metric], {"source": label, "age_min": round(age), **(extra or {})}

    for kind, sid in spot["water"]:
        key = (kind, sid)
        if key not in cache:
            cache[key] = coops_source(sid) if kind == "coops" else usgs_source(sid)
        for m in ("temp_f", "temp_d24", "cond_ms", "turb_fnu", "wind_mph", "level_d6"):
            take(m, cache[key], f"{kind}:{sid}")

    if not spot.get("marine") and "cond_ms" not in got:  # inland/coastal only — open Gulf has no gauges
        for sid, dist in near_sites(spot, cache, "00095"):
            if ("usgs", sid) not in cache:
                cache[("usgs", sid)] = usgs_source(sid)
            take("cond_ms", cache[("usgs", sid)], f"usgs:{sid}", {"distance_mi": dist})
            if "cond_ms" in got:
                break

    if not spot.get("marine") and "temp_f" not in got:
        for sid, dist in near_sites(spot, cache, "00010"):
            if ("usgs", sid) not in cache:
                cache[("usgs", sid)] = usgs_source(sid)
            for m in ("temp_f", "temp_d24"):
                take(m, cache[("usgs", sid)], f"usgs:{sid}", {"distance_mi": dist})
            if "temp_f" in got:
                break

    mar = None
    if spot.get("marine"):
        mar = cache.setdefault(("mar", spot["id"]), marine(spot["lat"], spot["lon"], now))
        if mar:
            nh = now.replace(minute=0, second=0, microsecond=0)
            msrc = {"source": "open-meteo-marine (model)", "age_min": round((now - nh).total_seconds() / 60)}
            for m, key in (("temp_f", "sst"), ("temp_d24", "sst_d24"), ("wave", "wave"), ("cur", "cur")):
                if mar["now"].get(key) is not None:
                    got[m], src[m] = (mar["now"][key], nh, None), msrc

    wx = cache.setdefault(("wx", spot["id"]), weather(spot["lat"], spot["lon"]))
    if wx:
        age = (now - wx["time"]).total_seconds() / 60
        wsrc = {"source": "open-meteo", "age_min": round(age)}
        if "wind_mph" not in got:
            got["wind_mph"], src["wind_mph"] = (wx["wind_mph"], wx["time"], None), wsrc
        if wx.get("wind_dir") is not None:
            got["wdir"], src["wdir"] = (wx["wind_dir"], wx["time"], None), wsrc
        got["cloud"], src["cloud"] = (wx["cloud"], wx["time"], None), wsrc
        if wx["pres"]:
            got["pres"], src["pres"] = (wx["pres"], wx["time"], None), wsrc
        if wx["rain48"] is not None:
            got["rain"], src["rain"] = (wx["rain48"], wx["time"], None), wsrc
        if wx["air_d24"] is not None:
            got["front"], src["front"] = (wx["air_d24"], wx["time"], None), wsrc

    # Satellite chlorophyll (water color / clarity proxy) — skip when inland freshwater-only spots prefer gauges
    chl_pack = sat_chlorophyll(spot["lat"], spot["lon"], cache)
    if chl_pack:
        chl_val, chl_t = chl_pack
        try:
            chl_dt = datetime.fromisoformat(str(chl_t).replace("Z", "+00:00"))
            if chl_dt.tzinfo is None:
                chl_dt = chl_dt.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            chl_dt = now
        age = max(0, (now - chl_dt).total_seconds() / 60)
        got["chl"], src["chl"] = (chl_val, chl_dt, None), {
            "source": "viirs-nrt (NOAA CoastWatch)", "age_min": round(age)
        }

    if spot.get("marine"):                    # satellite sea temperature and temperature breaks
        ss = sat_sst(spot["lat"], spot["lon"], cache)
        if ss:
            try:
                sdt = datetime.fromisoformat(str(ss["time"]).replace("Z", "+00:00"))
                sdt = sdt if sdt.tzinfo else sdt.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                sdt = now
            ssrc = {"source": "MUR satellite SST (NASA/JPL via CoastWatch)", "age_min": round(max(0, (now - sdt).total_seconds() / 60))}
            got["sbreak"], src["sbreak"] = (ss["break"], sdt, None), ssrc
            got["ssat"], src["ssat"] = (ss["sst_f"], sdt, None), ssrc

    if spot.get("marine"):                    # real buoy/platform readings beat the model
        done = set()
        for sid, dist in near_buoys(spot, cache):
            if ("ndbc", sid) not in cache:
                cache[("ndbc", sid)] = buoy_obs(sid)
                if cache[("ndbc", sid)] is None:          # no realtime file: skip it in future runs
                    with _BUOY_LOCK:
                        cache.setdefault("deadbuoys", [])
                        if sid not in cache["deadbuoys"]:
                            cache["deadbuoys"].append(sid)
                    cache[("ndbc", sid)] = {}
            for m, rec in cache[("ndbc", sid)].items():
                age = (now - rec[1]).total_seconds() / 60
                if m not in done and age <= STALE_MIN:
                    got[m], src[m] = rec, {"source": f"ndbc:{sid}", "age_min": round(age), "distance_mi": dist}
                    done.add(m)
    sbias = None
    if mar and mar["now"].get("sst") is not None and src.get("temp_f", {}).get("source", "").startswith("ndbc"):
        sbias = got["temp_f"][0] - mar["now"]["sst"]      # shift the model forecast to match the buoy
    pts = []
    if spot["tide"]:                          # several spots share a station: fetch it once
        tkey = ("tidepts", spot["tide"])
        if tkey not in cache:
            cache[tkey] = tide_pts(spot["tide"], now)
        pts = cache[tkey]
    tide, trange = stage_at(pts, now) if pts else (None, None)
    # next highs and lows for the page (48 h)
    tides = [{"t": t.isoformat(), "v": round(v, 2), "type": k} for t, v, k in pts
             if now - timedelta(hours=7) <= t <= now + timedelta(hours=48)] or None
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
    rain = got["rain"][0] if "rain" in got else None
    chl = got["chl"][0] if "chl" in got else None
    clar, clar_kind = clarity_label(turb, wind, sal, rain, chl)

    # Visibility (how many feet down you can see), best source first
    vis, vsrc = None, None
    kd = sat_kd490(spot["lat"], spot["lon"], cache, now)
    kd_age = None if not kd else (now - kd[1]).total_seconds() / 86400
    if turb is not None:
        vis = vis_from_turbidity(turb)
        vsrc = {**src.get("turb_fnu", {}), "method": "turbidity gauge"}
    elif kd and kd_age <= 4:
        vis = vis_from_kd490(kd[0])
        vsrc = {"source": f"satellite Kd490 ({kd[2]})", "age_min": round(kd_age * 1440), "method": "satellite"}
    elif spot.get("marine") and chl is not None:
        vis = vis_from_kd490(kd490_from_chl(chl))
        vsrc = {**src.get("chl", {}), "method": "satellite chlorophyll"}
    elif kd and kd_age <= 12:
        vis = vis_from_kd490(kd[0])
        vsrc = {"source": f"satellite Kd490 ({kd[2]})", "age_min": round(kd_age * 1440), "method": "satellite (older)"}
    if vis is not None and turb is None and not spot.get("marine") and \
            ((wind or 0) >= 20 or (rain or 0) >= 1.5):
        vis = min(vis, 1.5)                       # wind or heavy rain since the pass muddies inshore water
        vsrc["method"] += ", lowered for recent wind or rain"
    if vis is not None and turb is None and vsrc["method"].startswith("satellite"):
        clar, clar_kind = clarity_from_vis(vis), "satellite"     # measured clarity beats the chlorophyll guess
    if vis is None and not spot.get("marine"):    # last resort inshore: from the clarity estimate
        vis = {"Clear": 4.0, "Stained": 2.0, "Muddy": 0.8}[clar]
        vsrc = {"source": "estimated", "age_min": 0, "method": "estimated from wind, rain and salinity"}
    if vsrc:                                      # offshore with no satellite data: left missing
        src["vis"] = vsrc
    level = None
    if "level_d6" in got:
        d = got["level_d6"][0]
        level = "Rising" if d >= LEVEL_STEP else "Falling" if d <= -LEVEL_STEP else "Steady"
    wdir = round(got["wdir"][0]) if "wdir" in got else None
    wcolor = color_label(chl)

    wl = water_level_anomaly(spot["tide"], now, cache) if spot["tide"] else None
    if wl:
        src["wl_anom"] = {"source": f"coops:{spot['tide']}", "age_min": round((now - wl[1]).total_seconds() / 60)}
    rg = (cache.get("rivers") or {}).get(spot.get("river")) if spot.get("river") else None
    riv = river_index(rg)
    if riv:
        src["riv"] = {"source": f"nws:{rg['lid']}", "age_min": round(max(0, (now - parse_iso(rg["time"])).total_seconds()) / 60)}

    cond = {"temp": round(got["temp_f"][0]) if "temp_f" in got else None,
            "wind": round(wind) if wind is not None else None,
            "wdir": wdir,
            "pres": got["pres"][0] if "pres" in got else None,
            "tide": tide if tide else ("Slack" if not spot["tide"] else None),
            "sal": sal, "clar": clar,
            "chl": chl, "wcolor": wcolor,
            "vis": None if vis is None else round(vis, 1) if vis < 10 else round(vis),
            "kd490": round(kd[0], 3) if kd else None,
            "cloud": round(got["cloud"][0]) if "cloud" in got else None,
            "dtemp": round(got["temp_d24"][0], 1) if "temp_d24" in got else None,
            "rain": round(rain, 2) if rain is not None else None,
            "front": round(got["front"][0]) if "front" in got else None,
            "level": level,
            "trange": round(trange, 1) if trange is not None else None,
            "wave": round(got["wave"][0], 1) if "wave" in got else None,
            "cur": round(got["cur"][0], 1) if "cur" in got else None,
            "wl_anom": wl[0] if wl else None,
            "riv": riv,
            "sbreak": round(got["sbreak"][0], 2) if "sbreak" in got else None,
            "ssat": round(got["ssat"][0]) if "ssat" in got else None}
    na = []
    if not spot["tide"]:
        na += ["tide", "trange"]
    if not any(k == "usgs" for k, _ in spot["water"]):
        na.append("level")
    if spot.get("marine"):
        na += ["tide", "trange", "level", "sal"]  # clar can come from satellite now
    else:
        na += ["wave", "cur", "sbreak", "ssat"]
    # Optional enrichment fields — don't tank completeness when missing
    na += ["chl", "wcolor", "wdir", "kd490"]
    if not spot["tide"] or spot.get("marine"):
        na.append("wl_anom")
    if not spot.get("river"):
        na.append("riv")
    na = sorted(set(na))
    missing = [k for k, v in cond.items() if v is None and k not in na]
    applicable = len(cond) - len(na)
    depth_ft, depth_src = bottom_depth(spot, cache)
    mzone = marine_zone(spot, cache)
    return {"id": spot["id"], "name": spot["name"], "lat": spot["lat"], "lon": spot["lon"],
            "depth_ft": depth_ft, "depth_src": depth_src, "note": spot.get("note"), "river": spot.get("river"), "mzone": mzone,
            "updated": now.isoformat(),
            "zone": "offshore" if spot.get("marine") else "inshore",
            "alerts": get_alerts(spot["lat"], spot["lon"]),
            "tides": tides,
            "forecast": build_forecast(cond, wx, pts, now, mar, sbias) if wx and wx.get("series") else None,
            "conditions": cond,
            "quality": {"missing": missing, "sources": src,
                        "estimated": ["clar"] if clar_kind in ("estimated", "satellite") else [],
                        "clarity_method": clar_kind,
                        "vis_method": vsrc["method"] if vsrc else None,
                        "not_applicable": na,
                        "pressure_delta_3h_hpa": None if not wx or wx["pres_delta_hpa"] is None else round(wx["pres_delta_hpa"], 1),
                        "completeness": round(1 - len(missing) / applicable, 2)}}

# ---------------------------------------------------------------- storage / run
def save(results, now):
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS readings(
        id INTEGER PRIMARY KEY, spot TEXT, metric TEXT, value REAL, text TEXT,
        source TEXT, fetched_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix ON readings(spot, metric, fetched_at)")
    for r in results:
        for m, v in r["conditions"].items():
            if v is None or isinstance(v, dict):
                continue
            s = (r["quality"]["sources"].get(m) or {}).get("source")
            con.execute("INSERT INTO readings(spot,metric,value,text,source,fetched_at) VALUES(?,?,?,?,?,?)",
                        (r["id"], m, v if isinstance(v, (int, float)) else None,
                         v if isinstance(v, str) else None, s, now.isoformat()))
    con.commit(); con.close()

# ---------------------------------------------------------------- ocean features (map lines)
# Lines drawn on the map: temperature breaks, color (chlorophyll) edges, and the Loop Current /
# eddy outline from satellite sea surface height. Written to ocean.json.
OCEAN = "ocean.json"
BOX = (26.4, 30.5, -94.6, -87.4)          # lat0, lat1, lon0, lon1: Louisiana shelf and slope

def griddap_box(base, ds, var, lat0, lat1, lon0, lon1, stride=1, lat_desc=False, extra="[(0.0)]"):
    """(time, lats, lons, grid) from an ERDDAP griddap dataset for the newest time step.
    grid[i][j] is None where there's no data (land, clouds)."""
    la = f"[({lat1}):{stride}:({lat0})]" if lat_desc else f"[({lat0}):{stride}:({lat1})]"
    path = f"griddap/{ds}.json?{var}[(last)]{extra}{la}[({lon0}):{stride}:({lon1})]"
    if base == ERDDAP_MIRRORS[0]:
        j = erddap_json(path, timeout=60)
    else:
        j = get_json(f"{base}/{path}", tries=1, timeout=60)
    try:
        rows = j["table"]["rows"]
        names = j["table"]["columnNames"]
    except (TypeError, KeyError):
        return None
    ilat, ilon, ival = names.index("latitude"), names.index("longitude"), len(names) - 1
    lats = sorted({r[ilat] for r in rows})
    lons = sorted({r[ilon] for r in rows})
    li, lj = {v: i for i, v in enumerate(lats)}, {v: j for j, v in enumerate(lons)}
    grid = [[None] * len(lons) for _ in lats]
    for r in rows:
        v = r[ival]
        if isinstance(v, (int, float)) and v == v:          # skip NaN
            grid[li[r[ilat]]][lj[r[ilon]]] = float(v)
    return rows[0][0] if rows else None, lats, lons, grid

def contour(grid, lats, lons, level):
    """Marching squares: line segments where the grid crosses `level`. Cells with a gap
    (None) are skipped. Returns [[(lat, lon), (lat, lon)], ...]."""
    segs = []
    for i in range(len(lats) - 1):
        for j in range(len(lons) - 1):
            c = (grid[i][j], grid[i][j + 1], grid[i + 1][j + 1], grid[i + 1][j])   # SW, SE, NE, NW
            if any(v is None for v in c):
                continue
            corners = ((lats[i], lons[j]), (lats[i], lons[j + 1]), (lats[i + 1], lons[j + 1]), (lats[i + 1], lons[j]))
            pts = []
            for a in range(4):
                b = (a + 1) % 4
                va, vb = c[a], c[b]
                if (va < level) != (vb < level):
                    t = (level - va) / (vb - va)
                    pts.append((corners[a][0] + t * (corners[b][0] - corners[a][0]),
                                corners[a][1] + t * (corners[b][1] - corners[a][1])))
            if len(pts) == 2:
                segs.append(pts)
            elif len(pts) == 4:                                   # saddle: pair them in order
                segs += [pts[:2], pts[2:]]
    return segs

def join_segments(segs, nd=3):
    """Join touching segments into longer lines to keep ocean.json small."""
    key = lambda p: (round(p[0], nd), round(p[1], nd))
    ends, lines = {}, []
    for a, b in segs:
        a, b = key(a), key(b)
        la, lb = ends.pop(a, None), ends.pop(b, None)
        if la is not None and lb is not None and la is not lb:
            if la[-1] != a: la.reverse()
            if lb[0] != b: lb.reverse()
            la.extend(lb)
            lines.remove(lb)
            ends.pop(la[0], None); ends.pop(la[-1], None)
            ends[la[0]] = la; ends[la[-1]] = la
        elif la is not None:
            (la.append(b) if la[-1] == a else la.insert(0, b))
            ends[b] = la
        elif lb is not None:
            (lb.insert(0, a) if lb[0] == b else lb.append(a))
            ends[a] = lb
        else:
            ln = [a, b]
            lines.append(ln)
            ends[a] = ln; ends[b] = ln
    return [ln for ln in lines if len(ln) >= 3]

def gradient(grid, lats, lons, i, j):
    """Magnitude of change per mile at grid cell (i, j), or None."""
    def g(ii, jj):
        return grid[ii][jj] if 0 <= ii < len(lats) and 0 <= jj < len(lons) else None
    c, n, s_, e, w = g(i, j), g(i + 1, j), g(i - 1, j), g(i, j + 1), g(i, j - 1)
    if c is None:
        return None
    dy = ((n if n is not None else c) - (s_ if s_ is not None else c))
    dx = ((e if e is not None else c) - (w if w is not None else c))
    mi_lat = 69.0 * (lats[min(i + 1, len(lats) - 1)] - lats[max(i - 1, 0)]) or 1
    mi_lon = 69.0 * math.cos(math.radians(lats[i])) * (lons[min(j + 1, len(lons) - 1)] - lons[max(j - 1, 0)]) or 1
    return math.hypot(dy / mi_lat, dx / mi_lon)

def breaks(grid, lats, lons, step, min_grad, keep_share=0.25):
    """Contours every `step` units, keeping only pieces that sit on a sharp change."""
    vals = [v for row in grid for v in row if v is not None]
    if len(vals) < 50:
        return []
    lo, hi = min(vals), max(vals)
    level = math.ceil(lo / step) * step
    scored = []
    while level < hi:
        for seg in contour(grid, lats, lons, level):
            mid = ((seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2)
            i = min(range(len(lats)), key=lambda k: abs(lats[k] - mid[0]))
            j = min(range(len(lons)), key=lambda k: abs(lons[k] - mid[1]))
            gr = gradient(grid, lats, lons, i, j)
            if gr is not None:
                scored.append((gr, seg))
        level += step
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    cut = max(min_grad, scored[int(len(scored) * keep_share) - 1][0] if len(scored) > 4 else min_grad)
    return join_segments([seg for gr, seg in scored if gr >= cut])

def ocean_features(now):
    lat0, lat1, lon0, lon1 = BOX
    out = {"generated": now.isoformat()}
    # temperature breaks from MUR sea surface temperature (deg C -> F), about 2 mi cells
    g = griddap_box("https://coastwatch.pfeg.noaa.gov/erddap", "jplMURSST41", "analysed_sst",
                    lat0, lat1, lon0, lon1, stride=3, extra="")
    if g:
        t, la, lo, gr = g
        gr = [[None if v is None else v * 1.8 + 32 for v in row] for row in gr]
        out["sst_breaks"] = {"time": t, "lines": breaks(gr, la, lo, 1.0, 0.12)}
    # color lines: where chlorophyll crosses 0.3 and 1 mg/m3 (blue to green-blue to green water)
    g = griddap_box("https://coastwatch.pfeg.noaa.gov/erddap", "nesdisVHNnoaa20chlaDaily", "chlor_a",
                    lat0, lat1, lon0, lon1, stride=1, lat_desc=True)
    if g:
        t, la, lo, gr = g
        lg = [[None if v is None or v <= 0 else math.log10(v) for v in row] for row in gr]
        out["color_lines"] = {"time": t, "levels": {
            "0.3": join_segments(contour(lg, la, lo, math.log10(0.3))),
            "1": join_segments(contour(lg, la, lo, 0.0))}}
    # Loop Current and eddies: sea surface height 17 cm above the Gulf average (Leben's method)
    for ds in (f"SSH_GoA_{now.year}", f"SSH_GoA_{now.year - 1}"):
        g = griddap_box("https://gcoos5.geos.tamu.edu/erddap", ds, "adt", 18.0, 31.0, -98.0, -80.0, extra="")
        if not g:
            continue
        t, la, lo, gr = g
        try:
            age = (now - parse_iso(t)).days
        except (TypeError, ValueError):
            age = 99
        if age > 10:
            continue
        gulf = [v for i, row in enumerate(gr) if la[i] >= 22 for v in row if v is not None]
        if not gulf:
            continue
        mean = sum(gulf) / len(gulf)
        rel = [[None if v is None else v - mean for v in row] for row in gr]
        out["ssh"] = {"time": t, "warm": join_segments(contour(rel, la, lo, 0.17)),
                      "cold": join_segments(contour(rel, la, lo, -0.10))}
        break
    def rnd(lines):
        return [[[round(a, 3), round(b, 3)] for a, b in ln] for ln in lines]
    for k in ("sst_breaks", "ssh"):
        if k in out:
            for kk in ("lines", "warm", "cold"):
                if kk in out[k]:
                    out[k][kk] = rnd(out[k][kk])
    if "color_lines" in out:
        out["color_lines"]["levels"] = {k: rnd(v) for k, v in out["color_lines"]["levels"].items()}
    return out

# ---------------------------------------------------------------- rigs and reefs (weekly)
RIGS, REEFS = "rigs.json", "reefs.json"
STATIC_DAYS = 7

def arcgis_geojson(url, where="1=1", bbox=None, max_pages=10, page=2000):
    """All features from an ArcGIS layer query as GeoJSON features (paged)."""
    feats = []
    for k in range(max_pages):
        params = dict(where=where, outFields="*", returnGeometry="true", outSR="4326", f="geojson",
                      resultOffset=k * page, resultRecordCount=page)
        if bbox:
            params.update(geometry=",".join(map(str, bbox)), geometryType="esriGeometryEnvelope",
                          inSR="4326", spatialRel="esriSpatialRelIntersects")
        j = get_json(url + "/query", params, tries=2, timeout=60)
        fs = (j or {}).get("features")
        if fs is None:
            return feats or None
        feats += fs
        if len(fs) < page and not (j or {}).get("exceededTransferLimit"):
            break
    return feats

def point_of(geom):
    """Representative (lat, lon) for a point or polygon geometry."""
    if not geom:
        return None
    t, c = geom.get("type"), geom.get("coordinates")
    try:
        if t == "Point":
            return c[1], c[0]
        ring = c[0] if t == "Polygon" else c[0][0]
        return sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)
    except (TypeError, IndexError, ZeroDivisionError):
        return None

def static_layers(now, rigs_path=RIGS, reefs_path=REEFS):
    """Refresh rigs.json and reefs.json about once a week (the workflow downloads the
    current copies from the live site first)."""
    def stale(path):
        try:
            with open(path) as f:
                return (now - parse_iso(json.load(f)["generated"])).days >= STATIC_DAYS
        except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            return True
    bbox = (-94.5, 26.4, -87.5, 30.6)
    try:
        _rigs(now, rigs_path, bbox, stale)
    except Exception:
        log.exception("rigs.json update failed")
    try:
        _reefs(now, reefs_path, stale)
    except Exception:
        log.exception("reefs.json update failed")

def _rigs(now, rigs_path, bbox, stale):
    if stale(rigs_path):
        feats = None
        for url in ("https://gis.boem.gov/arcgis/rest/services/BOEM_BSEE/GOM_Layers/MapServer/0",
                    "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/12"):
            feats = arcgis_geojson(url, bbox=bbox)
            if feats:
                break
        if feats:
            pts = []
            for f in feats:
                p = f.get("properties") or {}
                if any(p.get(k) for k in ("REMOVAL_DATE", "removal_da", "REMOVAL_DT")):
                    continue                                       # removed structures
                ll = point_of(f.get("geometry"))
                if not ll:
                    continue
                name = next((str(p[k]) for k in ("STRUCTURE_NAME", "STR_NAME", "str_name", "AREA_BLOCK",
                                                  "COMPLEX_ID_NUM", "complex_id") if p.get(k)), "")
                pts.append([round(ll[0], 4), round(ll[1], 4), name])
            with open(rigs_path, "w") as fh:
                json.dump({"generated": now.isoformat(), "source": "BOEM/BSEE OCS platforms", "rigs": pts}, fh,
                          separators=(",", ":"))
            log.info("rigs.json: %d platforms", len(pts))
        else:
            log.warning("rigs.json: no platform source answered; will retry next run")

def _reefs(now, reefs_path, stale):
    if stale(reefs_path):
        feats = arcgis_geojson("https://gis.ngdc.noaa.gov/arcgis/rest/services/NMSP/FGB/MapServer/23")
        if feats:
            reefs = []
            for f in feats:
                p, ll = f.get("properties") or {}, point_of(f.get("geometry"))
                if ll:
                    name = next((str(p[k]) for k in ("multilabel", "NAME", "Name", "REEF_NAME", "SITE_NAME") if p.get(k)), "Artificial reef")
                    reefs.append([round(ll[0], 4), round(ll[1], 4), name])
            with open(reefs_path, "w") as fh:
                json.dump({"generated": now.isoformat(), "source": "LDWF artificial reefs (via NOAA)", "reefs": reefs}, fh,
                          separators=(",", ":"))
            log.info("reefs.json: %d reefs", len(reefs))

# ---------------------------------------------------------------- NWS marine forecasts
def marine_zone(spot, cache):
    """NWS coastal or offshore marine zone id covering a spot (looked up once, cached)."""
    key = ("mzone", spot["id"])
    if key in cache:
        return cache[key][0] if isinstance(cache[key], (list, tuple)) else cache[key]
    for typ in ("coastal", "offshore"):
        j = get_json("https://api.weather.gov/zones", dict(type=typ, point=f"{spot['lat']:.4f},{spot['lon']:.4f}"),
                     tries=2, headers={"Accept": "application/geo+json"})
        fs = (j or {}).get("features") or []
        if fs:
            zid = (fs[0].get("properties") or {}).get("id")
            if zid:
                cache[key] = [zid, typ]
                return zid
        if j is None:
            return None                                # API down: try again next run
    cache[key] = [None, None]
    return None

def marine_forecast(zone, cache):
    """First few periods of the NWS marine forecast text for a zone."""
    key = ("mfc", zone)
    if key in cache:
        return cache[key]
    z = zone.lower()
    txt = None
    for d in ("coastal", "offshore"):
        txt = get_text(f"https://tgftp.nws.noaa.gov/data/forecasts/marine/{d}/gm/{z}.txt")
        if txt and zone.upper() in txt.upper():
            break
        txt = None
    out = None
    if txt:
        lines = [l.rstrip() for l in txt.splitlines()]
        zi = next((i for i, l in enumerate(lines) if l.upper().startswith(zone.upper())), None)
        name = next((l.strip().rstrip("-").strip() for l in lines[zi + 1:zi + 4] if l.strip()), zone) if zi is not None else zone
        issued = next((l.strip() for l in lines if " CST " in l or " CDT " in l), "")
        periods, cur = [], None
        for l in lines:
            if l.startswith(".") and not l.startswith("..") and "..." in l:     # ".TODAY...text" (not a "...HEADLINE...")
                if cur:
                    periods.append(cur)
                head, _, rest = l[1:].partition("...")
                cur = {"name": head.strip().title(), "text": rest.strip()}
            elif cur is not None and l.strip() and not l.startswith(("$$", "...")):
                cur["text"] += " " + l.strip()
            elif l.startswith("$$"):
                break
        if cur:
            periods.append(cur)
        headline = next((l.strip(". ") for l in lines if l.startswith("...") and l.rstrip().endswith("...")), None)
        out = {"zone": zone, "name": name, "issued": issued, "headline": headline,
               "periods": periods[:4],
               "url": f"https://forecast.weather.gov/shmrn.php?mz={z}"}
    cache[key] = out
    return out

# ---------------------------------------------------------------- history
HISTORY = "history.json"
HIST_DAYS = 14
HIST_KEYS = ("temp", "sal", "wind", "vis", "wl_anom")     # per spot, one value per run

def update_history(results, river_data, now, path=HISTORY):
    """Append this run's readings to history.json and keep the last HIST_DAYS days.
    The workflow downloads the previous file from the live site first, so history
    survives between runs without a database."""
    try:
        with open(path) as f:
            h = json.load(f)
        if not isinstance(h.get("t"), list):
            raise ValueError("bad history")
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        h = {"t": [], "spots": {}, "rivers": {}}
    t = h["t"]
    n_old = len(t)
    def series(group, key, name):
        s = h.setdefault(group, {}).setdefault(key, {}).setdefault(name, [])
        s.extend([None] * (n_old - len(s)))          # pad a spot or river that's new
        return s
    t.append(now.strftime("%Y-%m-%dT%H:%MZ"))
    for r in results:
        c = r["conditions"]
        for k in HIST_KEYS:
            v = c.get(k)
            series("spots", r["id"], k).append(None if not isinstance(v, (int, float)) else round(v, 1))
    for key, g in (river_data or {}).items():
        series("rivers", key, "stage").append(g.get("stage"))
    # pad anything not written this run, then trim to the window
    for group in ("spots", "rivers"):
        for d in h.get(group, {}).values():
            for s in d.values():
                s.extend([None] * (len(t) - len(s)))
    cutoff = (now - timedelta(days=HIST_DAYS)).strftime("%Y-%m-%dT%H:%MZ")
    keep = next((i for i, x in enumerate(t) if x >= cutoff), len(t))
    h["t"] = t[keep:]
    for group in ("spots", "rivers"):
        for d in h.get(group, {}).values():
            for k in d:
                d[k] = d[k][keep:]
    h["generated"] = now.isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, separators=(",", ":"))
    os.replace(tmp, path)
    return h

def run_once():
    now, cache, results = datetime.now(UTC), {}, []
    disk = load_station_cache()
    hydrate_station_cache(cache, disk)
    failed = []
    def one(spot):
        try:
            return collect(spot, now, cache)
        except Exception:                       # one bad spot must not stop the rest
            log.exception("spot %s failed", spot["id"])
            return None
    # Spots are fetched a few at a time. The shared cache may let two threads fetch the
    # same station once each; that only costs a duplicate request.
    t0 = time.time()
    try:
        cache["rivers"] = rivers(now)
    except Exception:
        log.exception("river gauges failed")
        cache["rivers"] = {}
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        for spot, r in zip(SPOTS, ex.map(one, SPOTS)):
            if r is None:
                failed.append(spot["id"])
            else:
                results.append(r)
    log.info("collected %d spots in %.0f s", len(results), time.time() - t0)
    marine = {}
    for z in sorted({r.get("mzone") for r in results if r.get("mzone")}):
        try:
            fc = marine_forecast(z, cache)
            if fc:
                marine[z] = fc
        except Exception:
            log.exception("marine forecast %s failed", z)
    cache["marine"] = marine
    for name, fn in (("ocean lines", lambda: ocean_features(now)), ("rigs and reefs", lambda: static_layers(now))):
        try:
            res = fn()
            if name == "ocean lines" and res:
                with open(OCEAN + ".tmp", "w") as fh:
                    json.dump(res, fh, separators=(",", ":"))
                os.replace(OCEAN + ".tmp", OCEAN)
                log.info("ocean.json: %s", ", ".join(k for k in res if k != "generated") or "no layers")
        except Exception:                          # map extras must never break the run
            log.exception("%s failed", name)
    save_station_cache(cache)
    fc = {"generated": now.isoformat(), "spots": {r["id"]: r["forecast"] for r in results if r.get("forecast")}}
    for r in results:
        r.pop("forecast", None)
    save(results, now)
    with open("forecast.json.tmp", "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    os.replace("forecast.json.tmp", "forecast.json")
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated": now.isoformat(), "rivers": cache.get("rivers") or {}, "marine": cache.get("marine") or {},
                   "spots": results}, f, indent=2)
    os.replace(tmp, OUT)
    # Lightweight health summary for the UI and operators
    status = {
        "generated": now.isoformat(),
        "spots_ok": len(results),
        "spots_failed": failed,
        "avg_completeness": round(sum(r["quality"]["completeness"] for r in results) / max(len(results), 1), 2),
        "spots": {
            r["id"]: {
                "name": r["name"],
                "completeness": r["quality"]["completeness"],
                "missing": r["quality"]["missing"],
                "sources": {k: (v.get("source") if isinstance(v, dict) else v)
                            for k, v in r["quality"].get("sources", {}).items()},
            }
            for r in results
        },
    }
    try:
        update_history(results, cache.get("rivers"), now)
    except Exception:                          # history is a nice-to-have; never fail the run
        log.exception("history update failed")
    with open(STATUS_OUT + ".tmp", "w") as f:
        json.dump(status, f, indent=2)
    os.replace(STATUS_OUT + ".tmp", STATUS_OUT)
    for r in results:
        q = r["quality"]
        log.info("%-8s completeness %.0f%% missing=%s", r["id"], q["completeness"] * 100, q["missing"] or "-")
    if failed:
        log.warning("failed spots: %s", ", ".join(failed))

def discover():
    for spot in SPOTS:
        print(f"\n== {spot['name']}")
        for kind, sid in spot["water"]:
            if kind == "coops":
                j = get_json(MDAPI % sid)
                names = [p["name"] for p in (j or {}).get("products", [])] or ["(lookup failed)"]
                print(f"  CO-OPS {sid}: {', '.join(names)}")
            else:
                feats = wd_items("time-series-metadata", dict(monitoring_location_id=f"USGS-{sid}",
                                                              skipGeometry="true", limit=200))
                names = sorted({f"{(f.get('properties') or {}).get('parameter_code')} "
                                f"{(f.get('properties') or {}).get('parameter_description') or (f.get('properties') or {}).get('parameter') or ''}".strip()
                                for f in feats or []}) or ["(lookup failed)"]
                print(f"  USGS {sid}: {'; '.join(names)}")

def check_usgs(site="07381515"):
    """Quick test of the new USGS API: prints what one gauge returned and whether the legacy
    fallback was needed. Run it after setting USGS_API_KEY."""
    print(f"API key: {'set' if USGS_KEY else 'NOT set (requests are rate limited)'}")
    new = usgs_source_new(site)
    print(f"New API, USGS-{site}: {sorted(new) if new else 'nothing returned'}")
    if new:
        for k, (v, t, _) in sorted(new.items()):
            print(f"  {k:10s} {v:8.2f}  at {t.isoformat()}")
    else:
        old = usgs_source_legacy(site)
        print(f"Legacy service: {sorted(old) if old else 'nothing returned'}")
    near = near_sites({"id": "check", "lat": 29.95, "lon": -91.15, "water": []}, {}, "00095")
    print(f"Conductance gauges near Lake Verret: {near or 'none found'}")

def tile_ok(url):
    """(ok, detail) for one map tile: ok means a real, non-blank image came back."""
    try:
        with urlopen(Request(url, headers={"User-Agent": f"{APP} ({CONTACT})"}), timeout=25) as r:
            body = r.read()
    except (URLError, TimeoutError, OSError) as e:
        return False, str(e)
    img = body[:4] == b"\x89PNG" or body[:3] == b"\xff\xd8\xff"
    return img and len(body) > 1500, f"{len(body)} bytes, {'image' if img else 'not an image'}"   # tiny PNGs are blank

GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/%s/default/%s/GoogleMapsCompatible_Level7/6/26/15.png"

def gibs_newest(layer, days=12):
    """Newest day (within `days`) a NASA GIBS layer has imagery over Louisiana, or None."""
    for i in range(days):
        day = (datetime.now(UTC) - timedelta(days=i)).strftime("%Y-%m-%d")
        if tile_ok(GIBS % (layer, day))[0]:
            return day
    return None

def map_checks():
    R = 20037508.342789244; w = 2 * R / 2 ** 7; x0 = -R + 32 * w; y1 = R - 53 * w
    out = []
    ok, d = tile_ok("https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/NOAACharts/MapServer/tile/7/212/127")
    out.append(("Map: NOAA nautical chart", ok, d))
    ok, d = tile_ok("https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_global_mosaic_hillshade/ImageServer/exportImage"
                    f"?bbox={x0},{y1 - w},{x0 + w},{y1}&bboxSR=3857&imageSR=3857&size=256,256&format=jpgpng&f=image")
    out.append(("Map: NOAA NCEI depth shading", ok, d))
    day = gibs_newest("GHRSST_L4_MUR_Sea_Surface_Temperature")
    out.append(("Map: sea temperature (NASA MUR)", bool(day), f"newest {day}" if day else "no image in 12 days"))
    chl = [(l, gibs_newest(l)) for l in ("VIIRS_NOAA21_Chlorophyll_a", "VIIRS_NOAA20_Chlorophyll_a", "OCI_PACE_Chlorophyll_a")]
    best = max((d for _, d in chl if d), default=None)
    out.append(("Map: water color (NASA chlorophyll)", bool(best),
                ", ".join(f"{l.split('_')[1]} {d or 'none'}" for l, d in chl)))
    m, src = depth_m(28.6375, -89.5568)
    out.append(("Depth lookup", m is not None, f"{src}: {round(-m * 3.28084)} ft at Midnight Lump" if m is not None else "no source answered"))
    return out

def check_map():
    """Tests the chart, depth and satellite map sources from this computer."""
    for name, ok, d in map_checks():
        print(f"  {'OK  ' if ok else 'FAIL'} {name}: {d}")

def health(out_md="health.md"):
    """Checks every data source the site depends on. Writes a Markdown report and returns
    the number of failures (critical sources: CO-OPS, Open-Meteo, NWS alerts)."""
    now, res = datetime.now(UTC), []
    def add(name, ok, detail, critical=False):
        res.append((name, bool(ok), detail, critical))
    d = coops_latest("8761724", "water_temperature")
    add("NOAA tides and currents (Grand Isle)", d, f"water temp {d.get('v')} F at {d.get('t')} UTC" if d else "no data", True)
    wx = weather(29.27, -89.95)
    add("Open-Meteo weather", wx and wx.get("series"), "forecast OK" if wx else "no data", True)
    al = get_json("https://api.weather.gov/alerts/active", dict(point="29.27,-89.95"), tries=2)
    add("NWS alerts", al is not None, f"{len((al or {}).get('features', []))} active" if al is not None else "no answer", True)
    u = usgs_source_new("07381515")
    add("USGS Water Data API (new)", u, ", ".join(sorted(u)) if u else
        f"nothing returned{'' if USGS_KEY else ' (no USGS_API_KEY set)'}")
    g = river_gauge("NORL1", now)
    add("NWS river gauges", g, f"Mississippi at New Orleans {g['stage']} ft" if g else "no data")
    kd = sat_kd490(29.27, -89.95, {}, now)
    add("Satellite water clarity (Kd490)", kd, f"{kd[2]}, {kd[1]:%Y-%m-%d}" if kd else "no dataset answered")
    b = buoy_obs("42040")
    add("NDBC buoy 42040", b, ", ".join(sorted(b)) or "no data")
    now_y = datetime.now(UTC).year
    ssh = griddap_box("https://gcoos5.geos.tamu.edu/erddap", f"SSH_GoA_{now_y}", "adt", 28.0, 29.0, -90.0, -89.0, extra="")
    add("Sea surface height (Loop Current)", ssh, f"newest {ssh[0]}" if ssh else "no answer")
    rigs = arcgis_geojson("https://gis.boem.gov/arcgis/rest/services/BOEM_BSEE/GOM_Layers/MapServer/0",
                          bbox=(-90.5, 28.5, -90.0, 29.0), max_pages=1, page=50)
    add("Offshore platforms (BOEM)", rigs, f"{len(rigs)} near Grand Isle" if rigs else "no answer")
    reefs = arcgis_geojson("https://gis.ngdc.noaa.gov/arcgis/rest/services/NMSP/FGB/MapServer/23", max_pages=1, page=5)
    add("Artificial reefs (LDWF via NOAA)", reefs, f"{len(reefs)} returned" if reefs else "no answer")
    mf = marine_forecast("GMZ550", {})
    add("NWS marine forecast text", mf, (mf["periods"][0]["name"] + ": " + mf["periods"][0]["text"][:60]) if mf and mf["periods"] else "no answer")
    for name, ok, detail in map_checks():
        add(name, ok, detail)
    fails = [r for r in res if not r[1]]
    lines = [f"Checked {now:%Y-%m-%d %H:%M} UTC. {len(res) - len(fails)} of {len(res)} sources OK.", "",
             "| Source | Status | Detail |", "|---|---|---|"]
    lines += [f"| {n} | {'OK' if ok else ('**FAIL** (critical)' if c else '**FAIL**')} | {str(dt).replace('|', '/')} |"
              for n, ok, dt, c in res]
    with open(out_md, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return len(fails)

def validate(path=OUT, min_spots=15, min_completeness=0.45, max_age_h=3):
    """Sanity-check a freshly written latest.json before it is deployed. Returns problems."""
    problems = []
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [f"can't read {path}: {e}"]
    spots = d.get("spots") or []
    if len(spots) < min_spots:
        problems.append(f"only {len(spots)} spots (expected at least {min_spots})")
    if spots:
        avg = sum((s.get("quality") or {}).get("completeness", 0) for s in spots) / len(spots)
        if avg < min_completeness:
            problems.append(f"average completeness {avg:.0%} is below {min_completeness:.0%}")
    try:
        age = (datetime.now(UTC) - parse_iso(d["generated"])).total_seconds() / 3600
        if age > max_age_h:
            problems.append(f"generated {age:.1f} h ago")
    except (KeyError, TypeError, ValueError):
        problems.append("no valid 'generated' time")
    return problems

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "discover":
        discover()
    elif cmd == "check-map":
        check_map()
    elif cmd == "health":
        sys.exit(1 if health() else 0)
    elif cmd in ("validate", "validate-file"):
        probs = validate(sys.argv[2]) if cmd == "validate-file" and len(sys.argv) > 2 else validate()
        for pr in probs:
            print("PROBLEM:", pr)
        print("latest.json looks good" if not probs else "latest.json failed validation")
        sys.exit(1 if probs else 0)
    elif cmd == "check-usgs":
        check_usgs(*sys.argv[2:3])
    elif cmd == "loop":
        mins = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        while True:
            run_once()
            time.sleep(mins * 60)
    else:
        run_once()
