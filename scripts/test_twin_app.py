import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import duckdb
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from test_ais_live import free_port
from twin_app import (CHART_DATUM_CGVD2013, REMOTE, app_config, extent_bbox, replay_days,
                      serve_feeds, start_http)

TODAY = date(2026, 10, 23)


def write_analysis(analysis_dir, day, rows):
    """analysis.parquet for one day, rows of (mmsi, ship_name, berth_name, dwell_time, lon, lat)."""
    path = analysis_dir / f"date={day}" / "analysis.parquet"
    path.parent.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE a (mmsi BIGINT, ship_name VARCHAR, berth_name VARCHAR, "
                "dwell_time INTEGER, lon DOUBLE, lat DOUBLE)")
    con.executemany("INSERT INTO a VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute(f"COPY a TO '{path}' (FORMAT parquet)")


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.analysis_dir = Path(self.tmp.name) / "analysis_daily"
        self.vessels_path = Path(self.tmp.name) / "vessels.parquet"

    def tearDown(self):
        self.tmp.cleanup()

    def replay(self, days=7):
        return replay_days(TODAY, days, self.analysis_dir, self.vessels_path)

    def test_week_with_gaps(self):
        write_analysis(self.analysis_dir, "2026-10-22", [
            (2, "OBELIX", "Pier 42", 21, -63.56, 44.63), (1, "ASTERIX", "Pier 42", 3, -63.56, 44.63)])
        write_analysis(self.analysis_dir, "2026-10-17", [(3, None, "Fairview", 8, -63.61, 44.67)])
        write_analysis(self.analysis_dir, "2026-10-16", [(4, "TOO OLD", "Pier 9", 2, -63.6, 44.6)])
        days = self.replay()["days"]
        self.assertEqual([d["date"] for d in days],
                         [f"2026-10-{d}" for d in range(17, 24)])
        self.assertEqual([d["analysed"] for d in days], [True, False, False, False, False, True, False])
        self.assertEqual(days[5]["vessels"], [
            {"mmsi": 1, "ship_name": "ASTERIX", "ship_type": None, "berth_name": "Pier 42",
             "dwell_time": 3, "lon": -63.56, "lat": 44.63},
            {"mmsi": 2, "ship_name": "OBELIX", "ship_type": None, "berth_name": "Pier 42",
             "dwell_time": 21, "lon": -63.56, "lat": 44.63}])

    def test_ship_type_joined(self):
        write_analysis(self.analysis_dir, "2026-10-23", [(1, "ASTERIX", "Pier 42", 3, -63.56, 44.63)])
        duckdb.sql(f"COPY (SELECT 1::BIGINT AS mmsi, 52::SMALLINT AS ship_type) "
                   f"TO '{self.vessels_path}' (FORMAT parquet)")
        self.assertEqual(self.replay(1)["days"][0]["vessels"][0]["ship_type"], 52)

    def test_no_analysis_files(self):
        days = self.replay(3)["days"]
        self.assertEqual([(d["date"], d["analysed"], d["vessels"]) for d in days], [
            ("2026-10-21", False, []), ("2026-10-22", False, []), ("2026-10-23", False, [])])


class TestConfig(unittest.TestCase):
    def test_datums_and_extent(self):
        self.assertAlmostEqual(CHART_DATUM_CGVD2013, -1.41)
        west, south, east, north = extent_bbox()
        self.assertTrue(west < -63.57 < east and south < 44.65 < north)
        config = app_config([west, south, east, north])
        self.assertEqual(config["feed_port"], 8765)
        self.assertEqual(config["feeds"],
                         {"vessels": "/vessels", "tide": "/tide", "storm_surge": "/storm_surge"})
        self.assertEqual(config["station"], "00490")


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for folder, name, body in [("app", "index.html", "<!doctype html>twin"),
                                   ("terrain", "layer.json", '{"format": "quantized-mesh-1.0"}'),
                                   ("terrain/0/0", "0.terrain", "mesh"),
                                   ("buildings", "tileset.json", '{"asset": {}}')]:
            (root / folder).mkdir(parents=True, exist_ok=True)
            (root / folder / name).write_text(body)
        (root / "secret.txt").write_text("not served")
        self.port = free_port()
        self.server = start_http(
            "localhost", self.port, config={"extent": [0, 0, 1, 1]}, app_dir=root / "app",
            terrain_dir=root / "terrain", tileset_dir=root / "buildings", remote={},
            analysis_dir=root / "analysis_daily", vessels_path=root / "vessels.parquet")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://localhost:{self.port}{path}", timeout=2) as r:
                return r.status, r.headers["Content-Type"], r.read().decode()
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, None, None

    def test_routes(self):
        self.assertEqual(self.get("/")[2], "<!doctype html>twin")
        self.assertEqual(self.get("/terrain/layer.json?v=1.0.0")[1:],
                         ("application/json", '{"format": "quantized-mesh-1.0"}'))
        self.assertEqual(self.get("/terrain/0/0/0.terrain?v=1.0.0")[1],
                         "application/vnd.quantized-mesh")
        self.assertEqual(self.get("/buildings/tileset.json")[0], 200)
        self.assertEqual(json.loads(self.get("/api/config")[2]),
                         {"extent": [0, 0, 1, 1],
                          "layers": {"terrain": "/terrain/", "buildings": "/buildings/"}})
        self.assertEqual(len(json.loads(self.get("/api/replay?days=3")[2])["days"]), 3)

    def test_config_reports_missing_layers(self):
        root = Path(self.tmp.name)
        (root / "buildings" / "tileset.json").unlink()
        self.assertEqual(json.loads(self.get("/api/config")[2])["layers"],
                         {"terrain": "/terrain/", "buildings": None})
        (root / "terrain" / "layer.json").unlink()
        self.assertEqual(json.loads(self.get("/api/config")[2])["layers"],
                         {"terrain": None, "buildings": None})

    def test_config_points_at_s3_when_not_built(self):
        root = Path(self.tmp.name)
        (root / "buildings" / "tileset.json").unlink()
        port = free_port()
        server = start_http("localhost", port, config={}, app_dir=root / "app",
                            terrain_dir=root / "terrain", tileset_dir=root / "buildings",
                            analysis_dir=root / "analysis_daily",
                            vessels_path=root / "vessels.parquet")
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/api/config", timeout=2) as r:
                self.assertEqual(json.load(r)["layers"],
                                 {"terrain": "/terrain/", "buildings": REMOTE["/buildings/"]})
        finally:
            server.shutdown()
            server.server_close()

    def test_not_found(self):
        self.assertEqual(self.get("/missing.js")[0], 404)
        self.assertEqual(self.get("/terrain/")[0], 404)            # no directory listings
        self.assertEqual(self.get("/terrain/%2e%2e/secret.txt")[0], 404)
        self.assertEqual(self.get("/api/replay?days=week")[0], 400)


def fake_feed(message):
    """A (handler, poll) feed that greets each browser with `message` and never polls."""
    async def handler(ws):
        await ws.send(message)
        await ws.wait_closed()

    async def poll():
        await asyncio.Event().wait()

    return handler, poll


class TestFeedRouting(unittest.TestCase):
    def test_paths_share_one_port(self):
        port = free_port()

        async def run():
            server = asyncio.create_task(serve_feeds(
                "localhost", port, {"/tide": fake_feed("tide"), "/vessels": fake_feed("vessels")}))
            await asyncio.sleep(0.3)
            try:
                got = {}
                for path in ("/tide", "/vessels", "/vessels?v=2"):
                    async with connect(f"ws://localhost:{port}{path}") as ws:
                        got[path] = await asyncio.wait_for(ws.recv(), 2)
                with self.assertRaises(InvalidStatus) as cm:
                    async with connect(f"ws://localhost:{port}/nope"):
                        pass
                return got, cm.exception.response.status_code
            finally:
                server.cancel()

        got, status = asyncio.run(run())
        self.assertEqual(got, {"/tide": "tide", "/vessels": "vessels", "/vessels?v=2": "vessels"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
