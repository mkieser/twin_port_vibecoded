import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import duckdb

import storm_surge_consumer
from storm_surge_consumer import (RiskIndexStore, fetch_features, parse_feature, poll,
                                  read_latest)
from test_ais_consumer import INSIDE

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
FAR = (-65.0, 47.08)   # Gulf of St. Lawrence, inside the collection, outside the extent


def cell(lon, lat, half=0.0417):
    """A risk index grid cell around lon, lat."""
    w, s, e, n = lon - half, lat - half, lon + half, lat + half
    return {"type": "Polygon", "coordinates": [[[w, s], [w, n], [e, n], [e, s], [w, s]]]}


def risk_feature(lon, lat, id="33226bba", storm_surge=True, amendment=0, risk=2,
                 expiration="2026-10-23T12:00:00Z"):
    """Feature as in the MSC Datamart files (nested metobject)."""
    return {"type": "Feature", "id": id, "geometry": cell(lon, lat), "properties": {
        "amendment": amendment, "publication_datetime": "2026-10-21T22:00:00Z",
        "validity_datetime": "2026-10-22T12:00:00Z", "expiration_datetime": expiration,
        "type": "metattrarea", "product_class": "CF_Risk_Index",
        "product_sub_type": "Maritimes", "product_type": "ASPC", "domain": "MSC",
        "status": "final", "id": id,
        "metobject": {"waves": {"value": False}, "tide": {"value": True}, "sub_type": 2,
                      "impact": {"value": 2}, "confidence": {"value": 3},
                      "risk": {"value": risk}, "storm_surge": {"value": storm_surge}}}}


def read_snapshot(path):
    return duckdb.sql(f"SELECT id, risk FROM read_parquet('{path}') ORDER BY id").fetchall()


class TestParse(unittest.TestCase):
    def test_nested_properties(self):
        row = parse_feature(risk_feature(*INSIDE), NOW)
        self.assertEqual(row[0], "33226bba")
        self.assertEqual(row[1:4], ("2026-10-21T22:00:00Z", "2026-10-22T12:00:00Z",
                                    "2026-10-23T12:00:00Z"))
        self.assertEqual(row[4:15], (0, "ASPC", "Maritimes", "final", True, True, False,
                                     2, 2, 3, 2))
        self.assertEqual(json.loads(row[16])["type"], "Polygon")

    def test_flattened_properties(self):
        feature = risk_feature(*INSIDE)
        props = feature["properties"]
        props.pop("metobject")
        props.update({"metobject.storm_surge.value": True, "metobject.risk.value": 3,
                      "metobject.likelihood.value": 4, "metobject.sub_type": 2})
        row = parse_feature(feature, NOW)
        self.assertEqual((row[8], row[11], row[13], row[14]), (True, 3, 4, 2))

    def test_no_geometry(self):
        self.assertIsNone(parse_feature({**risk_feature(*INSIDE), "geometry": None}, NOW))

    def test_fetch_follows_next_links(self):
        pages = {
            "first": {"features": [risk_feature(*INSIDE, id="a")],
                      "links": [{"rel": "next", "href": "second"}]},
            "second": {"features": [risk_feature(*INSIDE, id="b")],
                       "links": [{"rel": "next", "href": "third"}]},
            "third": {"features": [], "links": [{"rel": "next", "href": "fourth"}]},
        }
        urls = []

        def fetch(url):
            urls.append(url)
            return pages["first" if "bbox=" in url else url]

        with mock.patch.object(storm_surge_consumer, "fetch_json", fetch):
            features = fetch_features((-63.63, 44.61, -63.5, 44.7))
        self.assertEqual([f["id"] for f in features], ["a", "b"])
        self.assertIn("bbox=-63.63%2C44.61%2C-63.5%2C44.7", urls[0])
        self.assertEqual(urls[1:], ["second", "third"])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "risk_index"
        self.latest_path = Path(self.tmp.name) / "latest.json"
        self.store = RiskIndexStore(out_dir=self.out_dir, latest_path=self.latest_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, *features, fetched_at=NOW):
        return self.store.save([parse_feature(f, fetched_at) for f in features], fetched_at)

    def test_bbox_covers_halifax(self):
        west, south, east, north = self.store.bbox()
        self.assertTrue(west < INSIDE[0] < east and south < INSIDE[1] < north)

    def test_clips_to_extent_and_writes_geoparquet(self):
        written = self.save(risk_feature(*INSIDE, id="in"), risk_feature(*FAR, id="far"))
        self.assertEqual(written.name, "risk_index_20261022T1200Z.parquet")
        self.assertEqual(read_snapshot(written), [("in", 2)])
        (geo,) = duckdb.sql(f"SELECT decode(value) FROM parquet_kv_metadata('{written}') "
                            "WHERE decode(key) = 'geo'").fetchall()[0]
        self.assertEqual(json.loads(geo)["columns"]["geometry"]["geometry_types"], ["Polygon"])
        self.assertEqual(read_latest(self.latest_path), {
            "fetched_at": "2026-10-22T12:00:00Z", "snapshot": written.name,
            "features": [["in", 0]]})

    def test_unchanged_poll_writes_no_file(self):
        first = self.save(risk_feature(*INSIDE))
        later = datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc)
        self.assertIsNone(self.save(risk_feature(*INSIDE), fetched_at=later))
        self.assertEqual(list(self.out_dir.iterdir()), [first])
        latest = read_latest(self.latest_path)
        self.assertEqual((latest["fetched_at"], latest["snapshot"]),
                         ("2026-10-22T12:30:00Z", first.name))

    def test_amendment_writes_new_snapshot(self):
        self.save(risk_feature(*INSIDE))
        later = datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc)
        written = self.save(risk_feature(*INSIDE, amendment=1, risk=3), fetched_at=later)
        self.assertEqual(read_snapshot(written), [("33226bba", 3)])
        self.assertEqual(len(list(self.out_dir.iterdir())), 2)

    def test_cleared_risk(self):
        self.save(risk_feature(*INSIDE))
        later = datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc)
        self.assertIsNone(self.save(risk_feature(*FAR), fetched_at=later))
        self.assertEqual(read_latest(self.latest_path), {
            "fetched_at": "2026-10-22T12:30:00Z", "snapshot": None, "features": []})

    def test_first_poll_without_features(self):
        self.assertIsNone(self.save())
        self.assertFalse(self.out_dir.exists())
        self.assertEqual(read_latest(self.latest_path)["snapshot"], None)

    def test_poll_survives_fetch_errors(self):
        stop = threading.Event()
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("connection reset")
            stop.set()
            return {"features": [risk_feature(*INSIDE)], "links": []}

        with mock.patch.object(storm_surge_consumer, "fetch_json", fetch):
            poll(self.store, stop, poll_seconds=0.01)
        latest = read_latest(self.latest_path)
        self.assertEqual(read_snapshot(self.out_dir / latest["snapshot"]), [("33226bba", 2)])


if __name__ == "__main__":
    unittest.main()
