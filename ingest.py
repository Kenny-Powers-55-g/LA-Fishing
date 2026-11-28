#!/usr/bin/env python3
"""
Louisiana fishing conditions: ingestion worker (prototype)

Pulls public data for each spot, normalizes it, stores history in SQLite and
writes latest.json in the shape the scoring page expects.

Sources
  NOAA CO-OPS   water temp, conductivity (-> salinity), wind, tide predictions
  USGS Water Data APIs (api.waterdata.usgs.gov)
                water temp, specific conductance (-> salinity), turbidity, gage height and lake
                elevation (-> level trend), dissolved oxygen, diversion flows
  Open-Meteo    wind, cloud cover, surface pressure (-> 3 hr trend), 10 days of air temperature
                (-> lake water temperature where there's no gauge), marine model
  NOAA CoastWatch ERDDAP
                Geo-polar Blended satellite sea temperature and temperature breaks, blended altimetry
                (-> Loop Current and eddy edges), VIIRS chlorophyll and water clarity
  NDBC buoys, NWS alerts, NWS forecast grid (wind, gusts, waves; surf height at beach spots)
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
import io, json, logging, math, os, re, sqlite3, sys, threading, time, zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
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
        if isinstance(k, tuple) and k and k[0] in ("sites", "buoys", "depth", "mzone", "cstn", "ngrid"):
            # JSON keys must be strings
            out["|".join(str(x) for x in k)] = v
        elif k in ("buoys", "deadbuoys", "deadbuoys_t", "cstations", "launches_tried", "div_open") and v:
            out[k] = v
    try:
        with open(STATION_CACHE + ".tmp", "w") as f:
            json.dump(out, f)
        os.replace(STATION_CACHE + ".tmp", STATION_CACHE)
    except OSError as e:
        log.warning("could not write station cache: %s", e)

def hydrate_station_cache(cache, disk):
    for k, v in (disk or {}).items():
        if k in ("buoys", "deadbuoys", "deadbuoys_t", "cstations", "launches_tried", "div_open"):
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
    # Gulf beaches, fished from the surf: wave height from the NWS forecast grid counts here
    {"id": "gibeach", "name": "Grand Isle beach (surf)", "lat": 29.235, "lon": -90.00, "surf": True,
     "note": "Gulf-side beach and the state park; wade the troughs.",
     "water": [("coops", "8761724")], "tide": "8761724"},
    {"id": "elmers", "name": "Elmer's Island (surf)", "lat": 29.18, "lon": -90.08, "surf": True,
     "note": "Wildlife refuge beach west of Grand Isle; walk-in access.",
     "water": [("coops", "8761724")], "tide": "8761724"},
    {"id": "fourchonbeach", "name": "Fourchon Beach (surf)", "lat": 29.10, "lon": -90.21, "surf": True,
     "note": "Beach west of Belle Pass; check access rules before you drive it.",
     "water": [("coops", "8762075")], "tide": "8762075"},
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
    # ---- Added spots. Coordinates are approximate centers. Coastal spots use the nearest NOAA tide
    # prediction station; where no NOAA water station is close, the worker finds the nearest USGS gauges.
    # Pontchartrain basin and the east side
    {"id": "borgne", "name": "Lake Borgne", "lat": 30.03, "lon": -89.6,
     "water": [("coops", "8761305")], "tide": "8761305"},
    {"id": "rigolets", "name": "Rigolets / Lake St. Catherine", "lat": 30.17, "lon": -89.74,
     "water": [], "tide": "8761402"},
    {"id": "chef", "name": "Chef Menteur Pass", "lat": 30.07, "lon": -89.8,
     "water": [], "tide": "8761487"},
    {"id": "maurepas", "name": "Lake Maurepas", "lat": 30.25, "lon": -90.5,
     "water": [], "tide": None},
    {"id": "shellbeach", "name": "Shell Beach / MRGO", "lat": 29.86, "lon": -89.68,
     "water": [("coops", "8761305")], "tide": "8761305"},
    {"id": "hopedale", "name": "Hopedale / Lake Robin", "lat": 29.82, "lon": -89.61,
     "water": [("coops", "8761305")], "tide": "8761305"},
    {"id": "delacroix", "river": "miss", "name": "Delacroix", "lat": 29.76, "lon": -89.79,
     "water": [("coops", "8761305")], "tide": "8761305"},
    {"id": "biloximarsh", "name": "Biloxi Marsh", "lat": 30.05, "lon": -89.35,
     "water": [], "tide": "8760668"},
    {"id": "chandeleur", "name": "Chandeleur Islands", "lat": 29.85, "lon": -88.85,
     "water": [], "tide": "8760172",
     "note": "Remote barrier islands about 30 miles offshore. Open-water run: check the marine forecast."},
    {"id": "blackbay", "river": "miss", "name": "Black Bay / Breton Sound", "lat": 29.6, "lon": -89.55,
     "water": [], "tide": "8761108"},
    {"id": "palh", "river": "miss", "name": "Pointe a la Hache", "lat": 29.57, "lon": -89.8,
     "water": [], "tide": "8761431"},
    {"id": "empire", "river": "miss", "name": "Empire / Buras", "lat": 29.38, "lon": -89.6,
     "water": [], "tide": "8761115"},
    # Barataria basin
    {"id": "lafitte", "name": "Lafitte / Barataria", "lat": 29.67, "lon": -90.11,
     "water": [], "tide": "8761899"},
    {"id": "salvador", "name": "Lake Salvador", "lat": 29.75, "lon": -90.27,
     "water": [("coops", "8762482")], "tide": "8761899"},
    {"id": "cataouatche", "name": "Lake Cataouatche", "lat": 29.83, "lon": -90.23,
     "water": [], "tide": "8761899"},
    {"id": "allemands", "name": "Lac des Allemands", "lat": 29.92, "lon": -90.57,
     "water": [], "tide": None, "fresh": True},
    {"id": "myrtlegrove", "river": "miss", "name": "Myrtle Grove", "lat": 29.63, "lon": -89.94,
     "water": [], "tide": "8761899"},
    {"id": "leeville", "name": "Leeville / Bayou Lafourche", "lat": 29.25, "lon": -90.21,
     "water": [], "tide": "8762084"},
    {"id": "goldenmeadow", "name": "Golden Meadow / Catfish Lake", "lat": 29.38, "lon": -90.27,
     "water": [], "tide": "8762184"},
    # Terrebonne and the central coast
    {"id": "timbalier", "name": "Timbalier Bay", "lat": 29.15, "lon": -90.35,
     "water": [], "tide": "8762481"},
    {"id": "pelto", "name": "Lake Pelto / Isles Dernieres", "lat": 29.07, "lon": -90.75,
     "water": [], "tide": "8763206"},
    {"id": "pac", "name": "Pointe-aux-Chenes", "lat": 29.5, "lon": -90.56,
     "water": [], "tide": "8762525"},
    {"id": "decade", "name": "Lake Decade", "lat": 29.42, "lon": -90.92,
     "water": [], "tide": "8763843"},
    {"id": "mechant", "name": "Lake Mechant / Caillou Lake", "lat": 29.3, "lon": -90.95,
     "water": [], "tide": "8763535"},
    {"id": "fourleague", "river": "atch", "name": "Four League Bay", "lat": 29.3, "lon": -91.2,
     "water": [("coops", "8764314")], "tide": "8763843"},
    {"id": "atchbay", "river": "atch", "name": "Atchafalaya Bay / Wax Lake Delta", "lat": 29.5, "lon": -91.42,
     "water": [("coops", "8764227")], "tide": "8764227"},
    {"id": "vermilion", "river": "atch", "name": "Vermilion Bay (Cypremort Point)", "lat": 29.72, "lon": -91.87,
     "water": [], "tide": "8765251"},
    {"id": "marshisland", "name": "Marsh Island / Southwest Pass", "lat": 29.6, "lon": -92.0,
     "water": [], "tide": "8765551"},
    {"id": "intracoastal", "name": "Intracoastal City / Vermilion River", "lat": 29.78, "lon": -92.16,
     "water": [], "tide": "8765785"},
    # Southwest
    {"id": "whitelake", "name": "White Lake", "lat": 29.73, "lon": -92.5,
     "water": [], "tide": None, "fresh": True},
    {"id": "rockefeller", "name": "Rockefeller Refuge / Grand Chenier", "lat": 29.72, "lon": -92.8,
     "water": [], "tide": "8766941"},
    {"id": "grandlake", "name": "Grand Lake / Mermentau River", "lat": 29.9, "lon": -92.8,
     "water": [], "tide": None, "fresh": True},
    {"id": "cameron", "name": "Cameron Jetties", "lat": 29.765, "lon": -93.345,
     "water": [("coops", "8768094")], "tide": "8768094"},
    {"id": "holly", "name": "Holly Beach (surf)", "lat": 29.77, "lon": -93.46, "surf": True,
     "note": "Long, open beach west of Cameron, easy road access.",
     "water": [("coops", "8768094")], "tide": "8768094"},
    {"id": "rutherford", "name": "Rutherford Beach (surf)", "lat": 29.76, "lon": -93.12, "surf": True,
     "note": "Quiet beach east of Cameron at the end of Rutherford Beach Rd.",
     "water": [("coops", "8768094")], "tide": "8768094"},
    {"id": "lakecharles", "name": "Lake Charles / Prien Lake", "lat": 30.2, "lon": -93.26,
     "water": [("coops", "8767816")], "tide": "8767816"},   # not 8767961 (Bulk Terminal): reads ~6 F warmer than every gauge nearby
    {"id": "sabine", "name": "Sabine Lake", "lat": 29.85, "lon": -93.87,
     "water": [("coops", "8770475")], "tide": "8769858"},
    # Freshwater: south and central
    {"id": "henderson", "river": "atch", "name": "Henderson Lake", "lat": 30.33, "lon": -91.75,
     "water": [], "tide": None, "fresh": True},
    {"id": "falseriver", "name": "False River", "lat": 30.68, "lon": -91.45,
     "water": [], "tide": None, "fresh": True},
    {"id": "tchefuncte", "name": "Tchefuncte River (Madisonville)", "lat": 30.4, "lon": -90.16,
     "water": [], "tide": "8761993", "fresh": True},
    {"id": "pearl", "name": "Pearl River / Honey Island Swamp", "lat": 30.35, "lon": -89.7,
     "water": [], "tide": None, "fresh": True},
    {"id": "springbayou", "name": "Spring Bayou", "lat": 31.13, "lon": -92.03,
     "water": [], "tide": None, "fresh": True},
    {"id": "kincaid", "name": "Kincaid Lake", "lat": 31.27, "lon": -92.62,
     "water": [], "tide": None, "fresh": True},
    {"id": "vernon", "name": "Vernon Lake", "lat": 31.18, "lon": -93.33,
     "water": [], "tide": None, "fresh": True},
    {"id": "concordia", "name": "Lake Concordia", "lat": 31.6, "lon": -91.55,
     "water": [], "tide": None, "fresh": True},
    {"id": "bruin", "name": "Lake Bruin", "lat": 31.95, "lon": -91.2,
     "water": [], "tide": None, "fresh": True},
    # Freshwater: north
    {"id": "toledo", "name": "Toledo Bend", "lat": 31.4, "lon": -93.7,
     "water": [], "tide": None, "fresh": True},
    {"id": "blacklake", "name": "Black Lake / Clear Lake", "lat": 31.95, "lon": -93.0,
     "water": [], "tide": None, "fresh": True},
    {"id": "caney", "name": "Caney Creek Reservoir", "lat": 32.22, "lon": -92.5,
     "water": [], "tide": None, "fresh": True},
    {"id": "bistineau", "name": "Lake Bistineau", "lat": 32.4, "lon": -93.4,
     "water": [], "tide": None, "fresh": True},
    {"id": "crosslake", "name": "Cross Lake", "lat": 32.5, "lon": -93.83,
     "water": [], "tide": None, "fresh": True},
    {"id": "caddo", "name": "Caddo Lake", "lat": 32.7, "lon": -93.95,
     "water": [], "tide": None, "fresh": True},
    {"id": "claiborne", "name": "Lake Claiborne", "lat": 32.73, "lon": -92.92,
     "water": [], "tide": None, "fresh": True},
    {"id": "darbonne", "name": "Lake D'Arbonne", "lat": 32.72, "lon": -92.35,
     "water": [], "tide": None, "fresh": True},
    {"id": "ouachita", "name": "Ouachita River (Monroe)", "lat": 32.5, "lon": -92.12,
     "water": [], "tide": None, "fresh": True},
    {"id": "povertypoint", "name": "Poverty Point Reservoir", "lat": 32.53, "lon": -91.49,
     "water": [], "tide": None, "fresh": True},
    # Offshore grounds (approximate centers; check your chart)
    {"id": "off_mainpass", "name": "Main Pass rigs (east of the Delta)", "lat": 29.25, "lon": -88.75,
     "water": [], "tide": None, "marine": True},
    {"id": "off_westdelta", "name": "West Delta rigs", "lat": 28.95, "lon": -89.75,
     "water": [], "tide": None, "marine": True},
    {"id": "off_eugene", "name": "Eugene Island rigs", "lat": 28.6, "lon": -91.6,
     "water": [], "tide": None, "marine": True},
    {"id": "off_vermilion", "name": "Vermilion rigs", "lat": 28.8, "lon": -92.3,
     "water": [], "tide": None, "marine": True},
    {"id": "off_cameron", "name": "Cameron rigs (30 mi south)", "lat": 29.3, "lon": -93.3,
     "water": [], "tide": None, "marine": True},
    {"id": "ewing", "name": "Ewing Bank", "lat": 28.1, "lon": -91.03,
     "water": [], "tide": None, "marine": True,
     "note": "Offshore bank roughly 100 miles south of Morgan City. Position is approximate: check your chart."},
    {"id": "sonnier", "name": "Sonnier Bank", "lat": 28.33, "lon": -92.45,
     "water": [], "tide": None, "marine": True,
     "note": "Offshore bank roughly 100 miles south of Intracoastal City. Position is approximate: check your chart."},
    {"id": "off_mc_east", "name": "Mississippi Canyon deepwater (east)", "lat": 28.5, "lon": -88.3,
     "water": [], "tide": None, "marine": True},
]

# USGS gauges on the water itself at freshwater and swamp spots, for water level (rising, steady,
# falling) and, where one reports it, dissolved oxygen. Only gauges on the same lake or river are listed:
# a gauge on a different creek or lake miles away says little about the spot.
SPOT_GAUGES = {
    "verret": ["07381454"],                   # Belle River at Hwy 70 near Pierre Part (Lake Verret outlet)
    "palourde": ["07381600"],                 # Lower Atchafalaya River at Morgan City (level and oxygen)
    "maurepas": ["073802302"],                # Pass Manchac (the swamp oxygen sensor near Hope Canal reads swamp
                                              # water, routinely under 2 mg/L, not the open lake, so it isn't used)
    "henderson": ["302020091435700"],         # Lake Pelba at I-10 near Henderson
    "tchefuncte": ["07375230"],               # Tchefuncte River at Madisonville
    "pearl": ["02492600"],                    # Pearl River at Pearl River
    "springbayou": ["07383360"],              # Little River at Boggy Bayou Rd, east of Marksville
    "toledo": ["312914093422701"],            # Toledo Bend Reservoir near Negreet (lake elevation)
    "bistineau": ["07349250"],                # Lake Bistineau near Ringgold
    "crosslake": ["07344480"],                # Cross Lake at Shreveport
    "claiborne": ["07364840"],                # Lake Claiborne near Aycock
    "darbonne": ["07366364"],                 # Bayou D'Arbonne Lake at the dam
    "ouachita": ["07367005"],                 # Ouachita River at West Monroe
}
for _s in SPOTS:
    _s.setdefault("gauges", SPOT_GAUGES.get(_s["id"], []))

# ---------------------------------------------------------------- HTTP
# Hosts that time out when hit by several workers at once get a small concurrency limit.
_HOST_GATES = {"open-meteo.com": threading.Semaphore(2), "api.weather.gov": threading.Semaphore(3)}

# NOAA ERD's ERDDAP is sometimes busy (HTTP 503); upwell is its sister server with the same datasets.
ERDDAP_MIRRORS = ("https://coastwatch.pfeg.noaa.gov/erddap", "https://upwell.pfeg.noaa.gov/erddap")

def erddap_json(path, tries=1, timeout=12):
    """GET an ERDDAP path (e.g. 'griddap/jplMURSST41.json?...') from the first mirror that answers."""
    for base in ERDDAP_MIRRORS:
        j = get_json(f"{base}/{path}", tries=tries, timeout=timeout)
        if j is not None:
            return j
    return None

# A host that keeps failing is skipped for the rest of the run, so one slow or broken service can't eat the
# whole time budget (Open-Meteo, for one, sometimes stops answering for a while).
HOST_GIVE_UP = 6                      # failed calls in a row
_HOST_FAILS, _HOST_LOCK = {}, threading.Lock()
_NOT_FOUND = set()                    # URLs that answered 404: really missing, not just a blip

def host_down(url):
    with _HOST_LOCK:
        return _HOST_FAILS.get(urlparse(url).netloc, 0) >= HOST_GIVE_UP

def host_result(url, ok):
    host = urlparse(url).netloc
    with _HOST_LOCK:
        if ok:
            _HOST_FAILS[host] = 0
            return
        _HOST_FAILS[host] = _HOST_FAILS.get(host, 0) + 1
        if _HOST_FAILS[host] == HOST_GIVE_UP:
            log.warning("%s failed %d times in a row; skipping it for the rest of this run", host, HOST_GIVE_UP)

def get_json(url, params=None, tries=3, timeout=15, headers=None):
    if params:
        url += "?" + urlencode(params)
    if host_down(url):
        return None
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
                host_result(url, True)
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
                host_result(url, True)                    # the server answered; the request was the problem
                return None
        except (URLError, TimeoutError, OSError, ValueError) as e:      # ValueError: not JSON / bad bytes
            log.warning("fetch failed (%s): %s", str(e)[:120], url)
        if i + 1 < tries:
            time.sleep(2 ** i)
    host_result(url, False)
    return None

def get_bytes(url, timeout=60):
    """Raw bytes of a download (zip files), or None."""
    try:
        with urlopen(Request(url, headers={"User-Agent": f"{APP} ({CONTACT})"}), timeout=timeout) as r:
            return r.read()
    except HTTPError as e:
        log.warning("fetch failed (HTTP %s): %s", e.code, url)
    except (URLError, TimeoutError, OSError) as e:
        log.warning("fetch failed (%s): %s", e, url)
    return None

def get_text(url, params=None, quiet_404=False):
    if params:
        url += "?" + urlencode(params)
    if host_down(url):
        return None
    try:
        with urlopen(Request(url, headers={"User-Agent": f"{APP} ({CONTACT})"}), timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
        host_result(url, True)
        return txt
    except HTTPError as e:
        host_result(url, e.code < 500)
        if e.code == 404:
            _NOT_FOUND.add(url)
        if not (quiet_404 and e.code == 404):
            log.warning("fetch failed (HTTP %s): %s", e.code, url)
    except (URLError, TimeoutError, OSError) as e:
        host_result(url, False)
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
USGS_CODES = "00010,00095,63680,00065,62614,62615,00300"   # water temp C, conductance uS/cm@25C, turbidity FNU,
                                                           # gage ft, lake elevation ft (NGVD, NAVD), dissolved oxygen mg/L

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
    if txt is None:
        return []                                 # both USGS services failed: try again next run, don't remember this
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
    elif code in ("00065", "62614", "62615"):    # gage height or lake elevation, ft: only the 6 h change is used
        ref = nearest(pts, t - timedelta(hours=6), timedelta(hours=1))
        if ref is not None:
            out["level_d6"] = (x - ref, t, None)
    elif code == "00300":                        # dissolved oxygen, mg/L
        out["do_mgl"] = (x, t, None)

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
        hourly="surface_pressure,temperature_2m,precipitation,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover",
        past_hours=240, forecast_hours=169,
        wind_speed_unit="mph", temperature_unit="fahrenheit", precipitation_unit="inch",
        timezone="GMT"))
    try:
        cur = j["current"]
        t = datetime.fromisoformat(cur["time"]).replace(tzinfo=UTC)
        H = j["hourly"]
        times = [datetime.fromisoformat(a).replace(tzinfo=UTC) for a in H["time"]]
        # Ten days back is only for the lake-temperature estimate; everything else uses the last 48 h
        air_hist = [(ts, v) for ts, v in zip(times, H["temperature_2m"]) if v is not None and ts <= t]
        i0 = next((i for i, ts in enumerate(times) if ts >= t - timedelta(hours=48)), 0)
        times = times[i0:]
        H = {k: (v[i0:] if isinstance(v, list) else v) for k, v in H.items()}
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
                "pres": trend, "pres_delta_hpa": delta, "air_d24": air_d24, "rain48": rain48, "air_hist": air_hist,
                "series": {"t": times, "pres": H["surface_pressure"], "air": H["temperature_2m"], "rain": H["precipitation"],
                           "wind": H["wind_speed_10m"], "cloud": H["cloud_cover"],
                           "wdir": H.get("wind_direction_10m") or [None] * len(times),
                           "gust": H.get("wind_gusts_10m") or [None] * len(times)}}
    except (TypeError, KeyError, ValueError):
        return None

# ---------------------------------------------------------------- NWS gridded wind and wave forecast
# The National Weather Service's gridded forecast (the numbers behind its point and marine forecasts).
# Unlike the Open-Meteo marine model, which snaps a bay to the nearest Gulf grid cell, it forecasts
# waves for protected waters: bays, sounds and Lake Pontchartrain. Inland lakes get wind only, and
# points beyond the coastal waters (about 60 nautical miles out) have no grid cell at all.
NWS_API = "https://api.weather.gov"
_ISO_DUR = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")
_SPEED_TO_MPH = {"km_h-1": 0.621371, "m_s-1": 2.23694, "kt": 1.15078, "mi_h-1": 1.0}
_LEN_TO_FT = {"m": 3.28084, "ft": 1.0, "cm": 0.0328084}

def iso_span(s):
    """'2026-11-19T09:00:00+00:00/PT3H' -> (start as UTC datetime, whole hours). None if unreadable."""
    try:
        a, _, d = (s or "").partition("/")
        t = datetime.fromisoformat(a.replace("Z", "+00:00")).astimezone(UTC)
        m = _ISO_DUR.match(d)
        if not m or not any(m.groups()):
            return None
        hrs = int(m.group(1) or 0) * 24 + int(m.group(2) or 0) + (1 if int(m.group(3) or 0) >= 30 else 0)
        return t, max(1, hrs)
    except (AttributeError, TypeError, ValueError):
        return None

def grid_hourly(prop, start, factors=None, hours=168, nd=0):
    """Spread one NWS grid property (a list of time spans with values) over hourly slots from `start`.
    `factors` maps the property's unit to a multiplier; an unknown unit gives no values."""
    out = [None] * hours
    if not isinstance(prop, dict):
        return out
    k = 1.0
    if factors is not None:
        k = factors.get(str(prop.get("uom") or "").split(":")[-1])
        if k is None:
            return out
    for v in prop.get("values") or []:
        sp, x = iso_span(v.get("validTime")), v.get("value")
        if sp is None or not isinstance(x, (int, float)):
            continue
        t, n = sp
        i0 = int(round((t - start).total_seconds() / 3600))
        val = round(x * k, nd) if nd else round(x * k)
        for i in range(max(0, i0), min(hours, i0 + n)):
            out[i] = val
    return out

