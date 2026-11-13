"""Canned responses for every data source ingest.py calls, so the whole pipeline can be
tested offline. install() swaps them into the ingest module; calls are counted in CALLS."""
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

UTC = timezone.utc
now = datetime.now(UTC).replace(second=0, microsecond=0)
f = lambda t: t.strftime("%Y-%m-%d %H:%M")
CALLS = {}
STATE = {"kd": "fresh", "usgs_key_sent": None}

def bump(k):
    CALLS[k] = CALLS.get(k, 0) + 1

def fake_json(url, params=None, tries=3, timeout=15, headers=None):
    p=dict(params or {})
    if "?" in url: p.update({k:v[0] for k,v in parse_qs(urlparse(url).query).items()})
    if "mdapi" in url and "stations.json" in url:
        bump("cstations")
        return {"stations": [{"id": "ACT0001", "name": "Barataria Pass", "lat": 29.27, "lng": -89.94, "currbin": 1},
                             {"id": "ACT9999", "name": "Far away", "lat": 27.0, "lng": -80.0, "currbin": 1}]}
    if "datagetter" in url and p.get("product") == "currents_predictions":
        bump("currents"); assert p["interval"] == "MAX_SLACK"
        t0 = datetime.strptime(p["begin_date"], "%Y%m%d %H:%M").replace(tzinfo=UTC); rows = []
        for k in range(60):                    # slack, max flood, slack, max ebb ... every ~3.1 h
            typ = ["slack", "flood", "slack", "ebb"][k % 4]
            rows.append({"Time": f(t0 + timedelta(minutes=186 * k)), "Type": typ,
                         "Velocity_Major": {"slack": 0, "flood": 1.8, "ebb": -2.2}[typ]})
        return {"current_predictions": {"cp": rows}}
    if "radar_base_reflectivity" in url:
        bump("radar"); return {"layers": [{"id": 0, "name": "RIDGE 1x1 km Base Reflectivity"}]}
    if "Hypoxia/MapServer" in url:
        bump("hypoxia")
        if "/query" not in url:
            return {"layers": [{"id": 1, "name": "Stations 2026", "geometryType": "esriGeometryPoint"},
                               {"id": 3, "name": f"Bottom Dissolved Oxygen Contours {now.year}", "geometryType": "esriGeometryPolygon"},
                               {"id": 4, "name": "Bottom Dissolved Oxygen Contours 2019", "geometryType": "esriGeometryPolygon"}]}
        return {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-91.0, 28.3], [-90.2, 28.3], [-90.2, 28.8], [-91.0, 28.8], [-91.0, 28.3]]]}, "properties": {}}]}
    if "datagetter" in url:
        bump("coops"); pr=p["product"]
        if pr=="water_temperature":
            return {"data":[{"t":f(now-timedelta(minutes=6*k)),"v":"%.1f"%(66+k*0.008)} for k in range(300,-1,-1)]}
        if pr=="conductivity": return {"data":[{"t":f(now-timedelta(minutes=12)),"v":"24.0"}]}
        if pr=="water_level": return {"data":[{"t":f(now-timedelta(minutes=6)),"v":"1.45"}]}
        if pr=="wind": return {"data":[{"t":f(now-timedelta(minutes=12)),"s":"9.7"}]}
        if pr=="predictions" and p.get("interval")=="6":
            t0=datetime.strptime(p["begin_date"],"%Y%m%d %H:%M").replace(tzinfo=UTC)
            return {"predictions":[{"t":f(t0+timedelta(minutes=6*k)),"v":"0.80"} for k in range(21)]}
        if pr=="predictions":
            bump("tides"); start=now-timedelta(days=1); out=[]
            for k in range(22):
                t=start+timedelta(hours=6.21+12.42*k); out.append({"t":f(t),"v":"1.1" if k%2==0 else "0.1","type":"H" if k%2==0 else "L"})
            return {"predictions":out}
    if "marine-api" in url:
        base=now.replace(minute=0); times=[base+timedelta(hours=h) for h in range(-48,192)]
        n=len(times); return {"hourly":{"time":[t.strftime("%Y-%m-%dT%H:%M") for t in times],"wave_height":[1.0]*n,"wave_period":[5]*n,"sea_surface_temperature":[25.0]*n,"ocean_current_velocity":[1.5]*n}}
    if "open-meteo" in url:
        base=now.replace(minute=0); times=[base+timedelta(hours=h) for h in range(-48,169)]
        n=len(times)
        return {"current":{"time":base.strftime("%Y-%m-%dT%H:%M"),"wind_speed_10m":11,"wind_direction_10m":200,"cloud_cover":40,"surface_pressure":1012.0,"temperature_2m":64},
          "hourly":{"time":[t.strftime("%Y-%m-%dT%H:%M") for t in times],"wind_speed_10m":[8+4*math.sin(i/5) for i in range(n)],
           "wind_direction_10m":[180]*n,"cloud_cover":[50]*n,"surface_pressure":[1016-i*0.02 for i in range(n)],
           "precipitation":[0.0]*n,"temperature_2m":[65+5*math.sin(i/24*6.28) for i in range(n)]}}
    if "api.weather.gov/zones" in url:
        bump("zones"); lat = float(p["point"].split(",")[0])
        if p["type"] == "coastal" and lat > 28.6:
            return {"features": [{"properties": {"id": "GMZ550"}}]}
        if p["type"] == "offshore":
            return {"features": [{"properties": {"id": "GMZ089"}}]}
        return {"features": []}
    if "/griddap/" in url and "AFAI" in url:
        bump("afai")
        return fake_grid(url) if STATE.get("afai", "ok") == "ok" else None
    if "/griddap/" in url and ("(26.4)" in url or "(30.5)" in url or "SSH_GoA" in url):   # ocean_features boxes only
        bump("griddap"); return fake_grid(url)
    if "mapservices.weather.noaa.gov/tropical" in url:
        bump("nhc"); return fake_nhc(url, p)
    if "/query" in url and ("arcgis" in url or "mapping" in url):
        bump("arcgis")
        if "NMSP/FGB" in url:          # an older server: no GeoJSON, no paging (Esri JSON only)
            if p.get("f") != "json" or "resultOffset" in p:
                return {"error": {"code": 400, "message": "Invalid or missing input parameters."}}
            return {"features": [{"geometry": {"rings": [[[-90.1, 28.5], [-90.0, 28.5], [-90.0, 28.6], [-90.1, 28.5]]]},
                                  "attributes": {"multilabel": "South Timbalier 128"}}]}
        return {"features": [{"geometry": {"type": "Point", "coordinates": [-90.2 + k * 0.01, 28.4]},
                              "properties": {"STRUCTURE_NAME": f"A-{k}", "REMOVAL_DATE": None if k % 5 else 1600000000000}}
                             for k in range(20)]}
    if "weather.gov" in url: bump("nws"); return {"features":[{"properties":{"event":"Small Craft Advisory","severity":"Minor","ends":None}}]}
    if "api.waterdata.usgs.gov" in url:
        bump("wd:"+url.split("/collections/")[1].split("/")[0]); STATE["usgs_key_sent"] = "X-Api-Key" in (headers or {})
        if "latest-continuous" in url:
            return {"type":"FeatureCollection","features":[
              {"geometry":{"type":"Point","coordinates":[-91.0,29.9]},"properties":{"monitoring_location_id":"USGS-07381600","parameter_code":p["parameter_code"],"time":(now-timedelta(hours=1)).isoformat(),"value":"5000"}},
              {"geometry":{"type":"Point","coordinates":[-91.2,29.7]},"properties":{"monitoring_location_id":"USGS-99999999","parameter_code":p["parameter_code"],"time":(now-timedelta(days=30)).isoformat(),"value":"1"}}]}
        if "/continuous/" in url:
            assert p["time"]=="P1D" and p["monitoring_location_id"].startswith("USGS-")
            feats=[]
            for k in range(97):
                t=(now-timedelta(minutes=15*k)).isoformat().replace("+00:00","Z")
                feats.append({"properties":{"parameter_code":"00010","statistic_id":"00011","time_series_id":"a","time":t,"value":str(17.0+k*0.01)}})
                feats.append({"properties":{"parameter_code":"00095","statistic_id":"00011","time_series_id":"b","time":t,"value":"3000"}})
                feats.append({"properties":{"parameter_code":"63680","statistic_id":"00011","time_series_id":"d","time":t,"value":"25"}}) if p["monitoring_location_id"]=="USGS-07381515" else None
                feats.append({"properties":{"parameter_code":"00065","statistic_id":"00011","time_series_id":"c","time":t,"value":str(4.0-k*0.01)}})
            return {"features":feats}
        return {"features":[]}
    if "nwis" in url: bump("legacy-usgs"); return None
    if "api.water.noaa.gov/nwps" in url:
        bump("nwps"); lid=url.split("/gauges/")[1].split("/")[0]
        base={"NORL1":16.2,"MCGL1":2.6,"SMML1":18.0}[lid]
        if url.endswith("/stageflow"):
            obs=[{"validTime":(now-timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),"primary":base-0.02*h,"secondary":900} for h in range(48,-1,-1)]
            fc=[{"validTime":(now+timedelta(hours=6*k)).strftime("%Y-%m-%dT%H:%M:%SZ"),"primary":base+0.1*k-0.004*k*k} for k in range(1,29)]
            return {"observed":{"data":obs},"forecast":{"data":fc}}
        return {"status":{"observed":{"primary":base,"primaryUnit":"ft","secondary":950,"secondaryUnit":"kcfs","floodCategory":"action" if lid=="NORL1" else "no_flooding","validTime":now.strftime("%Y-%m-%dT%H:%M:%SZ")}},
                "flood":{"categories":{"action":{"stage":{"NORL1":11,"MCGL1":3.5,"SMML1":35}[lid]},"minor":{"stage":{"NORL1":17,"MCGL1":4,"SMML1":40}[lid]},"moderate":{"stage":-9999}}}}
    if "DEM_global_mosaic" in url:
        bump("ncei"); return {"value":"-128.4","name":"Pixel"}
    if "etopo180" in url:
        bump("etopo"); return {"table":{"rows":[[28.0,-90.0,-412]]}}
    if "kd_490" in url:
        bump("kd:"+url.split("griddap/")[1].split(".json")[0])
        mode=STATE["kd"]
        if mode=="fresh" and "nesdisVHNkd490Daily" in url:
            t=(now-timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z")
            return {"table":{"rows":[[t,0.0,29.3,-90.0,None],[t,0.0,29.3,-90.0,1.6],[t,0.0,29.3,-90.0,0.9],[t,0.0,29.3,-90.0,2.2]]}}
        if mode!="fresh" and "SCIDINEOF2km" in url:
            t=(now-timedelta(days=9)).strftime("%Y-%m-%dT12:00:00Z")
            return {"table":{"rows":[[t,0.0,29.3,-90.0,0.12],[t,0.0,29.3,-90.0,0.1]]}}
        return None
    if "erddap" in url: bump("erddap"); return None
    return None
def fake_grid(url):
    """A small grid with a warm eddy and a temperature front for any griddap box request."""
    import re
    var = url.split(".json?")[1].split("[")[0]
    nums = [float(x) for x in re.findall(r"\((-?\d+\.?\d*)\)", url.split("(last)")[1])]
    la0, la1, lo0, lo1 = (nums[-4], nums[-3], nums[-2], nums[-1])
    la0, la1 = min(la0, la1), max(la0, la1)
    rows, t = [], (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    n = 90 if var == "AFAI" else 30
    for i in range(n):
        la = la0 + (la1 - la0) * i / (n - 1)
        for j in range(n):
            lo = lo0 + (lo1 - lo0) * j / (n - 1)
            if var == "AFAI":                     # a weedline running east-west near 27.6 N
                v = 0.0012 if abs(la - 27.6) < 0.07 and -91.5 < lo < -89.5 else 0.00005
            elif var == "adt":
                v = 0.3 + 0.5 * math.exp(-(((la - 26) / 1.5) ** 2 + ((lo + 88) / 1.5) ** 2))
            elif var == "chlor_a":
                v = 0.1 * math.exp((la - la0) * 1.5)
            else:                                  # analysed_sst, deg C
                v = 22 + 3 / (1 + math.exp(-(la - (la0 + la1) / 2) / 0.1))
            rows.append([t, la, lo, v] if var != "chlor_a" else [t, 0.0, la, lo, v])
    names = ["time", "latitude", "longitude", var] if var != "chlor_a" else ["time", "altitude", "latitude", "longitude", var]
    return {"table": {"columnNames": names, "rows": rows}}

MARINE_TXT = """FZUS54 KLIX 120900
CWFLIX

Coastal Waters Forecast
National Weather Service New Orleans LA
300 AM CST Thu Nov 12 2026

GMZ550-122200-
Coastal waters from Port Fourchon LA to Lower Atchafalaya River LA out 20 NM-
300 AM CST Thu Nov 12 2026

...SMALL CRAFT EXERCISE CAUTION...
.TODAY...Northeast winds 15 to 20 knots. Seas 3 to 5 feet.
.TONIGHT...Northeast winds 10 to 15 knots. Seas 2 to 4 feet.
.FRIDAY...East winds 10 knots. Seas 2 feet.
$$
"""

BEACH_HTML = """<table><tr><th>Monitoring Site</th><th>Most Recent<br>Sample Date</th><th>Advisory Status</th></tr>
<tr><td>Grand Isle Beach Site #1</td><td>10/26/2026</td><td><strong>ADVISORY</strong></td></tr>
<tr><td>Holly Beach Site #1</td><td>10/20/2026</td><td>No Advisory</td></tr>
<tr><td>Martin (Mae&#39;s) Beach</td><td>10/20/2026</td><td>No Advisory</td></tr></table>"""

def fake_text(url, params=None, quiet_404=False):
    if "ldh.la.gov/beach" in url:
        bump("ldh"); return BEACH_HTML
    if "tgftp" in url:
        bump("tgftp")
        return MARINE_TXT if "/coastal/" in url and "gmz550" in url else None
    if "activestations" in url:
        bump("ndbc-list"); return '<stations><station id="42040" lat="29.2" lon="-88.2" met="y"/><station id="42001" lat="25.9" lon="-89.7" met="y"/><station id="kvaf" lat="29.0" lon="-89.6" met="y"/></stations>'
    if "realtime2" in url:
        bump("ndbc-obs")
        if "/kvaf." in url:                     # a coastal station with no realtime file (404)
            bump("ndbc-404"); return None
        hdr="#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE\n#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft\n"
        rows="".join(f"{t:%Y %m %d %H %M} 180 5.0 7.0 0.9 6 5 180 1015.0 24.0 25.5 20 MM MM MM\n" for t in [now-timedelta(minutes=30*k) for k in range(60)])
        return hdr+rows
    return None

# One hurricane in the Gulf heading for Terrebonne Bay, with a tropical storm watch on the coast
STATE["storm"] = True
def fake_nhc(url, p):
    lid = int(url.split("/MapServer/")[1].split("/")[0])
    pt = lambda lo, la, tau, kt: {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lo, la]},
                                  "properties": {"stormname": "TESTER", "stormtype": "HU", "maxwind": kt, "tau": tau,
                                                 "dvlbl": "H", "datelbl": f"+{tau}h"}}
    if not STATE["storm"] or lid not in (6, 7, 8, 9, 3):
        return {"type": "FeatureCollection", "features": []}
    if lid == 6:
        return {"features": [pt(-88.0, 25.0, 0, 85), pt(-89.0, 26.5, 24, 95), pt(-90.3, 28.3, 48, 100), pt(-90.8, 29.6, 72, 70)]}
    if lid == 7:
        return {"features": [{"geometry": {"type": "LineString", "coordinates": [[-88.0, 25.0], [-89.0, 26.5], [-90.3, 28.3], [-90.8, 29.6]]}, "properties": {}}]}
    if lid == 8:
        return {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-87.5, 24.8], [-88.5, 24.8], [-92.5, 30.2], [-89.0, 30.2], [-87.5, 24.8]]]}, "properties": {}}]}
    if lid == 9:
        return {"features": [{"geometry": {"type": "LineString", "coordinates": [[-91.3, 29.3], [-90.2, 29.1]]}, "properties": {"tcww": "TWA"}}]}
    return {"features": [{"geometry": {"type": "Polygon", "coordinates": [[[-94, 26], [-92, 26], [-92, 27.5], [-94, 27.5], [-94, 26]]]},
                          "properties": {"prob2day": "10%", "prob7day": "30%", "risk7day": "Low"}}]}

