import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from websockets.asyncio.server import serve

import ais_consumer
from ais_consumer import (PositionStore, analyze_day, compact_day, parse_position,
                          parse_static, parse_time_utc)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
INSIDE = (-63.57, 44.65)    # Halifax Harbour, inside the extent
OUTSIDE = (-63.62, 44.71)   # inside the padded bbox, north of the extent


def position_msg(lon, lat, mmsi=316001234, kind="PositionReport", **report):
    body = {"UserID": mmsi, "Longitude": lon, "Latitude": lat, "Sog": 4.2, "Cog": 87.5,
            "TrueHeading": 90, "NavigationalStatus": 0, "RateOfTurn": 0,
            "PositionAccuracy": True, "Valid": True, **report}
    return {"MessageType": kind, "Message": {kind: body},
            "MetaData": {"MMSI": mmsi, "ShipName": "ASTERIX             ",
                         "latitude": lat, "longitude": lon,
                         "time_utc": "2026-10-21 11:59:58.123456789 +0000 UTC"}}


def static_msg(mmsi=316001234, ship_type=52, **report):
    """ShipStaticData as aisstream sends it (class A)."""
    body = {"UserID": mmsi, "Type": ship_type, "CallSign": "CFA1234", "ImoNumber": 9586057,
            "Name": "ASTERIX             ", "Valid": True, **report}
    return {"MessageType": "ShipStaticData", "Message": {"ShipStaticData": body},
            "MetaData": {"MMSI": mmsi, "ShipName": "ASTERIX             "}}


def class_b_static_msg(mmsi=316009999, part_b=True, ship_type=37):
    report = {"UserID": mmsi, "Valid": True, "PartNumber": part_b,
              "ReportA": {"Valid": not part_b, "Name": "OBELIX"},
              "ReportB": {"Valid": part_b, "ShipType": ship_type if part_b else 0,
                          "CallSign": "VB1234 " if part_b else ""}}
    return {"MessageType": "StaticDataReport", "Message": {"StaticDataReport": report},
            "MetaData": {"MMSI": mmsi}}


def read_positions(out_dir):
    return duckdb.sql(f"""
        SELECT mmsi, lon, lat, date FROM read_parquet('{out_dir}/**/*.parquet', hive_partitioning = true)
        ORDER BY mmsi""")


class TestParse(unittest.TestCase):
    def test_time_utc_nanoseconds_truncated(self):
        self.assertEqual(parse_time_utc("2026-10-21 11:59:58.123456789 +0000 UTC"),
                         datetime(2026, 10, 21, 11, 59, 58, 123456, tzinfo=timezone.utc))
        self.assertEqual(parse_time_utc("2026-10-21 11:59:58 +0000 UTC").microsecond, 0)
        self.assertIsNone(parse_time_utc("garbage"))

    def test_position_fields(self):
        row = parse_position(position_msg(*INSIDE), NOW)
        self.assertEqual(row[:3], (316001234, "ASTERIX", "PositionReport"))
        self.assertEqual(row[5:7], INSIDE)

    def test_not_available_values_become_null(self):
        row = parse_position(position_msg(*INSIDE, Sog=102.3, Cog=360, TrueHeading=511,
                                          RateOfTurn=-128), NOW)
        self.assertEqual((row[7], row[8], row[9], row[11]), (None, None, None, None))

    def test_unusable_messages_dropped(self):
        self.assertIsNone(parse_position(position_msg(181, 91), NOW))
        self.assertIsNone(parse_position(position_msg(*INSIDE, Valid=False), NOW))
        self.assertIsNone(parse_position({"MessageType": "ShipStaticData"}, NOW))

    def test_static_class_a(self):
        self.assertEqual(parse_static(static_msg(), NOW), (316001234, 52, "CFA1234", 9586057, NOW))
        self.assertEqual(parse_static(static_msg(ImoNumber=0, CallSign=""), NOW)[2:4], (None, None))

    def test_static_class_b_part_b_only(self):
        self.assertEqual(parse_static(class_b_static_msg(), NOW), (316009999, 37, "VB1234", None, NOW))
        self.assertIsNone(parse_static(class_b_static_msg(part_b=False), NOW))

    def test_static_without_type_dropped(self):
        self.assertIsNone(parse_static(static_msg(ship_type=0), NOW))
        self.assertIsNone(parse_static(static_msg(Valid=False), NOW))
        self.assertIsNone(parse_static(position_msg(*INSIDE), NOW))


class TestPositionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "positions"
        self.store = PositionStore(out_dir=self.out_dir)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_bbox_covers_halifax(self):
        west, south, east, north = self.store.bbox(0)
        self.assertTrue(west < INSIDE[0] < east and south < INSIDE[1] < north)

    def test_flush_clips_to_extent_and_writes_geoparquet(self):
        self.store.add(parse_position(position_msg(*INSIDE, mmsi=1), NOW))
        self.store.add(parse_position(position_msg(*OUTSIDE, mmsi=2), NOW))
        self.assertEqual(self.store.flush(), (2, 1))

        files = list(self.out_dir.glob("date=2026-10-21/*.parquet"))
        self.assertEqual(len(files), 1)
        geo = duckdb.sql(f"SELECT decode(value) FROM parquet_kv_metadata('{files[0]}') "
                         "WHERE decode(key) = 'geo'").fetchone()[0]
        self.assertEqual(json.loads(geo)["primary_column"], "geometry")

        rows = read_positions(self.out_dir).fetchall()
        self.assertEqual([r[0] for r in rows], [1])
        self.assertEqual(rows[0][1:3], INSIDE)
        crs = json.loads(geo)["columns"]["geometry"]["crs"]
        self.assertEqual(crs["id"], {"authority": "EPSG", "code": 4326})

    def test_flushes_append(self):
        for mmsi in (1, 2):
            self.store.add(parse_position(position_msg(*INSIDE, mmsi=mmsi), NOW))
            self.store.flush()
        self.assertEqual(len(read_positions(self.out_dir).fetchall()), 2)

    def test_empty_flush_writes_nothing(self):
        self.store.add(parse_position(position_msg(*OUTSIDE), NOW))
        self.assertEqual(self.store.flush(), (1, 0))
        self.assertFalse(self.out_dir.exists())

    def test_compact_day_merges_flush_files(self):
        daily_dir = Path(self.tmp.name) / "daily"
        for mmsi in (2, 1, 3):
            self.store.add(parse_position(position_msg(*INSIDE, mmsi=mmsi), NOW))
            self.store.flush()
        self.assertEqual(len(list(self.out_dir.glob("date=2026-10-21/*.parquet"))), 3)

        for _ in range(2):  # rerunning replaces the merged file
            self.assertEqual(compact_day("2026-10-21", self.out_dir, daily_dir), 3)
        files = list(daily_dir.rglob("*.parquet*"))
        self.assertEqual(files, [daily_dir / "date=2026-10-21" / "positions.parquet"])

        merged = duckdb.sql(f"SELECT mmsi FROM read_parquet('{files[0]}')").fetchall()
        self.assertEqual(merged, [(1,), (2,), (3,)])
        geo = duckdb.sql(f"SELECT decode(value) FROM parquet_kv_metadata('{files[0]}') "
                         "WHERE decode(key) = 'geo'").fetchone()[0]
        self.assertEqual(json.loads(geo)["primary_column"], "geometry")
        self.assertEqual(read_positions(daily_dir).fetchall(),
                         read_positions(self.out_dir).fetchall())

    def test_compact_day_without_files(self):
        daily_dir = Path(self.tmp.name) / "daily"
        self.assertEqual(compact_day("2026-10-20", self.out_dir, daily_dir), 0)
        self.assertFalse(daily_dir.exists())