def nws_grid_cell(spot, cache):
    """NWS forecast office and grid cell for a spot, looked up once and cached. None if there is none."""
    key = ("ngrid", spot["id"])
    if key in cache:
        v = cache[key]
        return tuple(v) if v else None
    j = get_json(f"{NWS_API}/points/{spot['lat']:.4f},{spot['lon']:.4f}", tries=2,
                 headers={"Accept": "application/geo+json"})
    if j is None:
        return None                                   # service down: try again next run
    p = j.get("properties") or {}
    cell = (p.get("gridId"), p.get("gridX"), p.get("gridY"))
    if not cell[0] or cell[1] is None or cell[2] is None:
        cache[key] = []                               # beyond the NWS grid (far offshore): don't ask again
        return None
    cache[key] = list(cell)
    return cell

def nws_grid(spot, cache, start, hours=168):
    """Hourly NWS wind (mph), gusts (mph), direction (degrees from), waves (ft) and wave period (s)
    for a spot from `start`. Spots in the same grid cell share one request. None if unavailable."""
    cell = nws_grid_cell(spot, cache)
    if not cell:
        return None
    gkey = ("ngridfc",) + tuple(cell)
    if gkey not in cache:
        cache[gkey] = get_json(f"{NWS_API}/gridpoints/{cell[0]}/{cell[1]},{cell[2]}", tries=2, timeout=25,
                               headers={"Accept": "application/geo+json"})
    g = cache[gkey]
    P = g.get("properties") if isinstance(g, dict) else None
    if not P:
        return None
    out = {"src": "nws", "office": cell[0],
           "wind": grid_hourly(P.get("windSpeed"), start, _SPEED_TO_MPH, hours),
           "gust": grid_hourly(P.get("windGust"), start, _SPEED_TO_MPH, hours),
           "dir": grid_hourly(P.get("windDirection"), start, None, hours),
           "wave": grid_hourly(P.get("waveHeight"), start, _LEN_TO_FT, hours, nd=1),
           "per": grid_hourly(P.get("wavePeriod"), start, None, hours)}
    if all(v is None for v in out["wind"]):
        return None
    for k in ("wave", "per", "gust"):                 # inland lakes have no waves: leave the arrays out
        if all(v is None for v in out[k]):
            del out[k]
    return out

