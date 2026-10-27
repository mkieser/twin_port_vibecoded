"""
Consume live AIS positions from aisstream.io and store the ones inside the
project extent as GeoParquet, written with DuckDB.

  Extent   DATA_DIR/extent.gpkg  (EPSG:2961 polygon, the 10 km data square)
  Output   AIS_DIR/positions/date=YYYY-MM-DD/positions_{uuid}.parquet
           GeoParquet 1.0, point geometry in EPSG:4326, plus the original
           lon/lat as plain columns.

The websocket subscription asks for the extent's lon/lat bounding box (padded by
BBOX_PAD); each batch is then clipped exactly to the extent polygon in EPSG:2961
before it is written. Positions are buffered and flushed every FLUSH_SECONDS or
FLUSH_ROWS, whichever comes first, each flush adding one file per date (UTC).
AIS "not available" values (sog 102.3, cog 360, heading 511, rot -128) are
stored as NULL.

Position reports carry no ship type, so the static data messages are kept too,
one row per vessel with its latest values, rewritten on each flush that has any:
  Vessels  AIS_DIR/vessels.parquet
           mmsi, ship_type (AIS type code, e.g. 52 tug, 70-79 cargo), call_sign,
           imo, updated_at
Class A vessels send theirs (ShipStaticData) every 6 minutes, class B ones
(StaticDataReport part B) less often; type 0 ("not available") is stored as NULL.

Read everything back with
  SELECT * FROM read_parquet('AIS_DIR/positions/**/*.parquet', hive_partitioning = true)

compact_day merges one day's flush files into a single file in a separate folder,
sorted by mmsi then ts so each vessel's track is contiguous:
  Daily    AIS_DIR/positions_daily/date=YYYY-MM-DD/positions.parquet
analyze_day then reads that file and writes the day's berth occupancy, one row
per vessel and berth (see analyze_day for the definitions):
  Analysis AIS_DIR/analysis_daily/date=YYYY-MM-DD/analysis.parquet
           mmsi, ship_name, berth_name, dwell_time (whole hours), lon, lat (EPSG:4326)
Berths are the named polygons in DATA_DIR/berth.gpkg. Run both from the command
line with --compact [YYYY-MM-DD] (default yesterday, UTC), or on AWS Lambda
with compact_handler.

AIS_DIR is DATA_DIR/ais, or an S3 prefix such as s3://kieser-public/ais when the
AIS_DIR environment variable says so (see storage.py). deploy/ runs the consumer
on ECS Fargate and compact_handler daily on Lambda, both writing to S3.

Needs an aisstream.io API key in AISSTREAM_API_KEY, plus duckdb and websockets
(pip install duckdb websockets, and boto3 for S3). Stop with Ctrl+C or SIGTERM;
the buffer is flushed on the way out.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import time
from datetime import date, datetime, timedelta, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import storage
from load_rasters import DATA_DIR
from storage import AIS_DIR

EXTENT_PATH = DATA_DIR / "extent.gpkg"
POSITIONS_DIR = storage.join(AIS_DIR, "positions")
DAILY_DIR = storage.join(AIS_DIR, "positions_daily")
BERTH_PATH = DATA_DIR / "berth.gpkg"
ANALYSIS_DIR = storage.join(AIS_DIR, "analysis_daily")
VESSELS_PATH = storage.join(AIS_DIR, "vessels.parquet")
CRS = "EPSG:2961"

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport",
                 "ExtendedClassBPositionReport"]
STATIC_TYPES = ["ShipStaticData", "StaticDataReport"]
BBOX_PAD = 0.01        # deg added around the extent's lon/lat bounding box
FLUSH_SECONDS = 60
FLUSH_ROWS = 5000
MOORED_SOG = 1.0      # kn; slower positions inside a berth count as occupying it

log = logging.getLogger("ais_consumer")

# Staging columns, in the order parse_position returns them
STAGING_COLUMNS = """
    mmsi BIGINT, ship_name VARCHAR, message_type VARCHAR, ts TIMESTAMPTZ,
    received_at TIMESTAMPTZ, lon DOUBLE, lat DOUBLE, sog DOUBLE, cog DOUBLE,
    heading SMALLINT, nav_status SMALLINT, rot SMALLINT, position_accuracy BOOLEAN