class TestVessels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "positions"
        self.vessels_path = Path(self.tmp.name) / "vessels.parquet"

    def tearDown(self):
        self.tmp.cleanup()

    def read_vessels(self):
        return duckdb.sql(f"SELECT mmsi, ship_type, call_sign FROM read_parquet('{self.vessels_path}') "
                          "ORDER BY mmsi").fetchall()

    def test_latest_static_per_vessel_kept_across_restarts(self):
        store = PositionStore(out_dir=self.out_dir)
        self.assertEqual(store.vessels_path, self.vessels_path)
        store.add_static(parse_static(static_msg(mmsi=1, ship_type=70), NOW))
        store.add_static(parse_static(static_msg(mmsi=2, ship_type=52), NOW))
        store.add_static(parse_static(static_msg(mmsi=1, ship_type=80, CallSign="NEW"), NOW))
        self.assertEqual(store.flush(), (0, 0))   # vessels only, no positions
        store.close()
        self.assertEqual(self.read_vessels(), [(1, 80, "NEW"), (2, 52, "CFA1234")])

        store = PositionStore(out_dir=self.out_dir)   # reloads the file
        store.add_static(parse_static(static_msg(mmsi=3, ship_type=60), NOW))
        store.flush()
        store.close()
        self.assertEqual([r[0] for r in self.read_vessels()], [1, 2, 3])

    def test_no_static_no_file(self):
        store = PositionStore(out_dir=self.out_dir)
        store.flush()
        store.close()
        self.assertFalse(self.vessels_path.exists())