# ---------------------------------------------------------------- cold fronts
NORTHERLY = lambda d: d is not None and (d >= 290 or d <= 60)      # NW through NE
SOUTHERLY = lambda d: d is not None and 100 <= d <= 260            # ESE through WSW

def _mean_dir(ds):
    """Average wind direction (degrees) of a list, None if too few readings."""
    got = [d for d in ds if d is not None]
    if len(got) < max(2, len(ds) // 2):
        return None
    ds = got
    x = sum(math.cos(math.radians(d)) for d in ds); y = sum(math.sin(math.radians(d)) for d in ds)
    return None if abs(x) + abs(y) < 1e-9 else math.degrees(math.atan2(y, x)) % 360

def find_fronts(series, now, min_drop=6.0, min_rise=2.0):
    """Cold front passages in an hourly weather series (the last 48 h and the next 7 days).
    A front is where a southerly wind swings to the north, pressure climbs and the 24-hour
    average air temperature drops. Daily averages are compared so the normal day-night swing
    doesn't count. Returns [{t, drop_f, rise_hpa, nwind}] with t the hour the wind shifts."""
    t, pres, air, wdir, wind = (series.get(k) for k in ("t", "pres", "air", "wdir", "wind"))
    if not t or not pres or not air or not wdir:
        return []
    n = len(t)
    def avg(vals, a, b):
        v = [x for x in vals[max(0, a):min(n, b)] if x is not None]
        return sum(v) / len(v) if len(v) >= (b - a) * 0.75 else None
    out, i = [], 6
    while i < n - 6:
        before, after = _mean_dir(wdir[i - 6:i]), _mean_dir(wdir[i + 1:i + 7])
        if not (NORTHERLY(wdir[i]) and SOUTHERLY(before) and NORTHERLY(after)):
            i += 1
            continue
        warm, cold = avg(air, i - 30, i - 6), avg(air, i + 6, i + 30)
        p0 = [x for x in pres[max(0, i - 3):i + 1] if x is not None]
        p1 = [x for x in pres[i + 3:min(n, i + 13)] if x is not None]
        drop = None if warm is None or cold is None else warm - cold
        rise = max(p1) - min(p0) if p0 and p1 else None
        if drop is not None and rise is not None and drop >= min_drop and rise >= min_rise:
            nw = [w for w, d in zip(wind[i:i + 30], wdir[i:i + 30]) if w is not None and NORTHERLY(d)]
            out.append({"t": t[i].isoformat(), "drop_f": round(drop), "rise_hpa": round(rise, 1),
                        "nwind": round(max(nw)) if nw else None})
            i += 36                                   # one front at a time
        else:
            i += 1
    lo, hi = now - timedelta(hours=72), now + timedelta(days=8)
    return [f for f in out if lo <= parse_iso(f["t"]) <= hi]

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

# Satellite water color (chlorophyll), tried in order. In late 2026 NOAA moved these datasets from
# coastwatch.pfeg.noaa.gov to coastwatch.noaa.gov under new names; the old addresses redirect there.
# PolarWatch serves the same datasets. coastwatch.pfeg.noaa.gov also answers GitHub's servers with
# "403 Forbidden", so it is the last resort.
CHL_SETS = (("https://coastwatch.noaa.gov/erddap", "noaacwN20VIIRSchlaDaily"),
            ("https://polarwatch.noaa.gov/erddap", "noaacwN20VIIRSchlaDaily"),
            ("https://coastwatch.pfeg.noaa.gov/erddap", "nesdisVHNnoaa20chlaDaily"))

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
    out, j = None, None
    # [last] = newest composite available — avoids 404s for missing calendar days
    for base, ds in CHL_SETS:
        j = get_json(f"{base}/griddap/{ds}.json?chlor_a[(last)][(0.0)][({key[1]})][({key[2]})]", tries=1)
        if j is not None:
            break
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

# Satellite sea temperature, tried in order: NOAA's Geo-polar Blended analysis (daily, ~3 mi cells) on the
# CoastWatch server GitHub can reach, then MUR on the West Coast node (which refuses GitHub's runners and has
# dropped the jplMURSST41 dataset) as a last resort. (server, dataset, stride, label)
SST_SETS = [("https://coastwatch.noaa.gov/erddap", "noaacwBLENDEDsstDNDaily", 1, "NOAA Geo-polar Blended satellite SST (CoastWatch)"),
            (None, "jplMURSST41", 3, "MUR satellite SST (NASA/JPL via CoastWatch)")]

def sat_sst(lat, lon, cache):
    """Satellite sea temperature around a spot: temp at the spot, strongest temperature break (deg F per
    mile between neighboring cells) and the warm-to-cool range in the box."""
    key = ("sst", round(lat * 4) / 4, round(lon * 4) / 4)
    if key in cache:
        return cache[key]
    d, out, j, label = 0.25, None, None, None
    for base, ds, k, name in SST_SETS:
        q = f"griddap/{ds}.json?analysed_sst[(last)][({lat-d:.2f}):{k}:({lat+d:.2f})][({lon-d:.2f}):{k}:({lon+d:.2f})]"
        j = get_json(f"{base}/{q}", tries=1, timeout=20) if base else erddap_json(q)
        if j is not None:
            label = name
            break
    if j is None:                              # no server answered; get_json already logged it
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
                   "time": rows[0][0], "src": label}
    except (TypeError, KeyError, IndexError, ValueError) as e:
        log.warning("satellite SST problem: %s", e)
    cache[key] = out
    return out

def sat_ssh_edge(lat, lon, cache):
    """Loop Current and eddy edges from satellite altimetry (NOAA CoastWatch blended sea level anomaly,
    daily, 0.25 deg): the steepest change in sea height between neighboring cells within ~50 mi, in cm per
    mile. Yellowfin, marlin, wahoo and mahi concentrate along these current edges."""
    key = ("ssh", round(lat * 2) / 2, round(lon * 2) / 2)
    if key in cache:
        return cache[key]
    d, out = 0.75, None
    j = get_json("https://coastwatch.noaa.gov/erddap/griddap/noaacwBLENDEDsshDaily.json"
                 f"?sla[(last)][({lat-d:.2f}):1:({lat+d:.2f})][({lon-d:.2f}):1:({lon+d:.2f})]", tries=1, timeout=20)
    try:
        rows = (j or {}).get("table", {}).get("rows") or []
        grid = {(round(r[1], 3), round(r[2], 3)): r[3] for r in rows if r[3] is not None}
        lats, lons = sorted({k[0] for k in grid}), sorted({k[1] for k in grid})
        best = None
        for (la, lo), v in grid.items():
            i, k = lats.index(la), lons.index(lo)
            for la2, lo2 in ((lats[i + 1], lo) if i + 1 < len(lats) else (None, None),
                             (la, lons[k + 1]) if k + 1 < len(lons) else (None, None)):
                v2 = grid.get((la2, lo2)) if la2 is not None else None
                if v2 is None:
                    continue
                dist = miles(la, lo, la2, lo2)
                if dist > 0:
                    g = abs(v2 - v) * 100 / dist        # cm per mile
                    best = g if best is None or g > best else best
        if best is not None and len(grid) >= 9:
            out = {"edge": round(best, 2), "time": rows[0][0]}
    except (TypeError, KeyError, IndexError, ValueError) as e:
        log.warning("sea height problem: %s", e)
    cache[key] = out
    return out

# ---------------------------------------------------------------- water visibility
# Satellite water clarity: Kd490 = how fast blue-green light fades with depth (per metre).
# Tried in order. The gap-filled products fill cloudy days but run about a week or more behind.
# (server, dataset). The new NOAA CoastWatch server and PolarWatch first (see CHL_SETS for why).
KD_SETS = [("https://coastwatch.noaa.gov/erddap", "noaacwNPPVIIRSkd490Daily"),           # VIIRS near real-time
           ("https://polarwatch.noaa.gov/erddap", "noaacwNPPN20VIIRSkd490Daily"),        # VIIRS near real-time
           ("https://polarwatch.noaa.gov/erddap", "noaacwNPPN20S3AkdSCIDINEOF2kmDaily"), # VIIRS + OLCI gap-filled, 2 km
           ("https://coastwatch.pfeg.noaa.gov/erddap", "nesdisVHNkd490Daily"),           # old addresses, last resort
           ("https://coastwatch.pfeg.noaa.gov/erddap", "noaacwNPPN20S3AkdSCIDINEOF2kmDaily")]
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
    for base, ds in KD_SETS:
        if _KD_FAILS.get((base, ds), 0) >= 3:
            continue
        url = (f"{base}/griddap/{ds}.json"
               f"?kd_490[(last)][(0.0)][({lat+d:.3f}):({lat-d:.3f})][({lon-d:.3f}):({lon+d:.3f})]")
        j = get_json(url, tries=1)
        try:
            rows = j["table"]["rows"]
            vals = sorted(float(r[-1]) for r in rows if isinstance(r[-1], (int, float)) and 0.01 < r[-1] < 20)
            t = parse_iso(rows[0][0]) if rows else None
        except (TypeError, KeyError, IndexError, ValueError):
            with _KD_LOCK:
                _KD_FAILS[(base, ds)] = _KD_FAILS.get((base, ds), 0) + 1
                if _KD_FAILS[(base, ds)] == 3:
                    log.warning("satellite clarity %s on %s skipped for this run after repeated failures", ds, urlparse(base).netloc)
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

