import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import duckdb
from websockets.asyncio.client import connect

from test_ais_live import free_port
from test_tide_consumer import NOW, item
from tide_consumer import parse_reading, store_reading
from tide_live import latest_tide, serve_live_tide


class TestLiveTide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.levels_dir = Path(self.tmp.name) / "levels"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, event_date, value, series="wlp"):
        store_reading(parse_reading(item(event_date, value), series, NOW), self.levels_dir)

    def test_latest_tide(self):
        con = duckdb.connect()
        self.assertIsNone(latest_tide(con, self.levels_dir))
        self.store("2026-10-21T23:55:00Z", 1.0)
        self.store("2026-10-22T00:00:00Z", 1.1)
        self.store("2026-10-21T23:50:00Z", 0.9)
        self.assertEqual(latest_tide(con, self.levels_dir), {
            "station": "00490", "ts": "2026-10-22T00:00:00Z", "date": "2026-10-22",
            "time": "00:00:00", "tide_level": 1.1, "series": "wlp", "qc_flag": "2",
            "trend": "rising", "change": 0.1, "previous_ts": "2026-10-21T23:55:00Z"})

    def test_trend(self):
        con = duckdb.connect()
        self.store("2026-10-21T19:00:00Z", 1.40)
        self.assertEqual([latest_tide(con, self.levels_dir)[k] for k in ("trend", "change")],
                         [None, None])                            # first reading
        self.store("2026-10-21T19:05:00Z", 1.37)
        self.assertEqual(latest_tide(con, self.levels_dir)["trend"], "falling")
        self.store("2026-10-21T19:10:00Z", 1.373)
        self.assertEqual(latest_tide(con, self.levels_dir)["trend"], "steady")
        self.store("2026-10-21T19:15:00Z", 1.30, "wlo")        # observed after predicted
        self.assertIsNone(latest_tide(con, self.levels_dir)["trend"])
        self.store("2026-10-21T20:00:00Z", 1.20, "wlo")        # 45 min gap
        self.assertIsNone(latest_tide(con, self.levels_dir)["trend"])

    def test_browser_gets_current_then_updates(self):
        self.store("2026-10-21T19:20:00Z", 1.33)
        port = free_port()

        async def run():
            server = asyncio.create_task(serve_live_tide(
                "localhost", port, self.levels_dir, poll_seconds=0.05))
            await asyncio.sleep(0.3)
            try:
                async with connect(f"ws://localhost:{port}") as ws:
                    current = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    self.store("2026-10-21T19:25:00Z", 1.38, "wlo")
                    update = json.loads(await asyncio.wait_for(ws.recv(), 2))
            finally:
                server.cancel()
            return current, update

        current, update = asyncio.run(run())
        self.assertEqual((current["ts"], current["tide_level"]), ("2026-10-21T19:20:00Z", 1.33))
        self.assertEqual((update["time"], update["tide_level"], update["series"]),
                         ("19:25:00", 1.38, "wlo"))


if __name__ == "__main__":
    unittest.main()