def _fixed(width, **cols):
    """One fixed-width line: cols maps 1-based start position -> text."""
    line = [" "] * width
    for start, txt in cols.values():
        line[start - 1:start - 1 + len(txt)] = txt
    return "".join(line)

def boem_zip(kind):
    """Small copies of BOEM's platloc / platstru fixed-width files, zipped like the real ones."""
    import io, zipfile
    if kind == "loc":
        rows = [("  10001", "1", "ST", "   128", "A", "  -90.2500", " 28.7500"),
                ("  10002", "1", "EI", "   330", "B", "  -91.6000", " 28.2000"),
                ("  10003", "1", "MC", "   777", "C", "  -88.0000", " 25.0000"),   # outside the Louisiana box
                ("  10004", "1", "WD", "    73", "D", "  -89.8000", " 28.9000")]   # removed (see below)
        txt = "\r\n".join(_fixed(112, a=(4, c), b=(12, n), c=(15, ar), d=(17, bl), e=(23, nm), f=(86, lo), g=(100, la))
                          for c, n, ar, bl, nm, lo, la in rows)
    else:
        rows = [("   10001", "  1", ""), ("   10002", "  1", ""), ("   10003", "  1", ""), ("   10004", "  1", "01-JAN-2020")]
        txt = "\r\n".join(_fixed(148, a=(9, c), b=(82, n), c=(44, rm)) for c, n, rm in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("platloc.DAT" if kind == "loc" else "platstru.DAT", txt)
    return buf.getvalue()

def fake_bytes(url, timeout=60):
    bump("bytes")
    if not STATE.get("boem_files"):
        return None                                  # default: BOEM files "down", ArcGIS fallback is used
    return boem_zip("loc" if "platloc" in url else "stru")

def install(ingest):
    ingest.get_json = fake_json
    ingest.get_text = fake_text
    ingest.get_bytes = fake_bytes
    ingest.USGS_KEY = "test-key"
    CALLS.clear()
