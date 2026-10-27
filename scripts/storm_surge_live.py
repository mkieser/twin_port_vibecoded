"""
Push the current storm surge flooding risk to browsers over a websocket.

  Input    FLOODING_DIR/latest.json and the snapshot it names (from
           storm_surge_consumer.py), a local folder or S3 (see storage.py)
  Output   ws://HOST:PORT, one GeoJSON FeatureCollection per change

latest.json is polled every POLL_SECONDS. The current snapshot's features where
storm surge drives the risk and whose forecast period has not ended are sent as
a GeoJSON FeatureCollection: Polygon geometry in lon/lat, properties id,
publication_datetime, validity_datetime, expiration_datetime (ISO 8601 UTC),
risk, impact, likelihood, tide and waves. The collection's fetched_at member is
when the consumer last polled, so a browser can tell a quiet day (no features,
recent fetched_at) from a stopped consumer (old fetched_at). It is sent again
whenever it changes, i.e. after every poll, and a browser that connects gets the
current collection straight away.

In the browser, e.g. with Cesium:
  const ws = new WebSocket("ws://localhost:8767");
  ws.onmessage = async (e) => {
    surge.entities.removeAll();
    await surge.load(JSON.parse(e.data));   // surge: a Cesium.GeoJsonDataSource
  };

Needs duckdb and websockets (pip install duckdb websockets, and boto3 for S3).
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import duckdb
from websockets.asyncio.server import broadcast, serve

import storage
from storm_surge_consumer import LATEST_PATH, SNAPSHOTS_DIR, read_latest

HOST = "localhost"
PORT = 8767            # ais_live.py uses 8765, tide_live.py 8766
POLL_SECONDS = 10

log = logging.getLogger("storm_surge_live")

ISO = "'%Y-%m-%dT%H:%M:%SZ'"
STORM_SURGE = f"""
    SELECT id,
           strftime(publication_datetime AT TIME ZONE 'UTC', {ISO}) AS publication_datetime,
           strftime(validity_datetime AT TIME ZONE 'UTC', {ISO}) AS validity_datetime,
           strftime(expiration_datetime AT TIME ZONE 'UTC', {ISO}) AS expiration_datetime,
           risk, impact, likelihood, tide, waves, ST_AsGeoJSON(geometry) AS geometry
    FROM read_parquet($path)
    WHERE storm_surge AND expiration_datetime > $now
    ORDER BY validity_datetime, id
"""
PROPERTIES = ["id", "publication_datetime", "validity_datetime", "expiration_datetime",
              "risk", "impact", "likelihood", "tide", "waves"]


def storm_surge_geojson(con, now, latest_path=LATEST_PATH, snapshots_dir=SNAPSHOTS_DIR):
    """Current storm surge risk as a GeoJSON FeatureCollection, or None before the first poll."""
    latest = read_latest(latest_path)
    if latest is None:
        return None
    rows = []
    if latest["snapshot"]:
        path = storage.join(snapshots_dir, latest["snapshot"])
        rows = con.execute(STORM_SURGE, {"path": str(path), "now": now}).fetchall()
    return {"type": "FeatureCollection", "fetched_at": latest["fetched_at"], "features": [
        {"type": "Feature", "geometry": json.loads(geometry),
         "properties": dict(zip(PROPERTIES, props))}
        for *props, geometry in rows]}


def live_storm_surge(latest_path=LATEST_PATH, snapshots_dir=SNAPSHOTS_DIR,
                     poll_seconds=POLL_SECONDS):
    """The storm surge feed as (handler, poll): handler(ws) serves one browser and poll()
    pushes the current storm surge risk to every connected browser until cancelled."""
    con = storage.connect(snapshots_dir)
    current = {"message": None}
    clients = set()

    async def handler(ws):
        log.info("browser connected from %s", ws.remote_address)
        clients.add(ws)
        try:
            if current["message"]:
                await ws.send(current["message"])
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    async def poll():
        while True:
            try:
                collection = storm_surge_geojson(con, datetime.now(timezone.utc),
                                                 latest_path, snapshots_dir)
            except (duckdb.Error, OSError, ValueError) as exc:  # e.g. mid-rewrite
                log.warning("skipping this poll: %s", exc)
            else:
                message = collection and json.dumps(collection)
                if message and message != current["message"]:
                    current["message"] = message
                    broadcast(clients, message)
                    log.info("pushed %d storm surge features (fetched %s) to %d browsers",
                             len(collection["features"]), collection["fetched_at"],
                             len(clients))
            await asyncio.sleep(poll_seconds)

    return handler, poll


async def serve_live_storm_surge(host=HOST, port=PORT, latest_path=LATEST_PATH,
                                 snapshots_dir=SNAPSHOTS_DIR, poll_seconds=POLL_SECONDS):
    """Serve the storm surge feed on ws://host:port on its own (twin_app.py serves it at
    /storm_surge)."""
    handler, poll = live_storm_surge(latest_path, snapshots_dir, poll_seconds)
    async with serve(handler, host, port):
        log.info("serving live storm surge risk on ws://%s:%d", host, port)
        await poll()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve_live_storm_surge())
    except KeyboardInterrupt:
        pass