# How far away a gauge or buoy can be and still count as "at" a spot. Salinity changes fast across
# the marsh, so a gauge 30 miles off says little about a spot; offshore, a buoy past 30 miles is often
# in different weather (the Galveston buoy is no guide to Cameron), and the marine forecast at the spot
# itself is the better fallback. A missing reading lowers the score's data rating instead of misleading it.
SAL_RADIUS_MI = 20
TEMP_RADIUS_MI = 30
BUOY_RADIUS_MI = 30

def near_buoys(spot, cache, radius_mi=BUOY_RADIUS_MI, limit=6):
    """Nearest active NDBC stations with weather sensors (buoys and platforms)."""
    with _BUOY_LOCK:                          # one download of the station list per run
        if not cache.get("buoys"):            # never keep an empty list: that was a failed download
            cache["buoys"] = buoy_list()
        if cache.get("deadbuoys") and not cache.get("deadbuoys_t"):
            cache["deadbuoys_t"] = datetime.now(UTC).isoformat()     # older saved lists had no date
        try:                                  # stations marked dead get another chance after a week
            if (datetime.now(UTC) - parse_iso(cache.get("deadbuoys_t"))).days >= 7:
                cache["deadbuoys"], cache["deadbuoys_t"] = [], None
        except (TypeError, ValueError):
            pass
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
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{sid}.txt"
    txt = get_text(url, quiet_404=True)
    if txt is None:
        # None = the station has no realtime file (404) and can be skipped for a while;
        # {} = it didn't answer this time, which says nothing about tomorrow
        return None if url in _NOT_FOUND else {}
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
    # Slack only within half an hour of a turn. A weak (neap) tide still moves water, and its
    # small range already shows up in the tide swing, so it isn't called slack for hours.
    if min(t - pv[0], nx[0] - t) < timedelta(minutes=30):
        return "Slack", rng
    return ("Incoming" if nx[2] == "H" else "Outgoing"), rng

def build_forecast(cond, wx, pts, now, mar=None, bias=None, cev=None, sea=None):
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
    o = {k: [] for k in ("temp", "wind", "gust", "wdir", "cloud", "pres", "tide", "trange", "front", "dtemp", "rain", "clar")}
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
        gu, wd = at("gust", t), at("wdir", t)
        o["gust"].append(None if gu is None else round(gu))
        o["wdir"].append(None if wd is None else round(wd))
        o["cloud"].append(None if cloud is None else round(cloud))
        st, rg = stage_at(pts, t) if pts else (None, None)
        o["tide"].append(st)
        o["trange"].append(None if rg is None else round(rg, 1))
        o["clar"].append(clarity_label(None, wind, cond["sal"], rain)[0])
        if cev:                                   # predicted tidal current (knots, either direction)
            cv = current_at(cev, t)
            o.setdefault("tcur", []).append(None if cv is None else round(abs(cv), 1))
    o["start"] = start.isoformat()
    if sea:                                       # NWS gridded wind and waves, same hourly slots
        o["sea"] = sea
    return o