"""
VESSEL_COLUMNS = """
    mmsi BIGINT PRIMARY KEY, ship_type SMALLINT, call_sign VARCHAR, imo BIGINT,
    updated_at TIMESTAMPTZ
"""
TIME_UTC = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.(\d+))?")


def parse_time_utc(text):
    """aisstream time, e.g. '2026-10-21 14:03:07.123456789 +0000 UTC', as UTC datetime."""
    m = TIME_UTC.match(text or "")
    if not m:
        return None
    ts = datetime.strptime(m[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    frac = (m[2] or "").ljust(6, "0")[:6]
    return ts.replace(microsecond=int(frac))


def _available(value, missing):
    return None if value is None or value == missing else value


def parse_position(msg, received_at):
    """Staging row for one aisstream position message, or None if unusable."""
    kind = msg.get("MessageType")
    report = msg.get("Message", {}).get(kind)
    meta = msg.get("MetaData", {})
    if kind not in MESSAGE_TYPES or not report or not report.get("Valid", True):
        return None
    lon, lat = report.get("Longitude"), report.get("Latitude")
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return (
        report.get("UserID") or meta.get("MMSI"),
        (meta.get("ShipName") or "").strip() or None,
        kind,
        parse_time_utc(meta.get("time_utc")) or received_at,
        received_at,
        lon, lat,
        _available(report.get("Sog"), 102.3),
        _available(report.get("Cog"), 360),
        _available(report.get("TrueHeading"), 511),
        report.get("NavigationalStatus"),           # Class A only
        _available(report.get("RateOfTurn"), -128),  # Class A only
        report.get("PositionAccuracy"),
    )


def parse_static(msg, received_at):
    """Vessels row (mmsi, ship_type, call_sign, imo, updated_at) for one aisstream
    static data message, or None if it has no ship type."""
    kind = msg.get("MessageType")
    report = msg.get("Message", {}).get(kind)
    if kind not in STATIC_TYPES or not report or not report.get("Valid", True):
        return None
    if kind == "StaticDataReport":  # class B, sent in two parts; part B has the type
        part_b = report.get("ReportB") or {}
        if not report.get("PartNumber") or not part_b.get("Valid", True):
            return None
        ship_type, call_sign, imo = part_b.get("ShipType"), part_b.get("CallSign"), None
    else:
        ship_type, call_sign, imo = report.get("Type"), report.get("CallSign"), report.get("ImoNumber")
    if not ship_type:
        return None
    return (report.get("UserID") or msg.get("MetaData", {}).get("MMSI"), ship_type,
            (call_sign or "").strip() or None, imo or None, received_at)


class PositionStore:
    """Buffers positions and appends them to partitioned GeoParquet via DuckDB."""

    def __init__(self, extent_path=EXTENT_PATH, out_dir=POSITIONS_DIR, vessels_path=None):
        self.out_dir = out_dir
        # VESSELS_PATH by default, i.e. beside the positions folder
        self.vessels_path = vessels_path or storage.sibling(out_dir, storage.basename(VESSELS_PATH))
        self.rows, self.static_rows = [], []
        self.last_flush = time.monotonic()
        self.con = storage.connect(out_dir, self.vessels_path)
        self.con.execute(f"CREATE TABLE staging ({STAGING_COLUMNS})")
        self.con.execute(
            "CREATE TABLE extent AS SELECT ST_Union_Agg(geom) AS geom FROM ST_Read(?)",
            [str(extent_path)])
        self.con.execute(f"CREATE TABLE vessels ({VESSEL_COLUMNS})")
        if storage.exists(self.vessels_path):
            self.con.execute(
                f"INSERT INTO vessels SELECT * FROM read_parquet('{self.vessels_path}')")

    def bbox(self, pad=BBOX_PAD):
        """Extent bounding box in lon/lat as (west, south, east, north)."""
        return self.con.execute(f"""
            SELECT ST_XMin(g) - {pad}, ST_YMin(g) - {pad}, ST_XMax(g) + {pad}, ST_YMax(g) + {pad}
            FROM (SELECT ST_Transform(geom, '{CRS}', 'EPSG:4326', always_xy := true) AS g
                  FROM extent)""").fetchone()

    def add(self, row):
        self.rows.append(row)

    def add_static(self, row):
        self.static_rows.append(row)

    def due(self):
        return (len(self.rows) >= FLUSH_ROWS
                or time.monotonic() - self.last_flush >= FLUSH_SECONDS)

    def flush(self):
        """Write buffered positions inside the extent. Returns (received, written)."""
        rows, self.rows = self.rows, []
        self.last_flush = time.monotonic()
        self.flush_vessels()
        if not rows:
            return 0, 0
        self.con.executemany(f"INSERT INTO staging VALUES ({', '.join('?' * 13)})", rows)
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE batch AS
            SELECT s.* EXCLUDE (utm),
                   ST_SetCRS(ST_Point(lon, lat), 'EPSG:4326') AS geometry,
                   CAST(ts AS DATE) AS date
            FROM (SELECT *, ST_Transform(ST_Point(lon, lat), 'EPSG:4326', '{CRS}',
                                         always_xy := true) AS utm
                  FROM staging) s, extent e
            WHERE ST_Within(s.utm, e.geom)
            ORDER BY ts""")
        self.con.execute("DELETE FROM staging")
        written = self.con.execute("SELECT count(*) FROM batch").fetchone()[0]
        if written:
            if not storage.is_s3(self.out_dir):
                self.out_dir.mkdir(parents=True, exist_ok=True)
            self.con.execute(f"""
                COPY batch TO '{self.out_dir}'
                (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (date), APPEND,
                 FILENAME_PATTERN 'positions_{{uuid}}')""")
        return len(rows), written

    def flush_vessels(self):
        """Merge buffered static data into vessels_path. Returns the number of messages."""
        static_rows, self.static_rows = self.static_rows, []
        if not static_rows:
            return 0
        self.con.executemany("INSERT OR REPLACE INTO vessels VALUES (?, ?, ?, ?, ?)", static_rows)
        storage.copy_to(self.con, "SELECT * FROM vessels ORDER BY mmsi", self.vessels_path)
        return len(static_rows)

    def close(self):
        self.con.close()


