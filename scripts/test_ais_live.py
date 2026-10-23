import asyncio
import json
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from websockets.asyncio.client import connect

from ais_consumer import PositionStore, parse_position, parse_static
from ais_live import recent_files, serve_live_positions
from test_ais_consumer import INSIDE, position_msg, static_msg

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


class TestLivePositions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.positions_dir = Path(self.tmp.name) / "positions"
        self.store = PositionStore(out_dir=self.positions_dir)
        self.mtime = 1_800_000_000

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def write_file(self, *messages, minutes_later=1):
        """Flush messages into a new positions file, newer than any before it."""
        before = set(self.positions_dir.rglob("*.parquet"))
        for msg in messages:
            self.store.add(parse_position(msg, NOW))
        self.store.flush()
        (newest,) = set(self.positions_dir.rglob("*.parquet")) - before
        self.mtime += 60 * minutes_later
        os.utime(newest, (self.mtime, self.mtime))
        return newest

    def test_recent_files_window(self):
        self.assertEqual(recent_files(self.positions_dir), [])
        old = self.write_file(position_msg(*INSIDE, mmsi=1))
        mid = self.write_file(position_msg(*INSIDE, mmsi=2), minutes_later=11)
        new = self.write_file(position_msg(*INSIDE, mmsi=3), minutes_later=10)
        self.assertEqual(recent_files(self.positions_dir, 600), [new, mid])  # old is 21 min older

    def test_browser_gets_snapshot_then_updates(self):
        lon, lat = INSIDE
        later = position_msg(lon + 0.001, lat, mmsi=1, Sog=2.0)
        later["MetaData"]["time_utc"] = "2026-10-21 12:00:30.5 +0000 UTC"
        self.store.add_static(parse_static(static_msg(mmsi=1, ship_type=70), NOW))
        self.write_file(position_msg(lon, lat, mmsi=1), later, position_msg(lon, lat, mmsi=2))
        port = free_port()

        async def run():
            server = asyncio.create_task(serve_live_positions(
                "localhost", port, self.positions_dir, poll_seconds=0.05))
            await asyncio.sleep(0.3)
            try:
                async with connect(f"ws://localhost:{port}") as ws:
                    snapshot = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    self.write_file(position_msg(lon, lat, mmsi=3))
                    update = json.loads(await asyncio.wait_for(ws.recv(), 2))
            finally:
                server.cancel()
            return snapshot, update

        snapshot, update = asyncio.run(run())
        self.assertEqual(snapshot["type"], "FeatureCollection")
        self.assertEqual([f["properties"]["mmsi"] for f in snapshot["features"]], [1, 2])
        # the update still has vessels 1 and 2 from the earlier file in the window
        self.assertEqual([f["properties"]["mmsi"] for f in update["features"]], [1, 2, 3])

        feature = snapshot["features"][0]
        self.assertEqual(feature["geometry"]["coordinates"], [lon + 0.001, lat])
        self.assertEqual(feature["properties"]["sog"], 2.0)
        self.assertEqual(feature["properties"]["ship_name"], "ASTERIX")
        self.assertEqual([f["properties"]["ship_type"] for f in snapshot["features"]], [70, None])
        self.assertEqual(feature["properties"]["ts"], "2026-10-21T12:00:30.500000Z")

    def test_map_page_over_http(self):
        port = free_port()

        def get(path):
            try:
                with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=2) as r:
                    return r.status, r.headers["Content-Type"], r.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, None, None

        async def run():
            server = asyncio.create_task(serve_live_positions(
                "localhost", port, self.positions_dir, poll_seconds=0.05))
            await asyncio.sleep(0.3)
            try:
                return await asyncio.to_thread(get, "/"), await asyncio.to_thread(get, "/favicon.ico")
            finally:
                server.cancel()

        (status, content_type, body), (missing, _, _) = asyncio.run(run())
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn("new WebSocket", body)
        self.assertEqual(missing, 404)


if __name__ == "__main__":
    unittest.main()
