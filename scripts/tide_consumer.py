"""
Poll the CHS IWLS API for the current tide level at Halifax (station 00490) and
store each reading as Parquet, written with DuckDB.

  Source   https://api-iwls.dfo-mpo.gc.ca/api/v1 (no key needed)
  Output   DATA_DIR/tide/levels/levels_YYYY-MM-DD.parquet, one file per UTC day
           ts, date, time, tide_level, series, qc_flag, fetched_at

IWLS is a plain HTTP API, so this polls rather than streams. Every POLL_SECONDS
the station's readings for the last LOOKBACK are requested and the most recent
one is stored. The observed water level (wlo) is tried first; when the gauge has
nothing in the window, the predicted level (wlp) up to now is stored instead,
and the series column says which one each row is. Halifax's wlo series has been
empty since late 2025, so expect wlp rows until the gauge reports again.

  ts, date, time  when the level applies, UTC (date and time are ts split up)
  tide_level      metres above chart datum
  series          'wlo' (observed) or 'wlp' (predicted)
  qc_flag         IWLS quality control flag, e.g. '1' good, '2' not evaluated
  fetched_at      when the reading was fetched, UTC

The day's file is rewritten on each new reading, via a temporary file so readers
never see a half-written one. A reading already stored (same ts) is skipped, so
a gauge that falls behind does not create duplicates. Read everything back with
  SELECT * FROM read_parquet('DATA_DIR/tide/levels/*.parquet') ORDER BY ts

Needs duckdb (pip install duckdb). Stop with Ctrl+C or SIGTERM.
"""

import argparse
import json
import logging
import signal
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import duckdb

from load_rasters import DATA_DIR

LEVELS_DIR = DATA_DIR / "tide" / "levels"

IWLS_URL = "https://api-iwls.dfo-mpo.gc.ca/api/v1"
STATION_CODE = "00490"       # Halifax
TIME_SERIES = ["wlo", "wlp"]  # observed first, then predicted
POLL_SECONDS = 300
LOOKBACK = timedelta(hours=1)
HTTP_TIMEOUT = 30

log = logging.getLogger("tide_consumer")

# Columns, in the order parse_reading returns them
COLUMNS = """
    ts TIMESTAMPTZ, date DATE, time TIME, tide_level DOUBLE, series VARCHAR,
    qc_flag VARCHAR, fetched_at TIMESTAMPTZ
"""


def iso_utc(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.load(response)


def station_id(code=STATION_CODE, base_url=IWLS_URL):
    """IWLS station id for a CHS station code such as '00490'."""
    stations = fetch_json(f"{base_url}/stations?{urlencode({'code': code})}")
    if not stations:
        raise ValueError(f"no IWLS station with code {code}")
    return stations[0]["id"]


def parse_reading(item, series, fetched_at):
    """Row for one IWLS data item, e.g. {'eventDate': '2026-10-21T19:25:00Z', 'value': 1.38, ...}."""
    ts = datetime.fromisoformat(item["eventDate"]).astimezone(timezone.utc)
    return (ts, ts.date(), ts.time(), item["value"], series, item.get("qcFlagCode"),
            fetched_at)


def latest_reading(station, now, base_url=IWLS_URL, lookback=LOOKBACK):
    """Most recent reading at or before now, from the first of TIME_SERIES that has one."""
    for series in TIME_SERIES:
        query = urlencode({"time-series-code": series,
                           "from": iso_utc(now - lookback), "to": iso_utc(now)})
        items = [item for item in fetch_json(f"{base_url}/stations/{station}/data?{query}")
                 if item.get("value") is not None]
        if items:
            return parse_reading(max(items, key=lambda item: item["eventDate"]), series, now)
    return None


def store_reading(row, out_dir=LEVELS_DIR):
    """Add a reading to its day's file. Returns False if that ts was already stored."""
    path = out_dir / f"levels_{row[1]}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.execute(f"CREATE TABLE levels ({COLUMNS})")
        if path.exists():
            con.execute(f"INSERT INTO levels SELECT * FROM read_parquet('{path}')")
        if con.execute("SELECT count(*) FROM levels WHERE ts = ?", [row[0]]).fetchone()[0]:
            return False
        con.execute(f"INSERT INTO levels VALUES ({', '.join('?' * 7)})", row)
        out_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT * FROM levels ORDER BY ts)
            TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)""")
    finally:
        con.close()
    tmp.replace(path)
    return True


def poll(station, stop, out_dir=LEVELS_DIR, base_url=IWLS_URL, poll_seconds=POLL_SECONDS):
    """Store the latest reading every poll_seconds until stop is set."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            row = latest_reading(station, datetime.now(timezone.utc), base_url)
        except (OSError, ValueError, KeyError) as exc:  # network, bad JSON, odd payload
            log.warning("fetch failed, retrying next poll: %s", exc)
        else:
            if row is None:
                log.warning("no %s readings in the last %s", " or ".join(TIME_SERIES), LOOKBACK)
            elif store_reading(row, out_dir):
                log.info("stored %s %.3f m (%s)", iso_utc(row[0]), row[3], row[4])
            else:
                log.info("no new reading since %s", iso_utc(row[0]))
        stop.wait(poll_seconds - (time.monotonic() - started))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--station", default=STATION_CODE, help="CHS station code")
    parser.add_argument("--url", default=IWLS_URL)
    parser.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    station = station_id(args.station, args.url)
    log.info("polling station %s (IWLS id %s) every %g s", args.station, station, args.interval)
    try:
        poll(station, stop, base_url=args.url, poll_seconds=args.interval)
    except KeyboardInterrupt:
        pass
    print(f"Tide levels in {LEVELS_DIR}")
