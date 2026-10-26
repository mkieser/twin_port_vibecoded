"""
Push live vessel positions to browsers over a websocket.

  Input    the most recent files in DATA_DIR/ais/positions (from ais_consumer.py)
  Output   ws://HOST:PORT, one GeoJSON FeatureCollection per new file

The positions folder is polled every POLL_SECONDS. When a newer .parquet file
appears (the consumer writes one per flush), it and every file written up to
WINDOW_SECONDS before it are read, so moored ships that report only every few
minutes stay in the feed. Each vessel's latest position across those files is
broadcast to every connected browser as a GeoJSON FeatureCollection: Point
geometry in lon/lat, properties mmsi, ship_name, ship_type, ts (ISO 8601 UTC),
sog, cog, heading and nav_status. ship_type is the AIS type code from
DATA_DIR/ais/vessels.parquet, null until the vessel's static data has arrived.
A browser that connects gets the current positions straight away, then each
update. The twin app (twin_app.py) shows them on its Live panel.

To use the feed elsewhere, e.g. in Cesium:
  const ws = new WebSocket("ws://localhost:8765");
  ws.onmessage = async (e) => {
    vessels.entities.removeAll();
    await vessels.load(JSON.parse(e.data));   // vessels: a Cesium.GeoJsonDataSource
  };

Needs duckdb and websockets (pip install duckdb websockets).
"""

import asyncio
import json
import logging

import duckdb
from websockets.asyncio.server import broadcast, serve

from ais_consumer import POSITIONS_DIR, VESSELS_PATH

HOST = "localhost"
PORT = 8765
POLL_SECONDS = 5
WINDOW_SECONDS = 600   # files this much older than the newest are still read

log = logging.getLogger("ais_live")

# Latest position per vessel, with its ship type from {vessels}
LATEST_POSITIONS = """
    SELECT p.mmsi, ship_name, v.ship_type, strftime(ts, '%Y-%m-%dT%H:%M:%S.%fZ') AS ts,
           sog, cog, heading, nav_status, lon, lat
    FROM read_parquet($paths) p LEFT JOIN {vessels} v USING (mmsi)
    QUALIFY row_number() OVER (PARTITION BY p.mmsi ORDER BY ts DESC) = 1
    ORDER BY p.mmsi
"""
NO_VESSELS = "(SELECT NULL::BIGINT AS mmsi, NULL::SMALLINT AS ship_type WHERE false)"
PROPERTIES = ["mmsi", "ship_name", "ship_type", "ts", "sog", "cog", "heading", "nav_status"]


def recent_files(positions_dir=POSITIONS_DIR, window_seconds=WINDOW_SECONDS):
    """Positions files written within window_seconds of the newest one, newest first.

    The window is relative to the newest file, not the clock, so the last known
    positions are still served if the consumer stops.
    """
    files = sorted(((f.stat().st_mtime, f) for f in positions_dir.glob("date=*/*.parquet")),
                   reverse=True)
    if not files:
        return []
    return [f for mtime, f in files if mtime >= files[0][0] - window_seconds]


def positions_geojson(con, paths, vessels_path=VESSELS_PATH):
    """Each vessel's latest position across positions files, as a GeoJSON FeatureCollection."""
    vessels = (f"read_parquet('{vessels_path}')" if vessels_path.exists() else NO_VESSELS)
    rows = con.execute(LATEST_POSITIONS.format(vessels=vessels),
                       {"paths": [str(p) for p in paths]}).fetchall()
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": dict(zip(PROPERTIES, props))}
        for *props, lon, lat in rows]}


def live_positions(positions_dir=POSITIONS_DIR, poll_seconds=POLL_SECONDS, vessels_path=None):
    """The vessels feed as (handler, poll): handler(ws) serves one browser and poll() pushes
    the recent positions files' vessels to every connected browser until cancelled.

    vessels_path defaults to vessels.parquet beside positions_dir, as the consumer writes it.
    """
    vessels_path = vessels_path or positions_dir.parent / VESSELS_PATH.name
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    current = {"file": None, "message": None}
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
            paths = recent_files(positions_dir)
            if paths and paths[0] != current["file"]:
                try:
                    collection = positions_geojson(con, paths, vessels_path)
                except duckdb.Error as exc:  # e.g. the consumer is still writing one
                    log.warning("skipping %s for now: %s", paths[0].name, exc)
                else:
                    current.update(file=paths[0], message=json.dumps(collection))
                    broadcast(clients, current["message"])
                    log.info("pushed %d vessels from %d files to %d browsers",
                             len(collection["features"]), len(paths), len(clients))
            await asyncio.sleep(poll_seconds)

    return handler, poll


async def serve_live_positions(host=HOST, port=PORT, positions_dir=POSITIONS_DIR,
                               poll_seconds=POLL_SECONDS, vessels_path=None):
    """Serve the vessels feed on ws://host:port on its own (twin_app.py serves it at /vessels)."""
    handler, poll = live_positions(positions_dir, poll_seconds, vessels_path)
    async with serve(handler, host, port):
        log.info("serving live positions on ws://%s:%d", host, port)
        await poll()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve_live_positions())
    except KeyboardInterrupt:
        pass
