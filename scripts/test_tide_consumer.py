import tempfile
import threading
import unittest
from datetime import datetime, time, timezone
from pathlib import Path
from unittest import mock

import duckdb

import tide_consumer
from tide_consumer import latest_reading, parse_reading, poll, store_reading

NOW = datetime(2026, 10, 21, 19, 27, 13, tzinfo=timezone.utc)
STATION = "5cebf1df3d0f4a073c4bbcbb"


def item(event_date, value, qc="2"):
    return {"eventDate": event_date, "qcFlagCode": qc, "reviewed": False,
            "timeSeriesId": "5cebf1df3d0f4a073c4bbcba", "value": value}


def fake_api(**series):
    """fetch_json stand-in answering each time-series-code with series[code] (default [])."""
    urls = []

    def fetch(url):
        urls.append(url)
        code = url.split("time-series-code=")[1].split("&")[0]
        return series.get(code, [])
    return fetch, urls


def read_levels(out_dir):
    """Stored tide levels in ts order."""
    return [level for (level,) in duckdb.sql(
        f"SELECT tide_level FROM read_parquet('{out_dir}/*.parquet') ORDER BY ts").fetchall()]


class TestParse(unittest.TestCase):
    def test_reading_fields(self):
        row = parse_reading(item("2026-10-21T19:25:00Z", 1.382), "wlp", NOW)
        ts = datetime(2026, 10, 21, 19, 25, tzinfo=timezone.utc)
        self.assertEqual(row, (ts, ts.date(), time(19, 25), 1.382, "wlp", "2", NOW))

    def test_observed_preferred(self):
        fetch, urls = fake_api(wlo=[item("2026-10-21T19:20:00Z", 1.30, "1"),
                                    item("2026-10-21T19:21:00Z", 1.31, "1")],
                               wlp=[item("2026-10-21T19:27:00Z", 1.40)])
        with mock.patch.object(tide_consumer, "fetch_json", fetch):
            row = latest_reading(STATION, NOW)
        self.assertEqual((row[2], row[3], row[4], row[5]), (time(19, 21), 1.31, "wlo", "1"))
        self.assertEqual(len(urls), 1)
        self.assertIn("from=2026-10-21T18%3A27%3A13Z&to=2026-10-21T19%3A27%3A13Z", urls[0])

    def test_falls_back_to_predictions(self):
        fetch, _ = fake_api(wlo=[item("2026-10-21T19:20:00Z", None)],  # gap in the gauge
                            wlp=[item("2026-10-21T19:26:00Z", 1.39),
                                 item("2026-10-21T19:27:00Z", 1.40)])
        with mock.patch.object(tide_consumer, "fetch_json", fetch):
            row = latest_reading(STATION, NOW)
        self.assertEqual((row[2], row[3], row[4]), (time(19, 27), 1.40, "wlp"))

    def test_no_readings(self):
        fetch, _ = fake_api()
        with mock.patch.object(tide_consumer, "fetch_json", fetch):
            self.assertIsNone(latest_reading(STATION, NOW))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "levels"

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, event_date, value, series="wlp"):
        return parse_reading(item(event_date, value), series, NOW)

    def test_appends_sorted_and_skips_duplicates(self):
        self.assertTrue(store_reading(self.row("2026-10-21T19:25:00Z", 1.38), self.out_dir))
        self.assertTrue(store_reading(self.row("2026-10-21T19:20:00Z", 1.33), self.out_dir))
        self.assertFalse(store_reading(self.row("2026-10-21T19:25:00Z", 9.99), self.out_dir))
        self.assertEqual(read_levels(self.out_dir), [1.33, 1.38])
        self.assertEqual([f.name for f in self.out_dir.iterdir()], ["levels_2026-10-21.parquet"])

    def test_one_file_per_utc_day(self):
        store_reading(self.row("2026-10-21T23:59:00Z", 1.0), self.out_dir)
        store_reading(self.row("2026-10-22T00:04:00Z", 1.1), self.out_dir)
        self.assertEqual(sorted(f.name for f in self.out_dir.iterdir()),
                         ["levels_2026-10-21.parquet", "levels_2026-10-22.parquet"])

    def test_poll_survives_fetch_errors(self):
        stop = threading.Event()
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("connection reset")
            if len(calls) == 3:   # second poll: wlo empty, then wlp
                stop.set()
                return [item("2026-10-21T19:27:00Z", 1.40)]
            return []

        with mock.patch.object(tide_consumer, "fetch_json", fetch):
            poll(STATION, stop, self.out_dir, poll_seconds=0.01)
        self.assertEqual(read_levels(self.out_dir), [1.40])

    def test_lambda_handler_polls_once(self):
        fetch, urls = fake_api(wlp=[item("2026-10-21T19:25:00Z", 1.38)])
        with (mock.patch.object(tide_consumer, "fetch_json", fetch),
              mock.patch.object(tide_consumer, "LEVELS_DIR", self.out_dir),
              mock.patch.dict(tide_consumer._station, {tide_consumer.STATION_CODE: STATION})):
            self.assertEqual(tide_consumer.poll_handler({}, None),
                             {"ts": "2026-10-21T19:25:00Z", "tide_level": 1.38, "series": "wlp"})
        self.assertEqual(len(urls), 2)   # wlo, then wlp
        self.assertEqual(read_levels(self.out_dir), [1.38])


if __name__ == "__main__":
    unittest.main()
