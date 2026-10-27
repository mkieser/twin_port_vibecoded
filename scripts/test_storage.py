"""The AIS, tide and storm surge files on S3, against a local moto server (skipped without moto: pip install
'moto[server]')."""

import logging
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import duckdb

import storage
from ais_consumer import PositionStore, analyze_day, compact_day, parse_position, parse_static
from ais_live import positions_geojson, recent_files
from storm_surge_consumer import RiskIndexStore, parse_feature
from storm_surge_live import storm_surge_geojson
from test_ais_consumer import INSIDE, NOW, OUTSIDE, position_msg, static_msg
from test_ais_live import free_port
from test_storm_surge_consumer import NOW as SURGE_NOW, risk_feature
from test_tide_consumer import NOW as TIDE_NOW, item
from tide_consumer import parse_reading, store_reading
from tide_live import latest_tide
from twin_app import replay_days

try:
    from moto.server import ThreadedMotoServer
except ImportError:
    ThreadedMotoServer = None

BUCKET = "test-bucket"
ROOT = f"s3://{BUCKET}/ais"


@unittest.skipIf(ThreadedMotoServer is None, "needs moto[server]")
class TestS3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger("werkzeug").setLevel(logging.ERROR)   # moto's request log
        port = free_port()
        cls.server = ThreadedMotoServer(port=port, verbose=False)
        cls.server.start()
        cls.env = mock.patch.dict(os.environ, {
            "AWS_ENDPOINT_URL": f"http://localhost:{port}", "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1"})
        cls.env.start()
        storage._s3_client.cache_clear()
        storage._bucket_region.cache_clear()
        storage._s3_client().create_bucket(Bucket=BUCKET)

    @classmethod
    def tearDownClass(cls):
        storage._s3_client.cache_clear()
        storage._bucket_region.cache_clear()
        cls.env.stop()
        cls.server.stop()

    def setUp(self):
        client = storage._s3_client()
        for obj in client.list_objects_v2(Bucket=BUCKET).get("Contents", []):
            client.delete_object(Bucket=BUCKET, Key=obj["Key"])

    def test_paths(self):
        self.assertEqual(storage.join(ROOT, "positions", "date=x"), f"{ROOT}/positions/date=x")
        self.assertEqual(storage.sibling(f"{ROOT}/positions", "vessels.parquet"),
                         f"{ROOT}/vessels.parquet")
        self.assertEqual(storage.location("s3://b/ais/"), "s3://b/ais")
        self.assertEqual(storage.location("/tmp/ais"), Path("/tmp/ais"))

    def test_consumer_compaction_and_readers(self):
        positions, daily = f"{ROOT}/positions", f"{ROOT}/positions_daily"
        store = PositionStore(out_dir=positions)
        self.assertEqual(store.vessels_path, f"{ROOT}/vessels.parquet")
        self.assertFalse(storage.exists(store.vessels_path))
        for mmsi in (2, 1):
            store.add(parse_position(position_msg(*INSIDE, mmsi=mmsi), NOW))
            store.add(parse_position(position_msg(*OUTSIDE, mmsi=9), NOW))
            store.flush()
        store.add_static(parse_static(static_msg(mmsi=1, ship_type=70), NOW))
        store.flush()
        store.close()

        self.assertEqual([storage.basename(p) for p in storage.subdirs(positions, "date=")],
                         ["date=2026-10-21"])
        files = storage.list_files(f"{positions}/date=2026-10-21", ".parquet")
        self.assertEqual(len(files), 2)
        self.assertEqual(sorted(recent_files(positions)), sorted(f for _, f in files))

        con = storage.connect(positions)
        collection = positions_geojson(con, recent_files(positions), store.vessels_path)
        con.close()
        self.assertEqual([(f["properties"]["mmsi"], f["properties"]["ship_type"])
                          for f in collection["features"]], [(1, 70), (2, None)])

        self.assertEqual(compact_day("2026-10-21", positions, daily), 2)
        self.assertEqual(compact_day("2026-10-20", positions, daily), 0)
        merged = f"{daily}/date=2026-10-21/positions.parquet"
        self.assertEqual(storage.list_files(f"{daily}/date=2026-10-21"),
                         [(mock.ANY, merged)])   # written in place, no .tmp left behind
        con = storage.connect(merged)
        self.assertEqual(con.execute(f"SELECT mmsi FROM '{merged}'").fetchall(), [(1,), (2,)])
        con.close()

    def test_analysis_and_replay(self):
        daily, analysis = f"{ROOT}/positions_daily", f"{ROOT}/analysis_daily"
        with tempfile.TemporaryDirectory() as tmp:
            berth_path = Path(tmp) / "berth.gpkg"
            duckdb.sql("INSTALL spatial; LOAD spatial")
            duckdb.sql(f"""
                COPY (SELECT 'A' AS name, ST_Buffer(ST_Transform(ST_Point({INSIDE[0]}, {INSIDE[1]}),
                          'EPSG:4326', 'EPSG:2961', always_xy := true), 50) AS geometry)
                TO '{berth_path}' (FORMAT GDAL, DRIVER 'GPKG', SRS 'EPSG:2961')""")
            con = storage.connect(daily)
            con.execute(f"""
                COPY (SELECT 1::BIGINT AS mmsi, 'ASTERIX' AS ship_name,
                             TIMESTAMPTZ '2026-10-21 03:00:00+00' AS ts, 0.0 AS sog,
                             {INSIDE[0]} AS lon, {INSIDE[1]} AS lat)
                TO '{daily}/date=2026-10-21/positions.parquet' (FORMAT parquet)""")
            con.close()
            self.assertEqual(analyze_day("2026-10-21", daily, berth_path, analysis), 1)

        result = replay_days(date(2026, 10, 22), 2, analysis, f"{ROOT}/vessels.parquet")
        self.assertEqual([(d["date"], d["analysed"], [v["mmsi"] for v in d["vessels"]])
                          for d in result["days"]],
                         [("2026-10-21", True, [1]), ("2026-10-22", False, [])])

    def test_tide_levels_and_feed(self):
        levels = f"s3://{BUCKET}/tide/levels"
        con = storage.connect(levels, spatial=False)
        self.assertIsNone(latest_tide(con, levels))
        for event_date, value in [("2026-10-21T19:20:00Z", 1.33), ("2026-10-21T19:25:00Z", 1.38)]:
            self.assertTrue(store_reading(parse_reading(item(event_date, value), "wlp", TIDE_NOW),
                                          levels))
        self.assertFalse(store_reading(parse_reading(item("2026-10-21T19:25:00Z", 1.38), "wlp",
                                                     TIDE_NOW), levels))   # already stored
        self.assertEqual([storage.basename(f) for _, f in storage.list_files(levels)],
                         ["levels_2026-10-21.parquet"])
        tide = latest_tide(con, levels)
        con.close()
        self.assertEqual((tide["ts"], tide["tide_level"], tide["trend"]),
                         ("2026-10-21T19:25:00Z", 1.38, "rising"))

    def test_storm_surge_snapshot_and_feed(self):
        snapshots, latest_path = (f"s3://{BUCKET}/coastal_flooding/risk_index",
                                  f"s3://{BUCKET}/coastal_flooding/latest.json")
        store = RiskIndexStore(out_dir=snapshots, latest_path=latest_path)
        con = storage.connect(snapshots)
        try:
            self.assertIsNone(storm_surge_geojson(con, SURGE_NOW, latest_path, snapshots))
            written = store.save([parse_feature(risk_feature(*INSIDE, id="surge"), SURGE_NOW)],
                                 SURGE_NOW)
            self.assertEqual(written, f"{snapshots}/risk_index_20261022T1200Z.parquet")
            self.assertIsNone(store.save([parse_feature(risk_feature(*INSIDE, id="surge"),
                                                        SURGE_NOW)], SURGE_NOW))   # unchanged
            collection = storm_surge_geojson(con, SURGE_NOW, latest_path, snapshots)
        finally:
            con.close()
            store.close()
        self.assertEqual([f["properties"]["id"] for f in collection["features"]], ["surge"])


if __name__ == "__main__":
    unittest.main()
