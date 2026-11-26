"""Offline tests for ingest.py. Every network call is answered by tests/fakes.py.

Run with:  python -m unittest discover -s tests -v
"""
import json, os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ingest   # noqa: E402
import fakes    # noqa: E402


class Conversions(unittest.TestCase):
    def test_salinity_of_seawater(self):
        # Standard seawater: 42.914 mS/cm at 15 C is 35 PSU
        self.assertAlmostEqual(ingest.psu(42.914, 15.0), 35.0, places=1)

    def test_fresh_water_is_zero(self):
        self.assertLess(ingest.psu(0.2, 20.0), 0.2)

    def test_visibility_drops_as_water_gets_murkier(self):
        clear, stained, muddy = (ingest.vis_from_kd490(k) for k in (0.1, 0.8, 3.0))
        self.assertGreater(clear, stained)
        self.assertGreater(stained, muddy)
        self.assertGreater(ingest.vis_from_turbidity(2), ingest.vis_from_turbidity(50))

    def test_clarity_labels_follow_visibility(self):
        self.assertEqual(ingest.clarity_from_vis(6), "Clear")
        self.assertEqual(ingest.clarity_from_vis(2), "Stained")
        self.assertEqual(ingest.clarity_from_vis(0.5), "Muddy")

    def test_tide_stage(self):
        t0 = fakes.now
        pts = [(t0 - ingest.timedelta(hours=3), 0.1, "L"), (t0 + ingest.timedelta(hours=3), 1.2, "H")]
        self.assertEqual(ingest.stage_at(pts, t0)[0], "Incoming")
        pts = [(t0 - ingest.timedelta(hours=3), 1.2, "H"), (t0 + ingest.timedelta(hours=3), 0.1, "L")]
        self.assertEqual(ingest.stage_at(pts, t0)[0], "Outgoing")