def compact_day(day, src_dir=POSITIONS_DIR, out_dir=DAILY_DIR):
    """Merge one UTC day's flush files into out_dir/date=YYYY-MM-DD/positions.parquet.

    The flush files are left in place, and rerunning replaces the merged file.
    Run it on completed days only; today's partition is still being written to.
    Returns the number of rows merged, 0 if the day has no files.
    """
    day = date.fromisoformat(str(day))
    if not storage.list_files(storage.join(src_dir, f"date={day}"), ".parquet"):
        return 0
    dst = storage.join(out_dir, f"date={day}", "positions.parquet")
    con = storage.connect(src_dir, dst)
    try:
        return storage.copy_to(con, f"""
            SELECT * FROM read_parquet('{src_dir}/date={day}/*.parquet') ORDER BY mmsi, ts""",
            dst, "FORMAT parquet, COMPRESSION zstd")
    finally:
        con.close()


def analyze_day(day, daily_dir=DAILY_DIR, berth_path=BERTH_PATH, out_dir=ANALYSIS_DIR):
    """Berth occupancy for one UTC day, from compact_day's positions.parquet.

    Writes out_dir/date=YYYY-MM-DD/analysis.parquet, one row per vessel and berth
    it occupied that day:
      mmsi, ship_name   the vessel, with the name it reported most often that day
      berth_name        the berth polygon's name
      dwell_time        whole UTC clock hours (0-24) with a position in the berth
      lon, lat          mean position in the berth, EPSG:4326
    A position occupies a berth when it lies inside the polygon and the vessel is
    stationary (sog below MOORED_SOG, or not reported), so ships passing through
    a berth area are not counted. Positions are projected to CRS (the berths'
    CRS) for the in-berth test.

    Rerunning replaces the file. Returns the number of rows, or 0 with nothing
    written if the day has no positions.parquet.
    """
    day = date.fromisoformat(str(day))
    src = storage.join(daily_dir, f"date={day}", "positions.parquet")
    if not storage.exists(src):
        return 0
    dst = storage.join(out_dir, f"date={day}", "analysis.parquet")
    con = storage.connect(src, dst)
    try:
        # The geometry column is named by whoever wrote the GeoPackage (geom, geometry, ...)
        geom = con.execute("""
            SELECT column_name FROM (DESCRIBE SELECT * FROM ST_Read(?))
            WHERE column_type LIKE 'GEOMETRY%'""", [str(berth_path)]).fetchone()[0]
        return storage.copy_to(con, f"""
            WITH berth AS (SELECT name, "{geom}" AS geom FROM ST_Read('{berth_path}')),
            pos AS (SELECT * FROM read_parquet('{src}')),
            names AS (SELECT mmsi, mode(ship_name) AS ship_name FROM pos GROUP BY mmsi),
            moored AS (
                SELECT p.mmsi, b.name AS berth_name, p.ts, p.lon, p.lat
                FROM pos p JOIN berth b ON ST_Within(
                    ST_Transform(ST_Point(p.lon, p.lat), 'EPSG:4326', '{CRS}',
                                 always_xy := true), b.geom)
                WHERE p.sog IS NULL OR p.sog < {MOORED_SOG})
            SELECT mmsi, n.ship_name, berth_name,
                   CAST(count(DISTINCT hour(ts)) AS INTEGER) AS dwell_time,
                   avg(lon) AS lon, avg(lat) AS lat
            FROM moored JOIN names n USING (mmsi)
            GROUP BY ALL
            ORDER BY berth_name, mmsi""", dst, "FORMAT parquet, COMPRESSION zstd")
    finally:
        con.close()