def surf_waves(spot, fc, sea):
    """Surf spots: the NWS wave forecast in the same hourly slots as the rest of the forecast."""
    if fc and spot.get("surf") and sea and sea.get("wave") and "wave" not in fc:
        fc["wave"] = list(sea["wave"][:len(fc["temp"])])
    return fc

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
        for m in ("temp_f", "temp_d24", "cond_ms", "turb_fnu", "wind_mph", "level_d6", "do_mgl"):
            take(m, cache[key], f"{kind}:{sid}")
    # Gauges on the water at freshwater spots: only level and oxygen (their temperature and conductivity
    # can come from a river channel rather than the lake, so the spot's other sources stay in charge)
    for sid in spot.get("gauges", []):
        if ("usgs", sid) not in cache:
            cache[("usgs", sid)] = usgs_source(sid)
        for m in ("level_d6", "do_mgl"):
            take(m, cache[("usgs", sid)], f"usgs:{sid}")

    # Freshwater lakes and rivers ("fresh") skip this: the nearest conductivity gauge can be a coastal
    # one many miles away, which would report a salty lake.
    if not spot.get("marine") and not spot.get("fresh") and "cond_ms" not in got:  # open Gulf has no gauges
        for sid, dist in near_sites(spot, cache, "00095"):
            if dist > SAL_RADIUS_MI:              # the station list is cached; it can hold farther ones
                continue
            if ("usgs", sid) not in cache:
                cache[("usgs", sid)] = usgs_source(sid)
            take("cond_ms", cache[("usgs", sid)], f"usgs:{sid}", {"distance_mi": dist})
            if "cond_ms" in got:
                break

    if not spot.get("marine") and "temp_f" not in got:
        for sid, dist in near_sites(spot, cache, "00010"):
            if dist > TEMP_RADIUS_MI:
                continue
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
    est = []
    # No water-temperature gauge within reach (most north Louisiana lakes): estimate it from the air.
    # A lake's surface follows the air slowly, over days, so use an average of the last ten days that
    # counts recent days most (a 4-day time constant). The 24 h change is how much that average moved.
    if not spot.get("marine") and "temp_f" not in got and wx and (wx.get("air_hist") or wx.get("series")):
        air = wx.get("air_hist") or [(t, v) for t, v in zip(wx["series"]["t"], wx["series"]["air"]) if v is not None and t <= now]
        air = [(t, v) for t, v in air if t <= now]
        def lake(at):
            pts = [(max(0.0, (at - t).total_seconds() / 3600), v) for t, v in air if t <= at]
            if len(pts) < 36:
                return None
            wts = [math.exp(-age / 96) for age, _ in pts]
            return sum(w * v for w, (_, v) in zip(wts, pts)) / sum(wts)
        est_now = lake(now)
        if est_now is not None:
            got["temp_f"] = (est_now, now)
            src["temp_f"] = {"source": "estimated from air temperature (no gauge nearby)", "age_min": 0}
            est.append("temp")
            est_prev = lake(now - timedelta(hours=24))
            if est_prev is not None:
                got["temp_d24"] = (est_now - est_prev, now)
                src["temp_d24"] = dict(src["temp_f"])
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
            ssrc = {"source": ss.get("src") or "satellite SST", "age_min": round(max(0, (now - sdt).total_seconds() / 60))}
            got["sbreak"], src["sbreak"] = (ss["break"], sdt, None), ssrc
            got["ssat"], src["ssat"] = (ss["sst_f"], sdt, None), ssrc
        sh = sat_ssh_edge(spot["lat"], spot["lon"], cache)
        if sh:
            try:
                hdt = parse_iso(sh["time"])
            except (TypeError, ValueError):
                hdt = now
            got["ssh_edge"], src["ssh_edge"] = (sh["edge"], hdt, None), {
                "source": "NOAA blended altimetry (CoastWatch)", "age_min": round(max(0, (now - hdt).total_seconds() / 60))}
        if ss:
            # A recent satellite reading beats the ocean model for the spot's own temperature (a buoy, which
            # measures the water, still wins). The daily analysis is dated noon UTC the day before it's posted.
            if (now - sdt) <= timedelta(hours=60) and str(src.get("temp_f", {}).get("source", "")).startswith("open-meteo-marine"):
                got["temp_f"], src["temp_f"] = (ss["sst_f"], sdt, None), dict(ssrc)

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
                            cache.setdefault("deadbuoys_t", None)
                            if not cache["deadbuoys_t"]:
                                cache["deadbuoys_t"] = datetime.now(UTC).isoformat()
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
    # highs and lows for the page: the whole 7-day forecast, so the hour-by-hour view can draw the tide any day
    tides = [{"t": t.isoformat(), "v": round(v, 2), "type": k} for t, v, k in pts
             if now - timedelta(hours=7) <= t <= now + timedelta(hours=180)] or None
    if tide:
        src["tide"] = {"source": f"coops-predictions:{spot['tide']}", "age_min": 0}

    sal = None
    if "cond_ms" in got:
        c, _, tref = got["cond_ms"]
        tc = tref if tref is not None else ((got["temp_f"][0] - 32) * 5 / 9 if "temp_f" in got else None)
        if tc is not None:
            sal = round(psu(c, tc), 1)
            src["sal"] = src.pop("cond_ms")
    if sal is None and spot.get("fresh"):
        sal = 0.1
        src["sal"] = {"source": "freshwater lake or river (assumed)", "age_min": 0}
        est.append("sal")
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

    tcur = tidal_current(spot, now, cache)
    if tcur:
        src["tcur"] = {"source": f"coops-currents:{tcur['station']}", "age_min": 0, "distance_mi": tcur["mi"]}
    oxy = hypoxia_flag(spot, cache.get("deadzone"), now)
    weed = weed_near(spot, cache.get("sargassum"), now)
    if weed is not None:
        sg = cache["sargassum"]
        src["weed"] = {"source": sg["source"], "age_min": sg["age_days"] * 1440}
    if oxy:
        src["oxy"] = {"source": "NOAA Hypoxia Watch", "age_min": 0}

    try:                                          # wind and waves from the NWS grid; optional
        sea = nws_grid(spot, cache, now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    except Exception:
        log.exception("NWS grid for %s failed", spot["id"])
        sea = None
    if spot.get("surf") and "wave" not in got and sea and sea.get("wave") and sea["wave"][0] is not None:
        got["wave"], src["wave"] = (sea["wave"][0], now, None), {"source": "NWS forecast grid (surf zone)", "age_min": 0}
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
            "doxy": round(got["do_mgl"][0], 1) if "do_mgl" in got else None,
            "trange": round(trange, 1) if trange is not None else None,
            "wave": round(got["wave"][0], 1) if "wave" in got else None,
            "cur": round(got["cur"][0], 1) if "cur" in got else None,
            "wl_anom": wl[0] if wl else None,
            "riv": riv,
            "sbreak": round(got["sbreak"][0], 2) if "sbreak" in got else None,
            "ssat": round(got["ssat"][0]) if "ssat" in got else None,
            "ssh_edge": got["ssh_edge"][0] if "ssh_edge" in got else None,
            "tcur": round(abs(tcur["now"]), 1) if tcur else None,
            "tdir": tcur["dir"] if tcur else None,
            "oxy": oxy,
            "weed": weed}
    na = []
    if not spot["tide"]:
        na += ["tide", "trange"]
    if not any(k == "usgs" for k, _ in spot["water"]) and not spot.get("gauges"):
        na.append("level")
    if "do_mgl" not in got:
        na.append("doxy")
    if spot.get("marine"):
        na += ["tide", "trange", "level", "sal"]  # clar can come from satellite now
    else:
        na += (["cur", "sbreak", "ssat", "ssh_edge"] if spot.get("surf") else ["wave", "cur", "sbreak", "ssat", "ssh_edge"])
    # Optional enrichment fields — don't tank completeness when missing
    na += ["chl", "wcolor", "wdir", "kd490"]
    if not spot["tide"] or spot.get("marine"):
        na.append("wl_anom")
    if not spot.get("river"):
        na.append("riv")
    if not tcur:
        na += ["tcur", "tdir"]
    if not oxy:
        na.append("oxy")
    if weed is None:
        na.append("weed")
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
            "current": {k: v for k, v in tcur.items() if k != "_ev"} if tcur else None,
            "fronts": find_fronts(wx["series"], now) if wx and wx.get("series") else [],
            "forecast": surf_waves(spot, build_forecast(cond, wx, pts, now, mar, sbias, tcur["_ev"] if tcur else None,
                                       sea=sea) if wx and wx.get("series") else None, sea),
            "conditions": cond,
            "quality": {"missing": missing, "sources": src,
                        "estimated": (["clar"] if clar_kind in ("estimated", "satellite") else []) + est,
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
    # temperature breaks from NOAA's Geo-polar Blended satellite SST (deg C -> F), about 3.5 mi cells, on the
    # CoastWatch server GitHub can reach (the MUR dataset this used is gone, and its server refuses GitHub)
    g = griddap_box("https://coastwatch.noaa.gov/erddap", "noaacwBLENDEDsstDNDaily", "analysed_sst",
                    lat0, lat1, lon0, lon1, stride=1, extra="")
    if g:
        t, la, lo, gr = g
        gr = [[None if v is None else v * 1.8 + 32 for v in row] for row in gr]
        out["sst_breaks"] = {"time": t, "lines": breaks(gr, la, lo, 1.0, 0.08)}
    # color lines: where chlorophyll crosses 0.3 and 1 mg/m3 (blue to green-blue to green water)
    g = griddap_box("https://coastwatch.noaa.gov/erddap", "noaacwN20VIIRSchlaDaily", "chlor_a",
                    lat0, lat1, lon0, lon1, stride=1)
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

# ---------------------------------------------------------------- sargassum (weedlines)
# USF Alternative Floating Algae Index, 7-day composite from MODIS, served by NOAA AOML CoastWatch.
# A pixel counts as floating weed when it stands out from the water around it (pixel minus the
# local median above 1.79e-4, the threshold used in the USF/AOML Sargassum work). Pixels near
# land or cloud edges are skipped: muddy river water and cloud edges fool the index there.
AFAI_SOURCES = (("https://cwcgom.aoml.noaa.gov/erddap", "noaa_aoml_atlantic_oceanwatch_AFAI_7D"),
                ("https://upwell.pfeg.noaa.gov/erddap", "noaa_aoml_ae10_d11a_85ab"))
AFAI_MIN = 1.79e-4
SARG_MAX_AGE_D = 12          # older than this, the weed has drifted too far to score with

def _median(v):
    v = sorted(v)
    n = len(v)
    return None if not n else v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

def sargassum_patches(lats, lons, grid, block=12, edge=10, cell=0.1):
    """[[lat, lon, pixels]] for 0.1-degree cells with floating weed in an AFAI grid."""
    ni, nj = len(lats), len(lons)
    if not ni or not nj:
        return []
    # distance (in cells) to the nearest gap: land, cloud or no data
    INF = 10 ** 6
    dist = [[INF] * nj for _ in range(ni)]
    q = []
    for i in range(ni):
        for j in range(nj):
            if grid[i][j] is None:
                dist[i][j] = 0; q.append((i, j))
    k = 0
    while k < len(q):
        i, j = q[k]; k += 1
        for a, b in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
            if 0 <= a < ni and 0 <= b < nj and dist[a][b] > dist[i][j] + 1:
                dist[a][b] = dist[i][j] + 1; q.append((a, b))
    med = {}
    for bi in range(0, ni, block):
        for bj in range(0, nj, block):
            med[(bi // block, bj // block)] = _median([grid[i][j] for i in range(bi, min(ni, bi + block))
                                                      for j in range(bj, min(nj, bj + block)) if grid[i][j] is not None])
    cells = {}
    for i in range(ni):
        for j in range(nj):
            v = grid[i][j]
            if v is None or dist[i][j] <= edge:
                continue
            m = med.get((i // block, j // block))
            if m is not None and v - m > AFAI_MIN:
                key = (round(math.floor(lats[i] / cell) * cell + cell / 2, 3), round(math.floor(lons[j] / cell) * cell + cell / 2, 3))
                cells[key] = cells.get(key, 0) + 1
    out = [[la, lo, n] for (la, lo), n in cells.items() if n >= 2]
    out.sort(key=lambda r: -r[2])
    return out[:400]

def sargassum(now):
    """{'time', 'patches': [[lat, lon, pixels]], 'source'} from the newest 7-day AFAI image, or None."""
    lat0, lat1, lon0, lon1 = BOX
    for base, ds in AFAI_SOURCES:
        g = None
        for desc in (False, True):
            try:
                g = griddap_box(base, ds, "AFAI", lat0, lat1, lon0, lon1, stride=2, lat_desc=desc, extra="")
            except Exception:
                log.warning("AFAI request failed at %s", base)
                g = None
            if g and g[1]:
                break
        if not g or not g[1]:
            continue
        t, la, lo, gr = g
        try:
            age = (now - parse_iso(t)).days
        except (TypeError, ValueError):
            continue
        if age > 30:
            log.info("AFAI at %s is %d days old; skipping", base, age)
            continue
        return {"time": t, "age_days": age, "patches": sargassum_patches(la, lo, gr),
                "source": "USF floating algae index (NOAA AOML CoastWatch)"}
    return None

def weed_near(spot, sg, now, radius_mi=100):
    """Miles from an offshore spot to the nearest weed patch in a recent image (capped at radius_mi),
    None when there is no usable image."""
    if not spot.get("marine") or not sg or sg.get("age_days") is None or sg["age_days"] > SARG_MAX_AGE_D:
        return None
    d = [miles(spot["lat"], spot["lon"], la, lo) for la, lo, _ in sg.get("patches") or []]
    return round(min([radius_mi] + d))

# ---------------------------------------------------------------- rigs and reefs (weekly)
RIGS, REEFS, LAUNCHES = "rigs.json", "reefs.json", "launches.json"
STATIC_DAYS = 7
# Tried in order until one answers
# BOEM's own download files come first: its map service (gis.boem.gov) only returns a
# "Service Unavailable" page now, and the two mirrors below no longer answer at all.
BOEM_PLATFORM_FILES = ("https://www.data.boem.gov/Platform/Files/platlocfixed.zip",
                       "https://www.data.boem.gov/Platform/Files/platstrufixed.zip")
RIG_SOURCES = (   # ArcGIS fallbacks, tried in order if the download files fail
    "https://gis.boem.gov/arcgis/rest/services/BOEM_BSEE/GOM_Layers/MapServer/0",
    "https://services2.coastalresilience.org/arcgis/rest/services/Gulf_of_Mexico/Conservation_Blueways/MapServer/18",
    "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/12",
)

def _unzip_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if not n.endswith("/"))
        return z.read(name).decode("latin-1")

def boem_platforms(bbox):
    """Platforms still standing, from BOEM's fixed-width Platform Locations and Platform
    Structures files (column positions from BOEM's field definition pages). Returns
    [[lat, lon, name], ...] inside bbox, or None if the files couldn't be read."""
    try:
        raw = [get_bytes(u) for u in BOEM_PLATFORM_FILES]
        if not all(raw):
            return None
        loc_txt, stru_txt = (_unzip_text(b) for b in raw)
    except (zipfile.BadZipFile, StopIteration, OSError) as e:
        log.warning("BOEM platform files unreadable: %s", e)
        return None
    loc = {}
    for l in loc_txt.splitlines():   # complex id 4-11, structure no. 12-14, area 15-16, block 17-22, name 23-37, lon 86-99, lat 100-112
        lon, lat = fnum(l[85:99].strip()), fnum(l[99:112].strip())
        if lon is None or lat is None:
            continue
        loc[(l[3:11].strip(), l[11:14].strip())] = (lat, lon, f"{l[14:16].strip()} {l[16:22].strip()}".strip(), l[22:37].strip())
    pts = []
    for l in stru_txt.splitlines():  # complex id 9-16, removal date 44-54, structure no. 82-84
        if len(l) < 84 or l[43:54].strip():
            continue                                               # removed structures
        hit = loc.pop((l[8:16].strip(), l[81:84].strip()), None)
        if not hit:
            continue
        lat, lon, block, name = hit
        if bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]:
            pts.append([round(lat, 4), round(lon, 4), f"{block} {name}".strip()])
    return pts or None
REEF_SOURCES = (   # (url, label, filter to the Louisiana box?)
    ("https://gis.ngdc.noaa.gov/arcgis/rest/services/NMSP/FGB/MapServer/23", "LDWF artificial reefs (via NOAA)", False),
    ("https://coast.noaa.gov/arcgis/rest/services/Hosted/ArtificialReefs/FeatureServer/0", "Artificial reefs (NOAA Office for Coastal Management)", True),
)

def _esri_to_geojson(f):
    """One Esri JSON feature as a GeoJSON feature (points, polygons, polylines)."""
    g, geom = f.get("geometry") or {}, None
    if "x" in g and "y" in g:
        geom = {"type": "Point", "coordinates": [g["x"], g["y"]]}
    elif g.get("rings"):
        geom = {"type": "MultiPolygon", "coordinates": [[r] for r in g["rings"]]}
    elif g.get("paths"):
        geom = {"type": "MultiLineString", "coordinates": g["paths"]}
    return {"type": "Feature", "properties": f.get("attributes") or {}, "geometry": geom}

def arcgis_geojson(url, where="1=1", bbox=None, max_pages=10, page=2000):
    """All features from an ArcGIS layer query as GeoJSON features. Tries GeoJSON with
    paging first, then Esri JSON (older servers), then a single unpaged request.
    Returns None when no form of the query answered, [] when it answered with nothing."""
    base = dict(where=where, outFields="*", returnGeometry="true", outSR="4326")
    if bbox:
        base.update(geometry=",".join(map(str, bbox)), geometryType="esriGeometryEnvelope",
                    inSR="4326", spatialRel="esriSpatialRelIntersects")
    for fmt, paged in (("geojson", True), ("json", True), ("json", False)):
        feats, answered = [], False
        for k in range(max_pages if paged else 1):
            params = dict(base, f=fmt)
            if paged:
                params.update(resultOffset=k * page, resultRecordCount=page)
            j = get_json(url + "/query", params, tries=2, timeout=60)
            fs = j.get("features") if isinstance(j, dict) else None
            if fs is None:
                break
            answered = True
            feats += fs if fmt == "geojson" else [_esri_to_geojson(x) for x in fs]
            if len(fs) < page and not j.get("exceededTransferLimit"):
                break
        if answered:
            return feats
    return None

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

def static_layers(now, rigs_path=RIGS, reefs_path=REEFS, launches_path=LAUNCHES, cache=None):
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
    try:
        _launches(now, launches_path, stale, cache if cache is not None else {})
    except Exception:
        log.exception("launches.json update failed")

# ---------------------------------------------------------------- boat launches (OpenStreetMap)
# Boat ramps mapped in OpenStreetMap as leisure=slipway (© OpenStreetMap contributors, ODbL). No state
# agency publishes a current launch list as open data. Refreshed weekly; private ramps are left out.
OVERPASS_URLS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter")
LAUNCH_RETRY_H = 6          # after a failed download, wait this long before trying again (the servers are often busy)
LAUNCH_BOX = (28.8, -94.1, 33.1, -88.7)          # south, west, north, east: Louisiana and its coast

# Ramps that are mapped but aren't for the public, whatever their access tag says
NOT_PUBLIC = re.compile(r"\b(coast guard|uscg|navy|naval|military|air force|army|national guard|police|sheriff|fire department)\b", re.I)
UNNAMED = "Boat launch"
NAMED_EXTRA_MI = 2          # a named ramp up to this much farther is shown instead of an unnamed one

def parse_launches(j):
    """Overpass JSON -> [[lat, lon, name, note], ...] for ramps the public can use."""
    out, seen = [], set()
    for e in (j or {}).get("elements") or []:
        t = e.get("tags") or {}
        if t.get("access") in ("private", "no", "customers") or t.get("boat") == "no":
            continue
        lat, lon = (e.get("lat"), e.get("lon")) if "lat" in e else ((e.get("center") or {}).get("lat"), (e.get("center") or {}).get("lon"))
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        key = (round(lat, 3), round(lon, 3))           # the same ramp mapped as a point and a line
        if key in seen:
            continue
        seen.add(key)
        name = str(t.get("name") or t.get("operator") or UNNAMED)[:60]
        if NOT_PUBLIC.search(name) or NOT_PUBLIC.search(str(t.get("operator") or "")):
            continue
        note = "fee" if t.get("fee") == "yes" else "free" if t.get("fee") == "no" else ""
        out.append([round(lat, 5), round(lon, 5), name, note])
    return out

def _launches(now, path, stale, cache):
    if not stale(path):
        return
    tried = cache.get("launches_tried")                # kept between runs in stations_cache.json
    try:
        if tried and (now - parse_iso(tried)).total_seconds() < LAUNCH_RETRY_H * 3600:
            return
    except (TypeError, ValueError):
        pass
    cache["launches_tried"] = now.isoformat()
    s, w, n, e = LAUNCH_BOX
    q = f'[out:json][timeout:50];(nwr["leisure"="slipway"]({s},{w},{n},{e}););out center tags;'
    for url in OVERPASS_URLS:
        try:
            req = Request(url, data=urlencode({"data": q}).encode(), headers={"User-Agent": f"{APP} ({CONTACT})"})
            with urlopen(req, timeout=60) as r:
                ramps = parse_launches(json.load(r))
        except Exception as ex:                       # noqa: BLE001 - try the next mirror
            log.warning("boat launches from %s failed: %s", urlparse(url).netloc, ex)
            continue
        if len(ramps) < 20:                            # an empty or broken answer must not replace a good file
            log.warning("boat launches: only %d from %s, keeping the previous file", len(ramps), urlparse(url).netloc)
            continue
        with open(path, "w") as fh:
            json.dump({"generated": now.isoformat(), "source": "OpenStreetMap contributors (ODbL)", "launches": ramps}, fh, separators=(",", ":"))
        log.info("launches.json: %d boat launches from %s", len(ramps), urlparse(url).netloc)
        cache.pop("launches_tried", None)
        return

def nearest_launches(results, path=LAUNCHES, max_mi=15):
    """Give each inshore spot its nearest public launch (within max_mi), for the spot page."""
    try:
        with open(path) as f:
            ramps = json.load(f).get("launches") or []
    except (OSError, ValueError):
        return
    ramps = [x for x in ramps if not NOT_PUBLIC.search(str(x[2]))]      # also cleans an older weekly file
    for r in results:
        if r.get("zone") == "offshore" or r.get("lat") is None:
            continue
        near = sorted((miles(r["lat"], r["lon"], la, lo), la, lo, nm, nt) for la, lo, nm, nt in ramps)
        best = near[0] if near else None
        named = next((x for x in near if x[3] != UNNAMED), None)
        if best and named and best[3] == UNNAMED and named[0] <= best[0] + NAMED_EXTRA_MI:
            best = named                                              # "Bayou Rigaud launch" beats "Boat launch" a mile closer
        if best and best[0] <= max_mi:
            r["launch"] = {"name": best[3], "lat": best[1], "lon": best[2], "mi": round(best[0], 1), "note": best[4]}

def _rigs(now, rigs_path, bbox, stale):
    if not stale(rigs_path):
        return
    pts = boem_platforms(bbox)
    if pts:
        with open(rigs_path, "w") as fh:
            json.dump({"generated": now.isoformat(), "source": "BOEM platform structures (data.boem.gov)", "rigs": pts}, fh,
                      separators=(",", ":"))
        log.info("rigs.json: %d platforms from BOEM download files", len(pts))
        return
    if True:                                                       # fallback: ArcGIS map services
        feats = None
        for url in RIG_SOURCES:
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
                                                  "PLATFORM_", "COMPLEX_ID_NUM", "complex_id") if p.get(k)), "")
                pts.append([round(ll[0], 4), round(ll[1], 4), name])
            with open(rigs_path, "w") as fh:
                json.dump({"generated": now.isoformat(), "source": "BOEM/BSEE OCS platforms", "rigs": pts}, fh,
                          separators=(",", ":"))
            log.info("rigs.json: %d platforms", len(pts))
        else:
            log.warning("rigs.json: no platform source answered; will retry next run")