class FullRun(unittest.TestCase):
    """Runs the whole pipeline once against canned data and checks what the page will read."""

    @classmethod
    def setUpClass(cls):
        cls.cwd = os.getcwd()
        cls.tmp = tempfile.mkdtemp()
        os.chdir(cls.tmp)
        fakes.install(ingest)
        ingest._KD_FAILS.clear()
        ingest.run_once()
        with open("latest.json") as f:
            cls.latest = json.load(f)
        with open("forecast.json") as f:
            cls.forecast = json.load(f)
        cls.spots = {s["id"]: s for s in cls.latest["spots"]}

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_spot_is_written(self):
        self.assertEqual(len(self.latest["spots"]), len(ingest.SPOTS))

    def test_output_passes_validation(self):
        self.assertEqual(ingest.validate("latest.json"), [])

    def test_core_readings_present(self):
        c = self.spots["barat"]["conditions"]
        for k in ("temp", "wind", "pres", "tide", "sal", "clar", "vis"):
            self.assertIsNotNone(c[k], k)

    def test_usgs_uses_new_api_with_key(self):
        self.assertIn("wd:continuous", fakes.CALLS)
        self.assertNotIn("legacy-usgs", fakes.CALLS)
        self.assertTrue(fakes.STATE["usgs_key_sent"])
        self.assertEqual(self.spots["atch"]["quality"]["sources"]["temp_f"]["source"], "usgs:07381515")

    def test_rivers_and_spillway_watch(self):
        r = self.latest["rivers"]
        self.assertEqual(r["miss"]["lid"], "NORL1")
        self.assertTrue(r["miss"]["spillway_watch"])
        self.assertIn("upstream", r["atch"])
        self.assertEqual(self.spots["pont"]["conditions"]["riv"]["trend"], "Rising")
        self.assertIsNone(self.spots["barat"]["conditions"]["riv"])

    def test_water_level_vs_tide_table(self):
        self.assertAlmostEqual(self.spots["barat"]["conditions"]["wl_anom"], 0.65, places=2)
        self.assertIn("wl_anom", self.spots["atch"]["quality"]["not_applicable"])

    def test_offshore_depths(self):
        self.assertEqual(self.spots["midlump"]["depth_ft"], 206)
        self.assertEqual(self.spots["midlump"]["depth_src"], "published")
        self.assertEqual(self.spots["bullwinkle"]["depth_src"], "NOAA NCEI bathymetry")
        self.assertIsNone(self.spots["barat"]["depth_ft"])

    def test_tides_and_forecast(self):
        self.assertTrue(self.spots["barat"]["tides"])
        f = self.forecast["spots"]["barat"]
        self.assertEqual(len(f["wind"]), 168)

    def test_wind_and_waves_forecast(self):
        f = self.forecast["spots"]["barat"]
        self.assertEqual(len(f["gust"]), 168)                # Open-Meteo gusts and direction for every spot
        self.assertEqual(len(f["wdir"]), 168)
        sea = f["sea"]                                       # NWS grid: wind, gusts, direction, waves
        self.assertEqual(sea["src"], "nws")
        self.assertEqual(sea["office"], "LIX")
        for k in ("wind", "gust", "dir", "wave", "per"):
            self.assertEqual(len(sea[k]), 168, k)
        self.assertEqual(sea["wind"][0], 12)                 # 20 km/h -> 12 mph, from a span that began earlier
        self.assertEqual(sea["wind"][3], 19)                 # 30 km/h -> 19 mph
        self.assertEqual(sea["gust"][0], 25)
        self.assertEqual(sea["dir"][0], 45)
        self.assertEqual(sea["wave"][0], 2.0)                # 0.6096 m -> 2.0 ft
        self.assertIsNone(sea["wave"][15])                   # the gap stays empty
        self.assertEqual(sea["wave"][30], 4.0)
        self.assertIsNone(sea["wave"][120])                  # past the last span

    def test_no_nws_grid_far_offshore(self):
        f = self.forecast["spots"]["off_deep"]
        self.assertNotIn("sea", f)                           # beyond the NWS grid: model forecast only
        self.assertEqual(len(f["wdir"]), 168)
        self.assertTrue(any(v is not None for v in f["wave"]))
        with open(ingest.STATION_CACHE) as fh:
            disk = json.load(fh)
        self.assertEqual(disk.get("ngrid|off_deep"), [])     # remembered, so it isn't asked again
        self.assertEqual(disk.get("ngrid|barat"), ["LIX", 70, 60])

    def test_grid_time_spans(self):
        self.assertEqual(ingest.iso_span("2026-11-19T09:00:00+00:00/PT3H")[1], 3)
        self.assertEqual(ingest.iso_span("2026-11-19T09:00:00+00:00/P1DT6H")[1], 30)
        self.assertEqual(ingest.iso_span("2026-11-19T09:00:00+00:00/P2D")[1], 48)
        self.assertIsNone(ingest.iso_span("nonsense"))
        start = ingest.parse_iso("2026-11-19T09:00:00+00:00")
        h = ingest.grid_hourly({"uom": "wmoUnit:furlongs", "values": [{"validTime": "2026-11-19T09:00:00+00:00/PT3H", "value": 1}]},
                               start, ingest._LEN_TO_FT, hours=6)
        self.assertEqual(h, [None] * 6)                      # unknown unit: no values rather than wrong ones

    def test_visibility_methods(self):
        self.assertEqual(self.spots["atch"]["quality"]["vis_method"], "turbidity gauge")
        self.assertTrue(self.spots["barat"]["quality"]["vis_method"].startswith("satellite"))

    def test_history_written(self):
        with open("history.json") as f:
            h = json.load(f)
        self.assertEqual(len(h["t"]), 1)
        self.assertEqual(len(h["spots"]["barat"]["temp"]), 1)
        self.assertIn("miss", h["rivers"])

    def test_sargassum_weedline(self):
        with open("ocean.json") as f:
            o = json.load(f)
        sg = o["sargassum"]
        self.assertTrue(sg["patches"])
        self.assertTrue(all(27.3 < la < 27.9 and -91.6 < lo < -89.4 for la, lo, n in sg["patches"]))
        self.assertLessEqual(self.spots["off90"]["conditions"]["weed"], 20)          # 27.8 N, about 14 mi from the line
        self.assertGreater(self.spots["off_west40"]["conditions"]["weed"], 50)
        self.assertIsNone(self.spots["barat"]["conditions"]["weed"])                 # inshore
        self.assertIn("weed", self.spots["barat"]["quality"]["not_applicable"])

    def test_fronts_field(self):
        self.assertEqual(self.spots["barat"]["fronts"], [])        # canned weather has a steady south wind


    def test_ocean_lines(self):
        with open("ocean.json") as f:
            o = json.load(f)
        self.assertTrue(o["sst_breaks"]["lines"])
        self.assertTrue(o["ssh"]["warm"])
        self.assertIn("0.3", o["color_lines"]["levels"])

    def test_rigs_and_reefs(self):
        with open("rigs.json") as f:
            rigs = json.load(f)["rigs"]
        self.assertEqual(len(rigs), 16)                    # 4 of 20 have a removal date
        with open("reefs.json") as f:
            self.assertEqual(json.load(f)["reefs"][0][2], "South Timbalier 128")

    def test_boem_platform_files(self):
        fakes.STATE["boem_files"] = True
        try:
            pts = ingest.boem_platforms((-94.5, 26.4, -87.5, 30.6))
        finally:
            fakes.STATE["boem_files"] = False
        # 10003 is outside the box and 10004 has a removal date
        self.assertEqual(pts, [[28.75, -90.25, "ST 128 A"], [28.2, -91.6, "EI 330 B"]])

    def test_marine_forecast(self):
        m = self.latest["marine"]["GMZ550"]
        self.assertEqual(m["periods"][0]["name"], "Today")
        self.assertIn("Northeast winds", m["periods"][0]["text"])
        self.assertEqual(self.spots["barat"]["mzone"], "GMZ550")
        self.assertEqual(self.spots["off90"]["mzone"], "GMZ089")

    def test_phone_alerts_dry_run_and_no_repeats(self):
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node not installed")
        with open("alerts.json", "w") as f:
            json.dump({"rules": [{"species": "redfish", "spots": ["barat", "cocodrie"], "min_score": 0},
                                 {"species": "redfish", "spots": ["barat"], "min_score": 101}]}, f)
        env = dict(os.environ, ALERTS_DIR=os.getcwd(), ALERTS_RECORD_DRY="1")
        env.pop("NTFY_TOPIC", None)
        script = os.path.join(os.path.dirname(HERE), "scripts", "alerts.js")
        out1 = subprocess.run(["node", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out1.returncode, 0, out1.stderr)
        self.assertEqual(out1.stdout.count("[dry run] Redfish at"), 2, out1.stdout)
        out2 = subprocess.run(["node", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("[dry run]", out2.stdout)                    # same day: not sent again
        with open("alerts_state.json") as f:
            self.assertEqual(len(json.load(f)["sent"]), 2)

    def test_tropical_storm_threat(self):
        with open("tropical.json") as f:
            t = json.load(f)
        self.assertEqual(len(t["storms"]), 1)
        st = t["storms"][0]
        self.assertEqual(st["name"], "Tester")
        self.assertEqual(st["type"], "Hurricane")
        self.assertEqual(st["level"], "watch")             # TS watch segment on the Louisiana coast
        self.assertTrue(st["cone"] and st["track"] and st["ww"])
        self.assertLess(st["closest_mi"], 60)
        self.assertEqual(t["outlook"][0]["prob7"], "30%")
        self.assertEqual(self.latest["tropical"]["level"], "watch")
        self.assertEqual(self.latest["tropical"]["rank"], 5)
        self.assertIn("Tester", self.latest["tropical"]["text"])

    def test_safety_alerts(self):
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node not installed")
        d = tempfile.mkdtemp(dir=os.getcwd())
        for fn in ("latest.json", "forecast.json"):
            shutil.copy(fn, d)
        with open(os.path.join(d, "alerts.json"), "w") as f:
            json.dump({"safety": {"enabled": True, "spots": ["barat", "off90"], "nws": True, "wind_mph": 10,
                                  "tropical": "near"}, "rules": []}, f)
        env = dict(os.environ, ALERTS_DIR=d, ALERTS_RECORD_DRY="1")
        env.pop("NTFY_TOPIC", None)
        script = os.path.join(os.path.dirname(HERE), "scripts", "alerts.js")
        out = subprocess.run(["node", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Small Craft Advisory in effect for", out.stdout)
        self.assertIn("Wind over 10 mph coming", out.stdout)
        self.assertIn("Tropical: watch", out.stdout)
        self.assertEqual(out.stdout.count("[dry run]"), 3, out.stdout)
        again = subprocess.run(["node", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("[dry run]", again.stdout)                 # each is sent once

    def test_health_report_survives_a_missing_buoy(self):
        saved = ingest.buoy_obs, ingest.map_checks
        ingest.buoy_obs, ingest.map_checks = (lambda sid: None), (lambda: [])
        try:
            fails = ingest.health("health.md")
        finally:
            ingest.buoy_obs, ingest.map_checks = saved
        with open("health.md") as f:
            rep = f.read()
        self.assertIn("| NDBC buoy 42001 | **FAIL** | no data |", rep)
        self.assertIn("| NHC tropical storms | OK |", rep)
        self.assertIn("| Artificial reefs | OK |", rep)
        self.assertIn("| NWS radar map | OK |", rep)
        self.assertNotIn("Beach water quality", rep)
        self.assertGreaterEqual(fails, 1)

    def test_tidal_currents(self):
        c = self.spots["barat"]["current"]
        self.assertEqual(c["station"], "ACT0001")
        self.assertIn(c["dir"], ("Flood", "Ebb", "Slack"))
        self.assertLessEqual(abs(c["now"]), 2.2)
        self.assertTrue(any(e["type"] == "slack" for e in c["events"]))
        self.assertEqual(self.spots["barat"]["conditions"]["tcur"], round(abs(c["now"]), 1))
        self.assertEqual(len(self.forecast["spots"]["barat"]["tcur"]), 168)
        self.assertIsNone(self.spots["calc"]["current"])                 # no station within 12 mi
        self.assertIn("tcur", self.spots["calc"]["quality"]["not_applicable"])
        with open("stations_cache.json") as f:
            sc = json.load(f)
        self.assertEqual(sc["cstations"][0][0], "ACT0001")                 # kept for next run
        self.assertEqual(sc["cstn|calc"], [])                              # "none nearby" remembered too

    def test_diversions(self):
        dv = self.latest["diversions"]
        self.assertEqual(dv["bonnet"]["status"], "Open")
        self.assertEqual(dv["davis"]["status"], "Flowing")

    def test_dead_zone(self):
        with open("deadzone.json") as f:
            dz = json.load(f)
        self.assertEqual(dz["year"], fakes.now.year)                     # newest year's layer chosen
        self.assertTrue(dz["areas"])
        july = fakes.now.replace(month=7, day=15)
        off40 = next(s for s in ingest.SPOTS if s["id"] == "off40")
        off90 = next(s for s in ingest.SPOTS if s["id"] == "off90")
        self.assertEqual(ingest.hypoxia_flag(off40, dz, july), "Low")
        self.assertEqual(ingest.hypoxia_flag(off90, dz, july), "OK")
        self.assertIsNone(ingest.hypoxia_flag(off40, dz, july.replace(month=12)))   # out of season
        self.assertIsNone(ingest.hypoxia_flag(off40, dict(dz, year=2019), july))   # old survey

    def test_stations_without_data_are_skipped_next_time(self):
        with open("stations_cache.json") as f:
            self.assertIn("kvaf", json.load(f).get("deadbuoys", []))
        self.assertEqual(fakes.CALLS.get("ndbc-404"), 1)          # asked once, then remembered

    def test_status_file(self):
        with open("status.json") as f:
            st = json.load(f)
        self.assertEqual(st["spots_failed"], [])


class Tropical(unittest.TestCase):
    def test_service_down_keeps_last_file(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "tropical.json")
        fakes.install(ingest)
        self.assertIsNotNone(ingest.tropical(fakes.now, path))
        saved = ingest.get_json
        ingest.get_json = lambda *a, **k: None
        try:
            self.assertIsNone(ingest.tropical(fakes.now, path))
            prev = ingest.previous_tropical(fakes.now, path)
        finally:
            ingest.get_json = saved
        self.assertTrue(prev["stale"])
        self.assertEqual(prev["level"], "watch")

    def test_quiet_season(self):
        tmp = tempfile.mkdtemp()
        fakes.install(ingest)
        fakes.STATE["storm"] = False
        try:
            t = ingest.tropical(fakes.now, os.path.join(tmp, "t.json"))
        finally:
            fakes.STATE["storm"] = True
        self.assertEqual(t["level"], "none")
        self.assertEqual(t["storms"], [])

    def test_point_in_cone(self):
        ring = [[28, -91], [28, -89], [30, -89], [30, -91]]
        self.assertTrue(ingest._inside(29, -90, ring))
        self.assertFalse(ingest._inside(31, -90, ring))


class Fronts(unittest.TestCase):
    def series(self, front_at=None, hours=216, drop=14, rise=6):
        t0 = fakes.now.replace(minute=0) - ingest.timedelta(hours=48)
        t = [t0 + ingest.timedelta(hours=h) for h in range(hours)]
        s = {"t": t, "pres": [], "air": [], "wdir": [], "wind": []}
        for h in range(hours):
            after = front_at is not None and h >= front_at
            daily = 6 * __import__("math").sin(h / 24 * 6.283)          # normal day-night swing
            s["air"].append((72 - drop if after else 72) + daily)
            s["pres"].append(1012 + (rise * min(1, (h - front_at) / 12) if after else 0))
            s["wdir"].append(340 if after else 190)
            s["wind"].append(20 if after and h < front_at + 18 else 10)
        return s

    def test_finds_a_front_ahead(self):
        fr = ingest.find_fronts(self.series(front_at=120), fakes.now)
        self.assertEqual(len(fr), 1)
        t = ingest.parse_iso(fr[0]["t"])
        self.assertAlmostEqual((t - fakes.now.replace(minute=0)).total_seconds() / 3600, 72, delta=2)
        self.assertGreaterEqual(fr[0]["drop_f"], 10)
        self.assertEqual(fr[0]["nwind"], 20)

    def test_day_night_swing_and_sea_breeze_are_not_fronts(self):
        s = self.series()
        s["wdir"] = [190 if (h % 24) < 12 else 20 for h in range(len(s["t"]))]   # land/sea breeze flips
        self.assertEqual(ingest.find_fronts(s, fakes.now), [])

    def test_weak_shift_without_cooling_is_ignored(self):
        self.assertEqual(ingest.find_fronts(self.series(front_at=120, drop=2), fakes.now), [])


class Sargassum(unittest.TestCase):
    def test_land_and_cloud_edges_are_skipped(self):
        lats = [27 + 0.03 * i for i in range(40)]
        lons = [-91 + 0.03 * j for j in range(40)]
        g = [[0.00005] * 40 for _ in lats]
        for j in range(40):
            g[0][j] = None                                  # land along the top edge
            g[3][j] = 0.002                                 # bright, but right next to land
            g[20][j] = 0.002                                # a real weedline offshore
        p = ingest.sargassum_patches(lats, lons, g)
        self.assertTrue(p)
        self.assertTrue(all(abs(la - 27.65) < 0.1 for la, lo, n in p))

    def test_old_image_is_not_scored(self):
        spot = {"marine": True, "lat": 28, "lon": -90}
        self.assertIsNone(ingest.weed_near(spot, {"age_days": 20, "patches": [[28, -90, 3]]}, fakes.now))
        self.assertEqual(ingest.weed_near(spot, {"age_days": 3, "patches": [[28, -90, 3]]}, fakes.now), 0)


class History(unittest.TestCase):
    def test_appends_pads_and_trims(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "h.json")
        t0 = fakes.now - ingest.timedelta(days=20)
        spot = lambda sid, temp: {"id": sid, "conditions": {"temp": temp, "sal": 10, "wind": 5, "vis": 2.0, "wl_anom": None}}
        ingest.update_history([spot("a", 60)], {}, t0, path)                     # old, should be trimmed
        ingest.update_history([spot("a", 61)], {"miss": {"stage": 5.0}}, fakes.now - ingest.timedelta(hours=1), path)
        h = ingest.update_history([spot("a", 62), spot("b", 70)], {"miss": {"stage": 5.2}}, fakes.now, path)
        self.assertEqual(len(h["t"]), 2)
        self.assertEqual(h["spots"]["a"]["temp"], [61, 62])
        self.assertEqual(h["spots"]["b"]["temp"], [None, 70])                   # new spot padded
        self.assertEqual(h["rivers"]["miss"]["stage"], [5.0, 5.2])
        shutil.rmtree(tmp, ignore_errors=True)


class DeadSources(unittest.TestCase):
    """A reading missing at every spot (a source that's down or blocked) must be noticed."""
    def spot(self, marine, **cond):
        na = ["chl", "kd490"] + (["sal"] if marine else ["sbreak", "ssat"])
        return {"conditions": cond, "quality": {"not_applicable": na}}

    def test_satellite_missing_everywhere_is_reported(self):
        spots = [self.spot(True, sbreak=None, ssat=None, sal=None, temp=70, wind=5, tide="Incoming") for _ in range(4)]
        spots += [self.spot(False, chl=None, kd490=None, sal=12, temp=68, wind=8, tide="Outgoing") for _ in range(4)]
        dead = " ".join(ingest.dead_sources(spots))
        for k in ("chl", "kd490", "sbreak", "ssat"):
            self.assertIn(f"({k})", dead)
        self.assertNotIn("(sal)", dead)      # offshore spots don't use salinity; inshore ones have it

    def test_one_reading_anywhere_is_enough(self):
        spots = [self.spot(True, sbreak=None, ssat=None) for _ in range(4)] + [self.spot(True, sbreak=0.2, ssat=75.0)]
        self.assertEqual([d for d in ingest.dead_sources(spots) if "sbreak" in d or "ssat" in d], [])

    def test_deploy_only_warns_but_health_check_fails(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "latest.json")
            spots = [self.spot(True, sbreak=None, ssat=None, temp=70, wind=5, tide="Incoming") for _ in range(20)]
            for s in spots:
                s["quality"]["completeness"] = 0.9
            with open(path, "w") as f:
                json.dump({"generated": ingest.datetime.now(ingest.UTC).isoformat(), "spots": spots}, f)
            self.assertEqual(ingest.validate(path, min_spots=15), [])
            self.assertTrue(any("sbreak" in p for p in ingest.validate(path, min_spots=15, strict=True)))
        finally:
            shutil.rmtree(d)


class NetworkTrouble(unittest.TestCase):
    """A blip must not be remembered as "nothing here", and a dead host must not eat the run."""
    def setUp(self):
        # A separate copy of ingest.py: the other tests swap in fake network functions on the shared one
        import importlib.util
        spec = importlib.util.spec_from_file_location("ingest_net", os.path.join(os.path.dirname(HERE), "ingest.py"))
        self.ing = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.ing)

    def test_failing_host_is_skipped_after_a_few_calls(self):
        ing = self.ing
        calls = []
        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        ing.urlopen, sleep = boom, ing.time.sleep
        ing.time.sleep = lambda s: None
        try:
            for _ in range(10):
                self.assertIsNone(ing.get_text("https://slow.example/x"))
            self.assertEqual(len(calls), ing.HOST_GIVE_UP)         # then no more attempts this run
            self.assertIsNone(ing.get_json("https://slow.example/y"))
            self.assertEqual(len(calls), ing.HOST_GIVE_UP)
            self.assertIsNone(ing.get_text("https://other.example/x"))
            self.assertEqual(len(calls), ing.HOST_GIVE_UP + 1)     # other hosts are still tried
        finally:
            ing.time.sleep = sleep

    def test_buoy_blip_is_not_a_dead_buoy(self):
        ing = self.ing
        def boom(*a, **k):
            raise OSError("timeout")
        ing.urlopen = boom
        self.assertEqual(ing.buoy_obs("42001"), {})            # not None: don't mark it dead
        def gone(req, *a, **k):
            raise ing.HTTPError(req.full_url, 404, "Not Found", {}, None)
        ing.urlopen = gone
        self.assertIsNone(ing.buoy_obs("kxyz"))                  # a real 404: no realtime file

    def test_empty_station_list_is_fetched_again(self):
        ing = self.ing
        cache = {"buoys": []}
        real = ing.buoy_list
        ing.buoy_list = lambda: [("42001", 25.9, -89.7)]
        try:
            ing.near_buoys({"lat": 26.0, "lon": -89.7}, cache)
            self.assertEqual(cache["buoys"], [("42001", 25.9, -89.7)])
        finally:
            ing.buoy_list = real

    def test_old_dead_buoys_get_another_chance(self):
        ing = self.ing
        cache = {"buoys": [("42001", 25.9, -89.7)], "deadbuoys": ["42001"],
                 "deadbuoys_t": (ing.datetime.now(ing.UTC) - ing.timedelta(days=8)).isoformat()}
        self.assertEqual([b[0] for b in ing.near_buoys({"lat": 26.0, "lon": -89.7}, cache)], ["42001"])
        self.assertEqual(cache["deadbuoys"], [])


class StricterCheck(unittest.TestCase):
    def write(self, d, spots=80, hist=None, prev=None, temp=70, fc=80):
        sp = [{"id": f"s{i}", "conditions": {"temp": temp, "wind": 5}, "quality": {"completeness": 0.9}} for i in range(spots)]
        with open(os.path.join(d, "latest.json"), "w") as f:
            json.dump({"generated": ingest.datetime.now(ingest.UTC).isoformat(), "spots": sp}, f)
        with open(os.path.join(d, "forecast.json"), "w") as f:
            json.dump({"spots": {f"s{i}": {} for i in range(fc)}}, f)
        if hist is not None:
            with open(os.path.join(d, "history.json"), "w") as f:
                json.dump({"t": list(range(hist))}, f)
        if prev is not None:
            with open(os.path.join(d, "history.json.prev"), "w") as f:
                json.dump({"t": list(range(prev))}, f)
        return os.path.join(d, "latest.json")

    def check(self, **kw):
        d = tempfile.mkdtemp()
        try:
            return ingest.validate(self.write(d, **kw))
        finally:
            shutil.rmtree(d)

    def test_a_normal_run_passes(self):
        self.assertEqual(self.check(hist=331, prev=330), [])

    def test_too_few_spots(self):
        self.assertTrue(any("spots" in p for p in self.check(spots=40, fc=40)))

    def test_forecast_missing_for_most_spots(self):
        self.assertTrue(any("forecast.json" in p for p in self.check(fc=10)))

    def test_history_wiped(self):
        self.assertTrue(any("shrank" in p for p in self.check(hist=1, prev=330)))

    def test_broken_sensors(self):
        self.assertTrue(any("out of range" in p for p in self.check(temp=212)))


class BoatLaunches(unittest.TestCase):
    SAMPLE = {"elements": [
        {"type": "node", "lat": 29.2512, "lon": -90.2101, "tags": {"leisure": "slipway", "name": "Leeville public launch", "fee": "no"}},
        {"type": "way", "center": {"lat": 29.25121, "lon": -90.21012}, "tags": {"leisure": "slipway", "name": "Leeville public launch"}},
        {"type": "node", "lat": 29.30, "lon": -90.30, "tags": {"leisure": "slipway", "access": "private", "name": "Camp ramp"}},
        {"type": "node", "lat": 29.66, "lon": -90.11, "tags": {"leisure": "slipway", "operator": "Jefferson Parish", "fee": "yes"}},
        {"type": "node", "tags": {"leisure": "slipway"}}]}

    def test_public_ramps_only_and_no_duplicates(self):
        ramps = ingest.parse_launches(self.SAMPLE)
        self.assertEqual([r[2] for r in ramps], ["Leeville public launch", "Jefferson Parish"])
        self.assertEqual(ramps[0][3], "free")
        self.assertEqual(ramps[1][3], "fee")

    def test_nearest_launch_per_inshore_spot(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "launches.json")
            with open(path, "w") as f:
                json.dump({"launches": ingest.parse_launches(self.SAMPLE)}, f)
            spots = [{"id": "leeville", "lat": 29.25, "lon": -90.21, "zone": "inshore"},
                     {"id": "off40", "lat": 28.5, "lon": -90.6, "zone": "offshore"},
                     {"id": "toledo", "lat": 31.4, "lon": -93.7, "zone": "inshore"}]
            ingest.nearest_launches(spots, path)
            self.assertEqual(spots[0]["launch"]["name"], "Leeville public launch")
            self.assertLess(spots[0]["launch"]["mi"], 1)
            self.assertNotIn("launch", spots[1])          # offshore spots don't get one
            self.assertNotIn("launch", spots[2])          # nothing within 15 miles
        finally:
            shutil.rmtree(d)

    def test_coast_guard_and_military_ramps_are_left_out(self):
        j = {"elements": [{"type": "node", "lat": 29.26, "lon": -89.96, "tags": {"leisure": "slipway", "name": "U.S. Coast Guard - Station Grand Isle"}},
                          {"type": "node", "lat": 29.27, "lon": -89.97, "tags": {"leisure": "slipway", "operator": "US Navy"}},
                          {"type": "node", "lat": 29.28, "lon": -89.98, "tags": {"leisure": "slipway", "name": "Venice Marina Boat Launch"}}]}
        self.assertEqual([r[2] for r in ingest.parse_launches(j)], ["Venice Marina Boat Launch"])

    def test_named_ramp_preferred_when_nearly_as_close(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "launches.json")
            with open(path, "w") as f:   # an older weekly file can still hold a Coast Guard ramp
                json.dump({"launches": [[29.250, -90.210, "Coast Guard", ""], [29.255, -90.210, "Boat launch", ""],
                                        [29.270, -90.210, "Bayou Lafourche public launch", "free"]]}, f)
            spots = [{"id": "leeville", "lat": 29.25, "lon": -90.21, "zone": "inshore"}]
            ingest.nearest_launches(spots, path)
            self.assertEqual(spots[0]["launch"]["name"], "Bayou Lafourche public launch")
        finally:
            shutil.rmtree(d)

    def test_failed_download_waits_before_retrying(self):
        d = tempfile.mkdtemp()
        calls = []
        real = ingest.urlopen
        def boom(*a, **k):
            calls.append(1); raise OSError("busy")
        try:
            ingest.urlopen = boom
            cache, path, now = {}, os.path.join(d, "launches.json"), ingest.datetime.now(ingest.UTC)
            ingest._launches(now, path, lambda p: True, cache)
            first = len(calls)
            self.assertEqual(first, len(ingest.OVERPASS_URLS))       # every mirror tried once
            ingest._launches(now + ingest.timedelta(hours=1), path, lambda p: True, cache)
            self.assertEqual(len(calls), first)                       # an hour later: no new attempt
            ingest._launches(now + ingest.timedelta(hours=7), path, lambda p: True, cache)
            self.assertEqual(len(calls), 2 * first)                   # after six hours: tries again
            self.assertFalse(os.path.exists(path))                    # nothing written from failures
            saved = os.path.join(d, "c.json")
            real_path, ingest.STATION_CACHE = ingest.STATION_CACHE, saved
            try:
                ingest.save_station_cache(cache)
                self.assertIn("launches_tried", ingest.load_station_cache())   # remembered between runs
            finally:
                ingest.STATION_CACHE = real_path
        finally:
            ingest.urlopen = real
            shutil.rmtree(d)

    def test_missing_file_is_fine(self):
        spots = [{"id": "x", "lat": 29.0, "lon": -90.0, "zone": "inshore"}]
        ingest.nearest_launches(spots, "/nonexistent/launches.json")
        self.assertNotIn("launch", spots[0])


if __name__ == "__main__":
    unittest.main()
