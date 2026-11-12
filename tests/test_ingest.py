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

    def test_visibility_methods(self):
        self.assertEqual(self.spots["atch"]["quality"]["vis_method"], "turbidity gauge")
        self.assertTrue(self.spots["barat"]["quality"]["vis_method"].startswith("satellite"))

    def test_history_written(self):
        with open("history.json") as f:
            h = json.load(f)
        self.assertEqual(len(h["t"]), 1)
        self.assertEqual(len(h["spots"]["barat"]["temp"]), 1)
        self.assertIn("miss", h["rivers"])

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
        self.assertIn("| NDBC buoy 42040 | **FAIL** | no data |", rep)
        self.assertIn("| NHC tropical storms | OK |", rep)
        self.assertIn("| Artificial reefs | OK |", rep)
        self.assertGreaterEqual(fails, 1)

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


if __name__ == "__main__":
    unittest.main()