class TestAnalyzeDay(unittest.TestCase):
    """Two 100 m square berths, A at x 455000-455100 and B at 455200-455300."""

    # mmsi, ship_name, time (UTC), sog, x, y (EPSG:2961, stored as lon/lat)
    POSITIONS = [
        (1, "ASTERIX", "01:10", 0.1, 455010, 4945010),   # moored in A, hours 1, 2, 5
        (1, "ASTERIX", "01:50", 0.0, 455030, 4945030),
        (1, None,      "02:30", 0.2, 455050, 4945050),
        (1, "ASTRX",   "05:00", 0.0, 455070, 4945070),
        (1, "ASTERIX", "09:15", 0.3, 455250, 4945050),   # then B, hour 9
        (2, "OBELIX",  "03:00", 8.0, 455050, 4945050),   # passing through A
        (3, "IDEFIX",  "04:20", None, 455060, 4945040),  # no sog reported, in A
        (4, "PANORAMIX", "06:00", 0.0, 456000, 4946000), # stationary outside berths
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.daily_dir, self.out_dir = tmp / "daily", tmp / "analysis"
        self.berth_path = tmp / "berth.gpkg"
        duckdb.sql("INSTALL spatial; LOAD spatial")
        duckdb.sql(f"""
            COPY (SELECT * FROM (VALUES
                ('A', ST_GeomFromText('POLYGON((455000 4945000, 455100 4945000, 455100 4945100, 455000 4945100, 455000 4945000))')),
                ('B', ST_GeomFromText('POLYGON((455200 4945000, 455300 4945000, 455300 4945100, 455200 4945100, 455200 4945000))'))
            ) t(name, geometry))
            TO '{self.berth_path}' (FORMAT GDAL, DRIVER 'GPKG', SRS 'EPSG:2961')""")

    def tearDown(self):
        self.tmp.cleanup()

    def write_positions(self, rows):
        src = self.daily_dir / "date=2026-10-21" / "positions.parquet"
        src.parent.mkdir(parents=True)
        con = duckdb.connect()
        con.execute("CREATE TABLE pos (mmsi BIGINT, ship_name VARCHAR, ts TIMESTAMPTZ, "
                    "sog DOUBLE, x DOUBLE, y DOUBLE)")
        con.executemany("INSERT INTO pos VALUES (?, ?, ?, ?, ?, ?)",
                        [(m, n, f"2026-10-21 {t}:00+00", s, x, y) for m, n, t, s, x, y in rows])
        con.execute("LOAD spatial")
        con.execute(f"""
            COPY (SELECT * EXCLUDE (x, y, p), ST_X(p) AS lon, ST_Y(p) AS lat
                  FROM (SELECT *, ST_Transform(ST_Point(x, y), 'EPSG:2961', 'EPSG:4326',
                                               always_xy := true) AS p FROM pos))
            TO '{src}' (FORMAT parquet)""")
        con.close()

    def analyze(self):
        return analyze_day("2026-10-21", self.daily_dir, self.berth_path, self.out_dir)

    def read_analysis(self):
        return duckdb.sql(f"SELECT * FROM read_parquet('{self.out_dir}/date=2026-10-21/analysis.parquet', "
                          "hive_partitioning = false)")

    def test_occupancy(self):
        self.write_positions(self.POSITIONS)
        self.assertEqual(self.analyze(), 3)
        result = self.read_analysis()
        self.assertEqual(result.columns,
                         ["mmsi", "ship_name", "berth_name", "dwell_time", "lon", "lat"])
        rows = result.fetchall()
        expected = [(1, "ASTERIX", "A", 3, 455040, 4945040),
                    (3, "IDEFIX", "A", 1, 455060, 4945040),
                    (1, "ASTERIX", "B", 1, 455250, 4945050)]
        self.assertEqual([r[:4] for r in rows], [e[:4] for e in expected])
        for row, (*_, x, y) in zip(rows, expected):
            lon, lat = duckdb.sql(f"""
                SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_Point({x}, {y}),
                    'EPSG:2961', 'EPSG:4326', always_xy := true) AS p)""").fetchone()
            self.assertAlmostEqual(row[4], lon, places=7)  # ~1 cm
            self.assertAlmostEqual(row[5], lat, places=7)

    def test_rerun_replaces_file(self):
        self.write_positions(self.POSITIONS)
        self.analyze()
        self.assertEqual(self.analyze(), 3)
        self.assertEqual(len(list(self.out_dir.rglob("*.parquet*"))), 1)

    def test_day_without_berthed_vessels_writes_empty_file(self):
        self.write_positions(self.POSITIONS[5:6])
        self.assertEqual(self.analyze(), 0)
        self.assertEqual(self.read_analysis().fetchall(), [])

    def test_day_without_positions(self):
        self.assertEqual(self.analyze(), 0)
        self.assertFalse(self.out_dir.exists())


class TestConsumeEndToEnd(unittest.TestCase):
    def test_mock_stream(self):
        received = []

        async def handler(ws):
            received.append(json.loads(await ws.recv()))
            for i, pos in enumerate([INSIDE, OUTSIDE, INSIDE]):
                await ws.send(json.dumps(position_msg(*pos, mmsi=100 + i)))
            await ws.send(json.dumps({"error": "done"}))  # ends consume()

        async def run(store):
            async with serve(handler, "localhost", 0) as server:
                port = server.sockets[0].getsockname()[1]
                with self.assertRaisesRegex(RuntimeError, "done"):
                    await ais_consumer.consume(store, f"ws://localhost:{port}", "KEY")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "positions"
            store = PositionStore(out_dir=out_dir)
            asyncio.run(run(store))
            self.assertEqual(store.flush(), (3, 2))
            store.close()
            self.assertEqual([r[0] for r in read_positions(out_dir).fetchall()], [100, 102])

        sub = received[0]
        self.assertEqual(sub["APIKey"], "KEY")
        self.assertIn("ShipStaticData", sub["FilterMessageTypes"])
        (south, west), (north, east) = sub["BoundingBoxes"][0]
        self.assertTrue(south < north and west < east and 44 < south < 45)


if __name__ == "__main__":
    unittest.main()
