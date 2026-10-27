"""
Poll the MSC GeoMet coastal flooding risk index and store the features inside
the project extent as GeoParquet, written with DuckDB.

  Source   https://api.weather.gc.ca/collections/coastal-flooding-risk-index/items
  Extent   DATA_DIR/extent.gpkg  (EPSG:2961 polygon, the 10 km data square)
  Output   FLOODING_DIR/risk_index/risk_index_YYYYMMDDTHHMMZ.parquet
           GeoParquet 1.0, polygon geometry in EPSG:4326, one file per change
  Current  FLOODING_DIR/latest.json, which snapshot is current

The index does not give surge heights. Each feature is a grid cell polygon,
issued daily by the Storm Prediction Centres for forecast periods out to 5 days,
saying how likely coastal flooding is and how bad it would be, and whether storm
surge, tide and/or waves drive it. Columns:

  id                     feature id (a new id for each issue)
  publication_datetime   when the product was issued, UTC
  validity_datetime      start of the forecast period, UTC
  expiration_datetime    end of the forecast period, UTC
  amendment              0 for the original issue, counting up per amendment
  product_type           issuing centre, e.g. ASPC (Atlantic)
  product_sub_type       region, e.g. Maritimes
  status                 e.g. final
  storm_surge, tide, waves   BOOLEAN, which drive the flooding risk
  risk, impact, likelihood   index levels (higher is worse or more likely)
  sub_type               MSC product sub type
  fetched_at             when the snapshot was fetched, UTC
  geometry               EPSG:4326

Every POLL_SECONDS the items in the extent's lon/lat bounding box are requested
and clipped exactly to the extent polygon in EPSG:2961; whole cells are kept,
not cut at the edge. A snapshot file is written only when the set of features
(id and amendment) differs from the current one, since products change about
once a day. When the extent has no features, no file is written (an empty file
cannot carry GeoParquet metadata); latest.json then has "snapshot": null.
latest.json is rewritten after every successful poll:
  {"fetched_at": "2026-10-22T12:00:00Z", "snapshot": "risk_index_...parquet" | null,
   "features": [[id, amendment], ...]}

The extent is usually empty: risk is only issued where flooding is possible.
Read the history back with
  SELECT * FROM read_parquet('FLOODING_DIR/risk_index/*.parquet')

FLOODING_DIR is DATA_DIR/coastal_flooding, or an S3 prefix such as
s3://kieser-public/coastal_flooding when the FLOODING_DIR environment variable
says so (see storage.py). On AWS Lambda, poll_handler does one poll per
invocation instead of looping (see deploy/).

Needs duckdb (pip install duckdb, and boto3 for S3). Stop with Ctrl+C or SIGTERM.
"""

import argparse
import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import storage
from ais_consumer import CRS, EXTENT_PATH
from storage import FLOODING_DIR
from tide_consumer import fetch_json, iso_utc

SNAPSHOTS_DIR = storage.join(FLOODING_DIR, "risk_index")
LATEST_PATH = storage.join(FLOODING_DIR, "latest.json")

ITEMS_URL = "https://api.weather.gc.ca/collections/coastal-flooding-risk-index/items"
POLL_SECONDS = 1800
PAGE_SIZE = 500

log = logging.getLogger("storm_surge_consumer")

# Staging columns, in the order parse_feature returns them
STAGING_COLUMNS = """
    id VARCHAR, publication_datetime TIMESTAMPTZ, validity_datetime TIMESTAMPTZ,
    expiration_datetime TIMESTAMPTZ, amendment INTEGER, product_type VARCHAR,
    product_sub_type VARCHAR, status VARCHAR, storm_surge BOOLEAN, tide BOOLEAN,
    waves BOOLEAN, risk SMALLINT, impact SMALLINT, likelihood SMALLINT,
    sub_type SMALLINT, fetched_at TIMESTAMPTZ, geojson VARCHAR
"""


def metobject(props, name):
    """props['metobject'][name]['value'], also when the API flattens it to 'metobject.name.value'."""
    nested = props.get("metobject", {}).get(name, {})
    value = nested.get("value") if isinstance(nested, dict) else nested
    return value if value is not None else props.get(f"metobject.{name}.value")


def parse_feature(feature, fetched_at):
    """Staging row for one risk index feature, or None without a geometry."""
    props, geometry = feature.get("properties") or {}, feature.get("geometry")
    if not geometry:
        return None
    sub_type = props.get("metobject", {}).get("sub_type", props.get("metobject.sub_type"))
    return (
        feature.get("id") or props.get("id"),
        props.get("publication_datetime"),
        props.get("validity_datetime"),
        props.get("expiration_datetime"),
        props.get("amendment"),
        props.get("product_type"),
        props.get("product_sub_type"),
        props.get("status"),
        metobject(props, "storm_surge"),
        metobject(props, "tide"),
        metobject(props, "waves"),
        metobject(props, "risk"),
        metobject(props, "impact"),
        # The Datamart files call it confidence, the API's queryables likelihood
        metobject(props, "likelihood") if metobject(props, "likelihood") is not None
        else metobject(props, "confidence"),
        sub_type,
        fetched_at,
        json.dumps(geometry),
    )


