import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from websockets.asyncio.client import connect

from storm_surge_consumer import RiskIndexStore, parse_feature
from storm_surge_live import serve_live_storm_surge, storm_surge_geojson
from test_ais_consumer import INSIDE
from test_ais_live import free_port
from test_storm_surge_consumer import NOW, risk_feature


class TestLiveStormSurge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "risk_index"
        self.latest_path = Path(self.tmp.name) / "latest.json"
        self.store = RiskIndexStore(out_dir=self.out_dir, latest_path=self.latest_path)
        self.con = duckdb.connect()
        self.con.execute("LOAD spatial")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, *features, fetched_at=NOW):
        self.store.save([parse_feature(f, fetched_at) for f in features], fetched_at)

    def geojson(self, now=NOW):
        return storm_surge_geojson(self.con, now, self.latest_path, self.out_dir)

    def test_storm_surge_features_only(self):
        self.assertIsNone(self.geojson())
        lon, lat = INSIDE
        self.save(risk_feature(lon, lat, id="surge"),
                  risk_feature(lon + 0.02, lat, id="tide only", storm_surge=False),
                  risk_feature(lon - 0.02, lat, id="over", expiration="2026-10-22T06:00:00Z"))
        collection = self.geojson()
        self.assertEqual(collection["fetched_at"], "2026-10-22T12:00:00Z")
        (feature,) = collection["features"]
        self.assertEqual(feature["geometry"]["type"], "Polygon")
        self.assertAlmostEqual(feature["geometry"]["coordinates"][0][0][0], lon - 0.0417)
        self.assertEqual(feature["properties"], {
            "id": "surge", "publication_datetime": "2026-10-21T22:00:00Z",
            "validity_datetime": "2026-10-22T12:00:00Z",
            "expiration_datetime": "2026-10-23T12:00:00Z",
            "risk": 2, "impact": 2, "likelihood": 3, "tide": True, "waves": False})
        self.assertEqual(self.geojson(datetime(2026, 10, 24, tzinfo=timezone.utc))["features"], [])

    def test_browser_gets_current_then_updates(self):
        # The server filters by the real clock, so the risk must not have expired yet
        self.save(risk_feature(*INSIDE, expiration="2099-01-01T00:00:00Z"))
        port = free_port()

        async def run():
            server = asyncio.create_task(serve_live_storm_surge(
                "localhost", port, self.latest_path, self.out_dir, poll_seconds=0.05))
            await asyncio.sleep(0.5)
            try:
                async with connect(f"ws://localhost:{port}") as ws:
                    current = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    self.save(fetched_at=datetime.now(timezone.utc))   # risk cleared
                    update = json.loads(await asyncio.wait_for(ws.recv(), 2))
            finally:
                server.cancel()
            return current, update

        current, update = asyncio.run(run())
        self.assertEqual([f["properties"]["id"] for f in current["features"]], ["33226bba"])
        self.assertEqual(update["features"], [])
        self.assertNotEqual(update["fetched_at"], current["fetched_at"])


if __name__ == "__main__":
    unittest.main()
