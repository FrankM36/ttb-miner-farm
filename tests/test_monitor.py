import importlib.util
import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("monitor", ROOT / "ttb_miner_farm_v5.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MinerFarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "farm_config.json"
        self.db = Path(self.tmp.name) / "hist.sqlite3"
        self.example = {
            "farm_name": "Custom farm name",
            "wallet": "",
            "miners": [
                {"id": "m01", "name": "Workshop display", "ip": "192.168.1.10", "expected_khs": 990}
            ]
        }
        self.cfg.write_text(json.dumps(self.example))

    def test_custom_names_and_live_api(self):
        farm = monitor.Farm(self.cfg, self.db)
        api = {"version": "V1.5.0", "state": "mining", "hashrate_khs": 990,
               "uptime_s": 400, "pool": {"subscribed": True}, "shares_accepted": 4,
               "wifi": {"rssi": -55}}
        with patch.object(monitor, "fetch_miner", return_value={"id": "m01", "kind": "api", "data": api}):
            farm.poll_once()
        data = farm.metrics()
        self.assertEqual(data["farm_name"], "Custom farm name")
        self.assertEqual(data["miners"][0]["name"], "Workshop display")
        self.assertAlmostEqual(data["farm_live_mhs"], 0.99)
        self.assertEqual(data["api_count"], 1)

    def test_outlier_not_recorded_as_valid_rate(self):
        farm = monitor.Farm(self.cfg, self.db)
        api = {"version": "V1.5.0", "state": "mining", "hashrate_khs": 75_000_000,
               "uptime_s": 400, "pool": {"subscribed": True}}
        with patch.object(monitor, "fetch_miner", return_value={"id": "m01", "kind": "api", "data": api}):
            farm.poll_once()
        last = sqlite3.connect(self.db).execute("SELECT valid_count,live_khs FROM farm_samples ORDER BY ts DESC LIMIT 1").fetchone()
        self.assertEqual(last, (0, 0))
        self.assertIsNone(farm.metrics()["miners"][0]["hashrate_khs"])

    def test_old_extreme_point_preserved_but_not_graphed(self):
        farm = monitor.Farm(self.cfg, self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO farm_samples(ts,miner_count,api_count,live_khs) VALUES(?,?,?,?)",
                       (time.time() - 10, 1, 1, 75000000))
        item = farm.history(1)[-1]
        self.assertIsNone(item["mhs"])
        self.assertTrue(item["excluded"])

    def test_reject_invalid_settings(self):
        for invalid in [-1, float('inf'), float('nan')]:
            bad = json.loads(json.dumps(self.example))
            bad['miners'][0]['expected_khs'] = invalid
            with self.assertRaises(ValueError):
                monitor.valid_config(bad)

    def test_display_name_does_not_change_history_id(self):
        farm = monitor.Farm(self.cfg, self.db)
        cfg = json.loads(self.cfg.read_text())
        cfg['miners'][0]['name'] = 'Renamed ESP32'
        self.cfg.write_text(json.dumps(cfg))
        self.assertTrue(farm.read_config(force=True))
        self.assertEqual(farm.config['miners'][0]['id'], 'm01')
        self.assertEqual(farm.config['miners'][0]['name'], 'Renamed ESP32')


if __name__ == '__main__':
    unittest.main()