def fetch_features(bbox, url=ITEMS_URL):
    """All items intersecting a lon/lat bbox, following the API's next links."""
    features = []
    url = f"{url}?{urlencode({'f': 'json', 'limit': PAGE_SIZE, 'bbox': ','.join(map(str, bbox))})}"
    while url:
        page = fetch_json(url)
        features += page.get("features", [])
        url = next((link["href"] for link in page.get("links", []) if link.get("rel") == "next"
                    and page.get("features")), None)
    return features


def read_latest(latest_path=LATEST_PATH):
    """latest.json as a dict, or None before the first poll."""
    return json.loads(storage.read_text(latest_path)) if storage.exists(latest_path) else None


class RiskIndexStore:
    """Clips risk index features to the extent and writes changed snapshots as GeoParquet."""

    def __init__(self, extent_path=EXTENT_PATH, out_dir=SNAPSHOTS_DIR, latest_path=LATEST_PATH):
        self.out_dir = out_dir
        self.latest_path = latest_path
        self.con = storage.connect(out_dir, latest_path)
        self.con.execute(f"CREATE TABLE staging ({STAGING_COLUMNS})")
        self.con.execute(
            "CREATE TABLE extent AS SELECT ST_Union_Agg(geom) AS geom FROM ST_Read(?)",
            [str(extent_path)])

    def bbox(self):
        """Extent bounding box in lon/lat as (west, south, east, north)."""
        return self.con.execute(f"""
            SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g)
            FROM (SELECT ST_Transform(geom, '{CRS}', 'EPSG:4326', always_xy := true) AS g
                  FROM extent)""").fetchone()

    def save(self, rows, fetched_at):
        """Store one poll's rows. Returns the snapshot file written, or None if unchanged
        or nothing is inside the extent (see the "snapshot" in latest.json)."""
        self.con.execute("DELETE FROM staging")
        if rows:
            self.con.executemany(
                f"INSERT INTO staging VALUES ({', '.join('?' * 17)})", rows)
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE batch AS
            SELECT s.* EXCLUDE (geojson, geom),
                   ST_SetCRS(s.geom, 'EPSG:4326') AS geometry
            FROM (SELECT *, ST_GeomFromGeoJSON(geojson) AS geom FROM staging) s, extent e
            WHERE ST_Intersects(ST_Transform(s.geom, 'EPSG:4326', '{CRS}', always_xy := true),
                                e.geom)
            ORDER BY validity_datetime, id""")
        features = [list(key) for key in self.con.execute(
            "SELECT DISTINCT id, amendment FROM batch ORDER BY id, amendment").fetchall()]
        latest = read_latest(self.latest_path) or {"snapshot": None, "features": None}
        written = None
        if features != latest["features"]:
            latest["snapshot"] = None
            if features:
                written = storage.join(self.out_dir,
                                       f"risk_index_{fetched_at:%Y%m%dT%H%MZ}.parquet")
                storage.copy_to(self.con, "SELECT * FROM batch", written,
                                "FORMAT parquet, COMPRESSION zstd")
                latest["snapshot"] = storage.basename(written)
        latest.update(fetched_at=iso_utc(fetched_at), features=features)
        storage.write_text(self.latest_path, json.dumps(latest))
        return written

    def close(self):
        self.con.close()


def poll_once(store, bbox, url=ITEMS_URL):
    """Fetch and store the extent's risk index once. Returns latest.json as a dict."""
    fetched_at = datetime.now(timezone.utc)
    features = fetch_features(bbox, url)
    rows = [row for row in (parse_feature(f, fetched_at) for f in features) if row]
    written = store.save(rows, fetched_at)
    latest = read_latest(store.latest_path)
    if written:
        log.info("wrote %s, %d features in the extent", storage.basename(written),
                 len(latest["features"]))
    else:
        log.info("%d features in the bbox, %d in the extent, %s", len(rows),
                 len(latest["features"]),
                 f"current snapshot {latest['snapshot']}" if latest["snapshot"]
                 else "no flooding risk")
    return latest


def poll(store, stop, url=ITEMS_URL, poll_seconds=POLL_SECONDS):
    """Store the extent's risk index every poll_seconds until stop is set."""
    bbox = store.bbox()
    while not stop.is_set():
        started = time.monotonic()
        try:
            poll_once(store, bbox, url)
        except (OSError, ValueError, KeyError) as exc:  # network, bad JSON, odd payload
            log.warning("fetch failed, retrying next poll: %s", exc)
        stop.wait(poll_seconds - (time.monotonic() - started))


_store = []   # the RiskIndexStore, kept while a Lambda instance stays warm


def poll_handler(event, context):
    """AWS Lambda entry point, run every POLL_SECONDS: one poll. A failed fetch raises,
    so the invocation shows as an error; the next one retries."""
    logging.getLogger().setLevel(logging.INFO)
    if not _store:
        _store.append(RiskIndexStore(EXTENT_PATH, SNAPSHOTS_DIR, LATEST_PATH))
    latest = poll_once(_store[0], _store[0].bbox())
    return {"snapshot": latest["snapshot"], "features": len(latest["features"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=ITEMS_URL)
    parser.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    store = RiskIndexStore()
    log.info("extent bbox (lon/lat): %s, polling every %g s",
             ", ".join(f"{v:.4f}" for v in store.bbox()), args.interval)
    try:
        poll(store, stop, args.url, args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    print(f"Snapshots in {SNAPSHOTS_DIR}, current state in {LATEST_PATH}")