def compact(day):
    """compact_day then analyze_day for one UTC day. Returns both row counts."""
    n_positions = compact_day(day)
    log.info("merged %d positions for %s into %s", n_positions, day, DAILY_DIR)
    n_occupancies = analyze_day(day)
    log.info("found %d vessel berth occupancies, written to %s", n_occupancies, ANALYSIS_DIR)
    return n_positions, n_occupancies


def yesterday():
    return str(datetime.now(timezone.utc).date() - timedelta(days=1))


def compact_handler(event, context):
    """AWS Lambda entry point: compact {"day": "YYYY-MM-DD"}, by default yesterday (UTC)."""
    logging.getLogger().setLevel(logging.INFO)
    day = (event or {}).get("day") or yesterday()
    n_positions, n_occupancies = compact(day)
    return {"day": day, "positions": n_positions, "occupancies": n_occupancies}


def subscription(api_key, bbox):
    west, south, east, north = bbox
    return {"APIKey": api_key,
            "BoundingBoxes": [[[south, west], [north, east]]],  # [lat, lon] corners
            "FilterMessageTypes": MESSAGE_TYPES + STATIC_TYPES}


def flush(store):
    received, written = store.flush()
    if received:
        log.info("flushed %d positions, %d inside the extent", received, written)


async def consume(store, url, api_key):
    """Stream positions into the store, reconnecting with backoff on network errors."""
    sub = json.dumps(subscription(api_key, store.bbox()))
    async for ws in connect(url):
        try:
            # aisstream drops connections not subscribed within 3 s
            await ws.send(sub)
            log.info("subscribed to %s", url)
            async for raw in ws:
                msg = json.loads(raw)
                if "error" in msg:
                    raise RuntimeError(f"aisstream: {msg['error']}")
                received_at = datetime.now(timezone.utc)
                if row := parse_position(msg, received_at):
                    store.add(row)
                elif row := parse_static(msg, received_at):
                    store.add_static(row)
                if store.due():
                    flush(store)
        except ConnectionClosed as exc:
            log.warning("connection closed (%s), reconnecting", exc)


async def flush_periodically(store):
    """Flush on time even when no messages arrive."""
    while True:
        await asyncio.sleep(5)
        if store.due():
            flush(store)


async def main(url, api_key):
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, asyncio.current_task().cancel)
    store = PositionStore()
    log.info("extent bbox (lon/lat): %s", ", ".join(f"{v:.4f}" for v in store.bbox(0)))
    ticker = asyncio.create_task(flush_periodically(store))
    try:
        await consume(store, url, api_key)
    finally:
        ticker.cancel()
        flush(store)
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=AISSTREAM_URL)
    parser.add_argument("--compact", nargs="?", metavar="YYYY-MM-DD", const=yesterday(),
                        help="merge one day's files into DAILY_DIR, analyze berth "
                             "occupancy into ANALYSIS_DIR and exit (default yesterday, UTC)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.compact:
        compact(args.compact)
        raise SystemExit
    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        parser.error("set AISSTREAM_API_KEY (free key from https://aisstream.io)")
    try:
        asyncio.run(main(args.url, api_key))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    print(f"Positions in {POSITIONS_DIR}")