def _reefs(now, reefs_path, stale, bbox=(-94.5, 26.4, -87.5, 30.6)):
    if not stale(reefs_path):
        return
    for url, label, use_bbox in REEF_SOURCES:
        feats = arcgis_geojson(url, bbox=bbox if use_bbox else None)
        if not feats:
            continue
        reefs = []
        for f in feats:
            p, ll = f.get("properties") or {}, point_of(f.get("geometry"))
            if not ll:
                continue
            name = next((str(p[k]) for k in ("multilabel", "reefname", "NAME", "Name", "REEF_NAME", "SITE_NAME") if p.get(k)), "Artificial reef")
            d = p.get("waterdepthfeet")
            if isinstance(d, (int, float)) and d > 0:
                name += f" ({round(d)} ft)"
            reefs.append([round(ll[0], 4), round(ll[1], 4), name])
        with open(reefs_path, "w") as fh:
            json.dump({"generated": now.isoformat(), "source": label, "reefs": reefs}, fh, separators=(",", ":"))
        log.info("reefs.json: %d reefs from %s", len(reefs), label)
        return
    log.warning("reefs.json: no reef source answered; will retry next run")

# ---------------------------------------------------------------- tropical storms (NHC)
TROPICAL = "tropical.json"
NHC_MS = "https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather/MapServer"
# Atlantic storm slots AT1..AT5 in that service: (forecast points, track, cone, watches/warnings)
NHC_SLOTS = ((6, 7, 8, 9), (32, 33, 34, 35), (58, 59, 60, 61), (84, 85, 86, 87), (110, 111, 112, 113))
NHC_OUTLOOK7 = 3          # seven-day potential development areas
# Louisiana coast, Sabine Pass to the Pearl River, for "is the coast in the cone" checks
LA_COAST = ((29.73, -93.85), (29.60, -92.80), (29.55, -92.30), (29.35, -91.40), (29.10, -90.70),
            (29.15, -90.10), (29.25, -89.60), (29.00, -89.30), (29.70, -89.40), (30.18, -89.60))
LA_BOX = (28.6, -94.1, 30.6, -88.7)     # lat_lo, lon_lo, lat_hi, lon_hi, for watches/warnings
GULF_BOX = (18.0, -98.0, 31.0, -80.5)
TROP_LEVELS = ("none", "far", "gulf", "near", "cone", "watch", "warning")
TROP_TYPES = {"HU": "Hurricane", "TS": "Tropical Storm", "TD": "Tropical Depression", "STS": "Subtropical Storm",
              "SD": "Subtropical Depression", "PTC": "Potential Tropical Cyclone", "PT": "Post-Tropical Cyclone",
              "MH": "Major Hurricane", "M": "Major Hurricane", "H": "Hurricane", "S": "Tropical Storm", "D": "Tropical Depression"}

