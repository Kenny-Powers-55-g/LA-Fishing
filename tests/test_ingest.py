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

    def test_status_file(self):
        with open("status.json") as f:
            st = json.load(f)
        self.assertEqual(st["spots_failed"], [])


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