def _prop(p, *names):
    """First non-empty attribute among names, ignoring case."""
    low = {str(k).lower(): v for k, v in (p or {}).items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", " "):
            return v
    return None

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _rings(geom, step_max=160):
    """Outer rings of a (multi)polygon as [[lat, lon], ...], thinned for the page."""
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates") or []
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else []
    out = []
    for poly in polys:
        ring = poly[0] if poly else []
        if len(ring) >= 3:
            step = max(1, len(ring) // step_max)
            out.append([[round(y, 3), round(x, 3)] for x, y in ring[::step]])
    return out

def _paths(geom):
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates") or []
    paths = [c] if t == "LineString" else c if t == "MultiLineString" else []
    return [[[round(y, 3), round(x, 3)] for x, y in p] for p in paths if len(p) >= 2]

def _inside(lat, lon, ring):
    """Point in polygon (ray casting) for a ring of [lat, lon]."""
    hit, j = False, len(ring) - 1
    for i in range(len(ring)):
        (yi, xi), (yj, xj) = ring[i], ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            hit = not hit
        j = i
    return hit

def _in_box(lat, lon, box):
    return box[0] <= lat <= box[2] and box[1] <= lon <= box[3]

def _storm(slot, now):
    """One active Atlantic storm from the NHC map service, or None if the slot is empty.
    Raises LookupError if the service didn't answer."""
    pts_id, trk_id, cone_id, ww_id = NHC_SLOTS[slot]
    pts = arcgis_geojson(f"{NHC_MS}/{pts_id}", max_pages=1, page=200)
    if pts is None:
        raise LookupError("NHC map service didn't answer")
    if not pts:
        return None
    rows = []
    for f in pts:
        p, ll = f.get("properties") or {}, point_of(f.get("geometry"))
        if ll:
            rows.append(dict(lat=round(ll[0], 2), lon=round(ll[1], 2), tau=_num(_prop(p, "tau", "fcstprd")) or 0,
                             kt=_num(_prop(p, "maxwind", "intensity")), label=_prop(p, "dvlbl", "tcdvlp", "stormtype"),
                             when=_prop(p, "datelbl", "fldatelbl", "validtime"), p=p))
    if not rows:
        return None
    rows.sort(key=lambda r: r["tau"])
    first = rows[0]["p"]
    name = str(_prop(first, "stormname", "storm_name", "name") or f"Storm AT{slot + 1}").title()
    typ = _prop(first, "stormtype", "tcdvlp", "dvlbl")
    typ = TROP_TYPES.get(str(typ).upper(), str(typ).title()) if typ else "Tropical cyclone"
    cone, track, ww = [], [], []
    for f in arcgis_geojson(f"{NHC_MS}/{cone_id}", max_pages=1) or []:
        cone += _rings(f.get("geometry"))
    for f in arcgis_geojson(f"{NHC_MS}/{trk_id}", max_pages=1) or []:
        track += _paths(f.get("geometry"))
    for f in arcgis_geojson(f"{NHC_MS}/{ww_id}", max_pages=1) or []:
        kind = str(_prop(f.get("properties"), "tcww", "type", "wwtype") or "").upper()
        for pth in _paths(f.get("geometry")):
            ww.append({"kind": kind, "path": pth})
    # How close does it come to Louisiana?
    best = None
    for r in rows:
        d = min(miles(r["lat"], r["lon"], a, b) for a, b in LA_COAST)
        if best is None or d < best[0]:
            best = (d, r)
    in_cone = any(_inside(a, b, ring) for ring in cone for a, b in LA_COAST)
    la_ww = [w["kind"] for w in ww if any(_in_box(a, b, LA_BOX) for a, b in w["path"])]
    if any(k in ("HWR", "TWR") or "WARN" in k for k in la_ww):
        level = "warning"
    elif la_ww:
        level = "watch"
    elif in_cone:
        level = "cone"
    elif best[0] <= 350:
        level = "near"
    elif any(_in_box(r["lat"], r["lon"], GULF_BOX) for r in rows):
        level = "gulf"
    else:
        level = "far"
    d, r = best
    kt = rows[0]["kt"]
    head = f"{typ} {name}" + (f" ({round(kt * 1.151)} mph)" if kt else "")
    closest = (f"closest forecast position about {round(d / 10) * 10:,} miles from the coast"
               + (f" in {round(r['tau'])} hours" if r["tau"] else " now"))
    text = {"warning": f"{head}: hurricane or tropical storm warnings cover part of the Louisiana coast. Stay off the water.",
            "watch": f"{head}: watches cover part of the Louisiana coast. Plan to be off the water and secure your boat.",
            "cone": f"{head}: the Louisiana coast is inside the forecast cone; {closest}.",
            "near": f"{head}: {closest}. Swells and rough seas likely offshore.",
            "gulf": f"{head} is in the Gulf; {closest}.",
            "far": f"{head}: {closest}."}[level]
    return {"name": name, "type": typ, "wind_kt": kt, "level": level, "text": text,
            "closest_mi": round(d), "closest_h": r["tau"],
            "points": [[r["lat"], r["lon"], r["tau"], r["kt"], r["label"], r["when"]] for r in rows],
            "cone": cone, "track": track, "ww": ww}

def tropical(now, path=TROPICAL):
    """Active Atlantic storms and the 7-day outlook from NHC, with how much each threatens
    the Louisiana coast. Writes tropical.json and returns a short summary for latest.json,
    or None when the NHC service didn't answer (the previous file is kept)."""
    storms = []
    try:
        for slot in range(len(NHC_SLOTS)):
            s = _storm(slot, now)
            if s:
                storms.append(s)
    except LookupError as e:
        log.warning("tropical: %s", e)
        return None
    outlook = []
    for f in arcgis_geojson(f"{NHC_MS}/{NHC_OUTLOOK7}", max_pages=1) or []:
        p, rings = f.get("properties") or {}, _rings(f.get("geometry"))
        if not rings or not any(_in_box(a, b, GULF_BOX) for ring in rings for a, b in ring):
            continue
        cen = point_of(f.get("geometry")) or (rings[0][0][0], rings[0][0][1])
        outlook.append({"rings": rings, "prob2": _prop(p, "prob2day"), "prob7": _prop(p, "prob7day"),
                        "risk": _prop(p, "risk7day", "risk2day"),
                        "near": min(miles(cen[0], cen[1], a, b) for a, b in LA_COAST) <= 500})
    rank = max([TROP_LEVELS.index(s["level"]) for s in storms] + [0])
    level = TROP_LEVELS[rank]
    if storms:
        text = " ".join(s["text"] for s in sorted(storms, key=lambda s: -TROP_LEVELS.index(s["level"]))[:2])
    else:
        near = [o for o in outlook if o["near"]]
        text = (f"No active storms. NHC is watching an area in the Gulf ({near[0]['prob7'] or 'low'} chance of "
                "development in 7 days)." if near else "No active tropical storms in the Atlantic or Gulf.")
        if near and rank < 2:
            level, rank = "gulf", 2
    out = {"generated": now.isoformat(), "level": level, "rank": rank, "text": text, "storms": storms, "outlook": outlook,
           "source": "NOAA National Hurricane Center"}
    with open(path + ".tmp", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    log.info("tropical.json: %d storms, level %s", len(storms), level)
    return {"generated": out["generated"], "level": level, "rank": rank, "text": text,
            "storms": [{"name": s["name"], "type": s["type"], "level": s["level"], "closest_mi": s["closest_mi"],
                        "closest_h": s["closest_h"]} for s in storms]}

def previous_tropical(now, path=TROPICAL, max_h=12):
    """Summary from the last tropical.json if it is recent (used when NHC doesn't answer)."""
    try:
        with open(path) as f:
            d = json.load(f)
        if (now - parse_iso(d["generated"])).total_seconds() / 3600 > max_h:
            return None
        return {"generated": d["generated"], "level": d["level"], "rank": d["rank"], "text": d["text"], "stale": True,
                "storms": [{k: s.get(k) for k in ("name", "type", "level", "closest_mi", "closest_h")} for s in d.get("storms", [])]}
    except (OSError, KeyError, ValueError, TypeError):
        return None

# ---------------------------------------------------------------- tidal currents (CO-OPS)
# Predicted flood/ebb speed and slack times at NOAA current stations near tidal spots. Moving
# water at the passes matters more to many fish than how high the tide is.
CUR_RADIUS_MI = 12
_CST_LOCK = threading.Lock()

def current_stations(cache):
    """All CO-OPS current-prediction stations along the Louisiana coast (looked up once, kept)."""
    with _CST_LOCK:
        if cache.get("cstations") is None:
            j = get_json(MDAPI.replace("/%s/products.json", ".json"), dict(type="currentpredictions", units="english"), tries=2)
            out = []
            for st in (j or {}).get("stations") or []:
                la, lo = fnum(st.get("lat")), fnum(st.get("lng"))
                if la is None or lo is None or not (28.4 <= la <= 30.6 and -94.0 <= lo <= -88.7):
                    continue
                out.append([str(st.get("id")), str(st.get("name") or st.get("id")), la, lo, st.get("currbin")])
            if j is not None:
                cache["cstations"] = out
            else:
                return []
        return cache["cstations"]

def current_station(spot, cache):
    """Nearest current-prediction station within CUR_RADIUS_MI of a tidal inshore spot, or None."""
    key = ("cstn", spot["id"])
    if key in cache:
        v = cache[key]
        return v if v else None
    st = current_stations(cache)
    if not st:
        return None
    best = min(([miles(spot["lat"], spot["lon"], la, lo), sid, nm, b] for sid, nm, la, lo, b in st), default=None)
    cache[key] = [best[1], best[2], round(best[0], 1), best[3]] if best and best[0] <= CUR_RADIUS_MI else []
    return cache[key] or None

def _cp_rows(j):
    """Prediction rows from a currents_predictions answer (the layout differs by interval)."""
    cp = (j or {}).get("current_predictions")
    if isinstance(cp, dict):
        cp = cp.get("cp")
    return cp if isinstance(cp, list) else []

def current_events(station, now, cache):
    """Max flood (+), max ebb (-) and slack (0) events for the next week, as [(time, knots, type)]."""
    sid, _, _, b = station
    key = ("cur", sid)
    if key in cache:
        return cache[key]
    params = dict(station=sid, product="currents_predictions", interval="MAX_SLACK", units="english",
                  time_zone="gmt", format="json", application=APP,
                  begin_date=(now - timedelta(hours=8)).strftime("%Y%m%d %H:%M"), range=180)
    if b not in (None, ""):
        params["bin"] = b
    out = []
    for r in _cp_rows(get_json(COOPS, params, tries=2)):
        try:
            t = datetime.strptime(str(r.get("Time")), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            continue
        typ = str(r.get("Type") or "").lower()
        v = fnum(r.get("Velocity_Major"))
        if v is None:
            v = 0.0
        if typ.startswith("slack"):
            v, typ = 0.0, "slack"
        elif typ.startswith("ebb"):
            v, typ = -abs(v), "ebb"
        elif typ.startswith("flood"):
            v, typ = abs(v), "flood"
        else:
            typ = "flood" if v > 0 else "ebb" if v < 0 else "slack"
        out.append((t, round(v, 2), typ))
    out.sort()
    cache[key] = out
    return out

def current_at(events, t):
    """Signed current speed (knots, + flood) at time t, eased between the predicted events."""
    for (t0, v0, _), (t1, v1, _) in zip(events, events[1:]):
        if t0 <= t <= t1:
            f = (t - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
            return v0 + (v1 - v0) * (1 - math.cos(math.pi * f)) / 2
    return None

def tidal_current(spot, now, cache):
    """Current now plus the next events for the page, or None."""
    if spot.get("marine") or not spot.get("tide"):
        return None
    st = current_station(spot, cache)
    if not st:
        return None
    ev = current_events(st, now, cache)
    v = current_at(ev, now) if ev else None
    if v is None:
        return None
    return {"station": st[0], "name": st[1], "mi": st[2], "now": round(v, 2),
            "dir": "Flood" if v > 0.15 else "Ebb" if v < -0.15 else "Slack",
            "events": [{"t": t.isoformat(), "v": x, "type": k} for t, x, k in ev
                       if now - timedelta(hours=2) <= t <= now + timedelta(hours=36)][:12],
            "_ev": ev}

# ---------------------------------------------------------------- freshwater diversions
# Mississippi River water let into the estuaries. Each one freshens and muddies its basin.
DIVERSIONS = (
    {"id": "bonnet", "name": "Bonnet Carré Spillway", "affects": "Lake Pontchartrain and western Mississippi Sound",
     "spots": ["pont"], "sites": [], "bbox": (-90.52, 29.98, -90.30, 30.12), "open_cfs": 5000,
     "url": "https://www.mvn.usace.army.mil/Missions/Mississippi-River-Flood-Control/Bonnet-Carre-Spillway-Overview/"},
    {"id": "davis", "name": "Davis Pond Freshwater Diversion", "affects": "Lake Cataouatche, Lake Salvador and upper Barataria",
     "spots": ["barat"], "sites": ["295501090190400"], "open_cfs": 1000, "url": "https://coastal.la.gov/diversion-operations/"},
    {"id": "caern", "name": "Caernarvon Freshwater Diversion", "affects": "Breton Sound, Lake Lery and Black Bay",
     "spots": ["venice"], "sites": ["295124089542100"], "open_cfs": 1000, "url": "https://coastal.la.gov/diversion-operations/"},
)

def _flow(feats):
    best = None
    for f in feats or []:
        p = f.get("properties") or {}
        v = fnum(p.get("value"))
        try:
            t = parse_iso(p.get("time"))
        except (TypeError, ValueError):
            continue
        if v is not None and (best is None or t > best[1]):
            best = (v, t, str(p.get("monitoring_location_id") or ""))
    return best

def diversions(now, cache=None):
    """Latest discharge through each diversion from USGS (parameter 00060, cubic feet per second)."""
    out = {}
    for d in DIVERSIONS:
        best = None
        for site in d["sites"]:
            best = _flow(wd_items("latest-continuous", dict(monitoring_location_id=f"USGS-{site}", parameter_code="00060")))
            if best:
                break
        if not best and d.get("bbox"):
            b = d["bbox"]
            best = _flow(wd_items("latest-continuous", dict(bbox=",".join(map(str, b)), parameter_code="00060", limit=50)))
        rec = {"name": d["name"], "affects": d["affects"], "spots": d["spots"], "url": d["url"]}
        if best and now - best[1] <= timedelta(days=3):
            q = best[0]
            rec.update(cfs=round(q), time=best[1].isoformat(),
                       status="Open" if d["id"] == "bonnet" and q >= d["open_cfs"] else
                              "Flowing" if q >= d["open_cfs"] else "Low flow" if q > 50 else "Closed")
        else:
            rec.update(cfs=None, status="Unknown")
        if cache is not None:                    # remembered between runs in the station cache
            seen = cache.setdefault("div_open", {})
            if rec["status"] in ("Open", "Flowing") and d["id"] == "bonnet":
                seen[d["id"]] = now.isoformat()
            if seen.get(d["id"]):
                rec["last_open"] = seen[d["id"]]
        out[d["id"]] = rec
    return out

# ---------------------------------------------------------------- dead zone (hypoxia)
DEADZONE = "deadzone.json"
HYPOXIA_SERVICES = ("https://gis.ngdc.noaa.gov/arcgis/rest/services/Hypoxia/MapServer",
                    "https://gis.ncdc.noaa.gov/arcgis/rest/services/ms/hypoxia/MapServer")

def _layer_year(name):
    ys = [int(y) for y in re.findall(r"(?:19|20)\d\d", name or "")]
    return max(ys) if ys else None

def deadzone(now, path=DEADZONE):
    """Newest bottom-oxygen (hypoxia) contours or areas from NOAA's Gulf Hypoxia Watch.
    Refreshed weekly. Returns the parsed file (new or kept) or None."""
    try:
        with open(path) as f:
            old = json.load(f)
        if (now - parse_iso(old["generated"])).days < STATIC_DAYS:
            return old
    except (OSError, KeyError, ValueError, TypeError):
        old = None
    for svc in HYPOXIA_SERVICES:
        info = get_json(svc, dict(f="json"), tries=2)
        layers = (info or {}).get("layers") or []
        cand = []
        for L in layers:
            nm = str(L.get("name") or "")
            gt = str(L.get("geometryType") or "")
            if L.get("subLayerIds") or not re.search(r"oxygen|contour|hypox|DO\b", nm, re.I):
                continue
            if gt and not any(k in gt for k in ("Polygon", "Polyline")):
                continue
            cand.append((_layer_year(nm) or 0, "Polygon" in gt, L.get("id"), nm))
        for yr, _poly, lid, nm in sorted(cand, reverse=True):
            feats = arcgis_geojson(f"{svc}/{lid}", bbox=(-95.0, 27.5, -88.0, 30.5), max_pages=3)
            if not feats:
                continue
            areas, lines = [], []
            for f in feats:
                g = f.get("geometry") or {}
                areas += _rings(g)
                lines += _paths(g)
            if not areas and not lines:
                continue
            out = {"generated": now.isoformat(), "layer": nm, "year": yr or None, "areas": areas, "lines": lines,
                   "source": "NOAA Gulf of Mexico Hypoxia Watch (summer survey)"}
            with open(path + ".tmp", "w") as fh:
                json.dump(out, fh, separators=(",", ":"))
            os.replace(path + ".tmp", path)
            log.info("deadzone.json: %s (%d areas, %d lines)", nm, len(areas), len(lines))
            return out
    log.warning("deadzone.json: no hypoxia layer answered")
    return old

def hypoxia_flag(spot, dz, now):
    """'Low' if an offshore spot sits inside this summer's measured low-oxygen area, 'OK' if
    outside it, None when there is no current-season survey to go by."""
    if not spot.get("marine") or not dz or not dz.get("areas"):
        return None
    if dz.get("year") != now.year or not 5 <= now.month <= 10:
        return None
    return "Low" if any(_inside(spot["lat"], spot["lon"], r) for r in dz["areas"]) else "OK"

# ---------------------------------------------------------------- NWS radar
RADAR_MS = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/radar/radar_base_reflectivity/MapServer"

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

SPOT_BUDGET_S = 20 * 60          # stop starting new spots after this long (the workflow step allows 35 minutes)

def run_once():
    now, cache, results = datetime.now(UTC), {}, []
    disk = load_station_cache()
    hydrate_station_cache(cache, disk)
    failed = []
    def one(spot):
        if time.time() - t0 > SPOT_BUDGET_S:    # far over time: publish what's in rather than miss the deploy
            log.warning("spot %s skipped: the run is over its %d-minute budget", spot["id"], SPOT_BUDGET_S // 60)
            return None
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
    for key, fn in (("deadzone", lambda: deadzone(now)), ("diversions", lambda: diversions(now, cache)),
                    ("sargassum", lambda: sargassum(now))):
        try:
            cache[key] = fn()
        except Exception:                       # extras must never stop the run
            log.exception("%s failed", key)
            cache[key] = None
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
    try:
        cache["tropical"] = tropical(now) or previous_tropical(now)
    except Exception:
        log.exception("tropical storms failed")
        cache["tropical"] = previous_tropical(now)
    for name, fn in (("ocean lines", lambda: ocean_features(now)), ("rigs and reefs", lambda: static_layers(now, cache=cache))):
        try:
            res = fn()
            if name == "ocean lines" and res is not None and cache.get("sargassum"):
                res["sargassum"] = cache["sargassum"]
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
    nearest_launches(results)
    save(results, now)
    with open("forecast.json.tmp", "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    os.replace("forecast.json.tmp", "forecast.json")
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated": now.isoformat(), "rivers": cache.get("rivers") or {}, "marine": cache.get("marine") or {},
                   "tropical": cache.get("tropical"), "diversions": cache.get("diversions") or {},
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
    def check(name, fn, critical=False):
        """fn returns (ok, detail); a crash in one check is a failed check, not a failed report."""
        try:
            ok, detail = fn()
        except Exception as e:                       # noqa: BLE001 - report it and carry on
            ok, detail = False, f"error: {type(e).__name__}: {e}"[:160]
        add(name, ok, detail, critical)
    def coops():
        d = coops_latest("8761724", "water_temperature")
        return d, f"water temp {d.get('v')} F at {d.get('t')} UTC" if d else "no data"
    def wx():
        w = weather(29.27, -89.95)
        return w and w.get("series"), "forecast OK" if w else "no data"
    def nws():
        al = get_json("https://api.weather.gov/alerts/active", dict(point="29.27,-89.95"), tries=2)
        return al is not None, f"{len((al or {}).get('features', []))} active" if al is not None else "no answer"
    def usgs():
        u = usgs_source_new("07381515")
        return u, ", ".join(sorted(u)) if u else f"nothing returned{'' if USGS_KEY else ' (no USGS_API_KEY set)'}"
    def river():
        g = river_gauge("NORL1", now)
        return g, f"Mississippi at New Orleans {g['stage']} ft" if g else "no data"
    def kd490():
        kd = sat_kd490(29.27, -89.95, {}, now)
        return kd, f"{kd[2]}, {kd[1]:%Y-%m-%d}" if kd else "no dataset answered"
    def buoy():   # 42001, south of Southwest Pass. 42040 no longer reports weather (NDBC lists it met="n").
        b = buoy_obs("42001") or {}
        return b, ", ".join(sorted(b)) or "no data"
    def ssh():
        r = griddap_box("https://gcoos5.geos.tamu.edu/erddap", f"SSH_GoA_{now.year}", "adt", 28.0, 29.0, -90.0, -89.0, extra="")
        return r, f"newest {r[0]}" if r else "no answer"
    def layer(urls, bbox):
        def fn():
            for u in urls:
                feats = arcgis_geojson(u, bbox=bbox, max_pages=1, page=50)
                if feats:
                    return True, f"{len(feats)} from {urlparse(u).netloc}"
            return False, "no source answered"
        return fn
    def nhc():
        pts = arcgis_geojson(f"{NHC_MS}/{NHC_SLOTS[0][0]}", max_pages=1, page=50)
        return pts is not None, (f"{len(pts)} forecast points for storm 1" if pts else "answered, no active storm in slot 1") if pts is not None else "no answer"
    def marine():
        mf = marine_forecast("GMZ550", {})
        return mf, (mf["periods"][0]["name"] + ": " + mf["periods"][0]["text"][:60]) if mf and mf["periods"] else "no answer"
    check("NOAA tides and currents (Grand Isle)", coops, True)
    check("Open-Meteo weather", wx, True)
    check("NWS alerts", nws, True)
    check("USGS Water Data API (new)", usgs)
    check("NWS river gauges", river)
    check("Satellite water clarity (Kd490)", kd490)
    def mur():
        ss = sat_sst(28.5, -90.6, {})
        return ss, f"{ss['sst_f']:.1f} F, break {ss['break']:.2f} F/mi, {ss['time'][:10]} ({ss.get('src')})" if ss else "no server answered"
    def chl():
        c = sat_chlorophyll(29.27, -89.95, {})
        return c, f"{c[0]} mg/m3, {str(c[1])[:10]}" if c else "no server answered"
    check("Satellite sea temperature and breaks", mur)
    def ssh():
        h = sat_ssh_edge(28.0, -89.0, {})
        return h, f"edge {h['edge']} cm/mi, {h['time'][:10]}" if h else "no server answered"
    check("Satellite sea height (Loop Current and eddy edges)", ssh)
    check("Satellite water color (chlorophyll)", chl)
    check("NDBC buoy 42001", buoy)
    check("Sea surface height (Loop Current)", ssh)
    def rigs():
        pts = boem_platforms((-94.5, 26.4, -87.5, 30.6))
        if pts:
            return True, f"{len(pts)} standing platforms from BOEM download files"
        return layer(RIG_SOURCES, (-90.5, 28.5, -90.0, 29.0))()
    check("Offshore platforms", rigs)
    check("Artificial reefs", layer([u for u, _, _ in REEF_SOURCES], (-90.5, 28.5, -89.5, 29.2)))
    check("NHC tropical storms", nhc)
    check("NWS marine forecast text", marine)
    def cur():
        st = current_stations({})
        return bool(st), f"{len(st)} current stations on the Louisiana coast" if st else "no answer"
    def div():
        d = diversions(now)
        known = [f"{v['name'].split()[0]} {v['status'].lower()}" for v in d.values() if v["status"] != "Unknown"]
        return bool(known), ", ".join(known) or "no diversion flow readings"
    def radar():
        j = get_json(RADAR_MS, dict(f="json"), tries=2)
        return j is not None and "layers" in j, "map service answered" if j else "no answer"
    check("NOAA tidal current predictions", cur)
    check("Freshwater diversions (USGS flow)", div)
    check("NWS radar map", radar)
    try:
        checks = map_checks()
    except Exception as e:                           # noqa: BLE001
        checks = [("Map layers", False, f"error: {e}"[:160])]
    for name, ok, detail in checks:
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

DEAD_CHECK = {"chl": "satellite water color", "kd490": "satellite water clarity", "sbreak": "satellite temperature breaks",
              "ssat": "satellite sea temperature", "vis": "water visibility", "sal": "salinity",
              "temp": "water temperature", "wind": "wind", "tide": "tide"}

def dead_sources(spots, min_spots=3):
    """Readings that are missing at every spot that should have them (a source that is down or blocked)."""
    dead = []
    for k, name in DEAD_CHECK.items():
        # chl and kd490 are fetched for every spot but always listed as optional (not_applicable)
        want = spots if k in ("chl", "kd490") else [s for s in spots if k not in ((s.get("quality") or {}).get("not_applicable") or [])]
        if len(want) >= min_spots and all((s.get("conditions") or {}).get(k) is None for s in want):
            dead.append(f"{name} ({k}) is missing at all {len(want)} spots that use it")
    return dead

# Readings outside these are broken sensors or unit mix-ups, not weather
SANE = {"temp": (30, 105), "sal": (0, 45), "wind": (0, 120), "wave": (0, 60)}

def validate(path=OUT, min_spots=None, min_completeness=0.45, max_age_h=3, strict=False):
    """Sanity-check a freshly written latest.json before it is deployed. Returns problems.
    A reading missing at every spot is only a warning when deploying (the scores still work without
    it), but a problem with strict=True, which the daily health check uses so it opens an issue.
    Next to a fresh latest.json it also checks forecast.json, and that history.json didn't shrink
    (history.json.prev is the live copy the workflow downloaded first): a failed download must never
    replace two weeks of history with an empty one."""
    if min_spots is None:
        min_spots = max(15, int(0.8 * len(SPOTS)))
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
    for dead in dead_sources(spots):
        if strict:
            problems.append(dead)
        else:
            print("WARNING:", dead)
    odd = [f"{s.get('id')} {k}={(s.get('conditions') or {}).get(k)}" for s in spots for k, (lo, hi) in SANE.items()
           if isinstance((s.get("conditions") or {}).get(k), (int, float)) and not lo <= s["conditions"][k] <= hi]
    if len(odd) > 5:
        problems.append(f"{len(odd)} readings out of range, e.g. {', '.join(odd[:4])}")
    elif odd:
        print("WARNING: readings out of range:", ", ".join(odd))
    here = os.path.dirname(os.path.abspath(path))
    fc_path, hist_path = os.path.join(here, "forecast.json"), os.path.join(here, HISTORY)
    if not strict and os.path.exists(fc_path) and spots:
        try:
            with open(fc_path) as f:
                n_fc = len((json.load(f).get("spots") or {}))
            if n_fc < 0.8 * len(spots):
                problems.append(f"forecast.json covers only {n_fc} of {len(spots)} spots")
        except (OSError, ValueError, AttributeError) as e:
            problems.append(f"can't read forecast.json: {e}")
    if not strict and os.path.exists(hist_path + ".prev"):
        try:
            with open(hist_path) as f:
                new_n = len(json.load(f).get("t") or [])
            with open(hist_path + ".prev") as f:
                old_n = len(json.load(f).get("t") or [])
            if old_n >= 24 and new_n < old_n / 2:
                problems.append(f"history.json shrank from {old_n} to {new_n} points")
        except (OSError, ValueError) as e:
            problems.append(f"can't compare history.json with the live copy: {e}")
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
        probs = validate(sys.argv[2], strict=True) if cmd == "validate-file" and len(sys.argv) > 2 else validate()
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
