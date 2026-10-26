"""
Serve the Port and Coastal Operations Twin: a CesiumJS app with Live, Replay
and Flood panels, plus the three live websocket feeds it reads.

  App      http://HOST:HTTP_PORT/                twin_app/ (index.html, app.js, style.css)
  Terrain  http://HOST:HTTP_PORT/terrain/...     DATA_DIR/terrain (from export_terrain.py)
  Tiles    http://HOST:HTTP_PORT/buildings/...   DATA_DIR/buildings_3dtiles
                                                  (from export_buildings_3dtiles.py)
  Config   http://HOST:HTTP_PORT/api/config      extent, datums, feed port and paths, and
                                                  which of terrain and buildings exist, as JSON
  Replay   http://HOST:HTTP_PORT/api/replay?days=7   berth occupancy per day, as JSON
  Feeds    ws://HOST:FEED_PORT/vessels      vessels            ais_live.py, reading DATA_DIR/ais
           ws://HOST:FEED_PORT/tide         tide level         tide_live.py, reading DATA_DIR/tide
           ws://HOST:FEED_PORT/storm_surge  storm surge risk   storm_surge_live.py, reading
                                                               DATA_DIR/coastal_flooding

All three feeds share one websocket port and are told apart by path, so a TLS proxy in
front of the app (for https:// and wss://) needs just the HTTP port and FEED_PORT. Any
other path is refused with 404 during the handshake.

The consumers (ais_consumer.py, tide_consumer.py, storm_surge_consumer.py) run
separately and write the files the feeds read. /api/replay reads the
analysis_daily files that ais_consumer.py --compact writes, for the last `days`
UTC days up to today:
  {"days": [{"date": "2026-10-22", "analysed": true,
             "vessels": [{"mmsi", "ship_name", "ship_type", "berth_name",
                          "dwell_time", "lon", "lat"}, ...]}, ...]}
oldest first, with "analysed": false and no vessels for a day without a file.

The Flood panel puts its water plane at the tide level, which is metres above
chart datum. /api/config gives the offsets that place it with the terrain and
buildings: CHART_DATUM_CGVD2013 is chart datum zero at Halifax in CGVD2013,
the same offset load_rasters.py applies to the NONNA-10 bathymetry, and
CGVD2013_TO_WGS84 is the terrain's constant shift to the ellipsoid. A building is flooded when its z_bottom (ground elevation, CGVD2013)
is at or below the water.

Open http://localhost:8000. Needs duckdb and websockets (pip install duckdb
websockets); the page loads CesiumJS from a CDN.
"""

import argparse
import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import duckdb
from websockets.asyncio.server import serve

from ais_consumer import ANALYSIS_DIR, CRS, EXTENT_PATH, VESSELS_PATH
from ais_live import live_positions
from load_rasters import CD_TO_CGVD2013, DATA_DIR
from storm_surge_live import live_storm_surge
from tide_consumer import STATION_CODE
from tide_live import live_tide

HOST = "localhost"
HTTP_PORT = 8000
FEED_PORT = 8765
FEED_PATHS = {"vessels": "/vessels", "tide": "/tide", "storm_surge": "/storm_surge"}
REPLAY_DAYS = 7

APP_DIR = Path(__file__).with_name("twin_app")
TERRAIN_DIR = DATA_DIR / "terrain"              # export_terrain.TERRAIN_DIR
TILESET_DIR = DATA_DIR / "buildings_3dtiles"    # export_buildings_3dtiles.TILESET_DIR

# Same constant as export_terrain.py, which needs rasterio to import
CGVD2013_TO_WGS84 = -20.270
CHART_DATUM_CGVD2013 = CD_TO_CGVD2013   # -1.41 m

log = logging.getLogger("twin_app")

REPLAY = """
    SELECT CAST(a.date AS VARCHAR) AS date, a.mmsi, a.ship_name, v.ship_type,
           a.berth_name, a.dwell_time, a.lon, a.lat
    FROM read_parquet($paths, hive_partitioning = true) a LEFT JOIN {vessels} v USING (mmsi)
    ORDER BY a.date, a.berth_name, a.mmsi
"""
NO_VESSELS = "(SELECT NULL::BIGINT AS mmsi, NULL::SMALLINT AS ship_type WHERE false)"
REPLAY_FIELDS = ["mmsi", "ship_name", "ship_type", "berth_name", "dwell_time", "lon", "lat"]


def extent_bbox(extent_path=EXTENT_PATH):
    """Extent bounding box in lon/lat as [west, south, east, north]."""
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial")
        return list(con.execute(f"""
            SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g)
            FROM (SELECT ST_Transform(ST_Union_Agg(geom), '{CRS}', 'EPSG:4326',
                                      always_xy := true) AS g
                  FROM ST_Read(?))""", [str(extent_path)]).fetchone())
    finally:
        con.close()


def app_config(bbox, feed_port=FEED_PORT):
    return {"extent": bbox, "station": STATION_CODE,
            "feed_port": feed_port, "feeds": FEED_PATHS,
            "datums": {"chart_datum_cgvd2013": CHART_DATUM_CGVD2013,
                       "cgvd2013_to_wgs84": CGVD2013_TO_WGS84}}


def replay_days(today, days=REPLAY_DAYS, analysis_dir=ANALYSIS_DIR, vessels_path=VESSELS_PATH):
    """Berth occupancy for the `days` UTC days up to today, oldest first (see the docstring)."""
    dates = [str(today - timedelta(days=i)) for i in reversed(range(days))]
    paths = [p for d in dates if (p := analysis_dir / f"date={d}" / "analysis.parquet").exists()]
    by_date = {d: [] for d in dates}
    if paths:
        vessels = (f"read_parquet('{vessels_path}')" if vessels_path.exists() else NO_VESSELS)
        con = duckdb.connect()
        try:
            rows = con.execute(REPLAY.format(vessels=vessels),
                               {"paths": [str(p) for p in paths]}).fetchall()
        finally:
            con.close()
        for date, *row in rows:
            by_date[date].append(dict(zip(REPLAY_FIELDS, row)))
    analysed = {p.parent.name.removeprefix("date=") for p in paths}
    return {"days": [{"date": d, "analysed": d in analysed, "vessels": by_date[d]}
                     for d in dates]}


class TwinHandler(SimpleHTTPRequestHandler):
    """The app's static files, terrain and tiles from their folders, and the two API routes."""

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".terrain": "application/vnd.quantized-mesh",
                      ".glb": "model/gltf-binary", ".json": "application/json",
                      ".js": "text/javascript"}

    def __init__(self, *args, config, routes, analysis_dir, vessels_path, **kwargs):
        self.config, self.routes = config, routes
        self.analysis_dir, self.vessels_path = analysis_dir, vessels_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/config":
            return self.send_json({**self.config, "layers": self.layers()})
        if url.path == "/api/replay":
            try:
                days = min(max(int(parse_qs(url.query).get("days", [REPLAY_DAYS])[0]), 1), 31)
            except ValueError:
                return self.send_error(HTTPStatus.BAD_REQUEST, "days must be a whole number")
            today = datetime.now(timezone.utc).date()
            return self.send_json(replay_days(today, days, self.analysis_dir, self.vessels_path))
        return super().do_GET()

    def layers(self):
        """Whether the terrain and building tiles have been built, checked on every request."""
        return {"terrain": (self.routes["/terrain/"] / "layer.json").is_file(),
                "buildings": (self.routes["/buildings/"] / "tileset.json").is_file()}

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def translate_path(self, path):
        """Map /terrain/, /buildings/ and / to their folders; anything outside them is not found."""
        path = unquote(urlsplit(path).path)
        for prefix, root in self.routes.items():
            if path.startswith(prefix):
                root = root.resolve()
                target = (root / path[len(prefix):]).resolve()
                return str(target) if target.is_relative_to(root) else ""
        return ""

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format, *args):  # terrain and tiles make hundreds of requests
        log.debug(format, *args)


def start_http(host=HOST, port=HTTP_PORT, config=None, app_dir=APP_DIR, terrain_dir=TERRAIN_DIR,
               tileset_dir=TILESET_DIR, analysis_dir=ANALYSIS_DIR, vessels_path=VESSELS_PATH):
    """Start the HTTP server in a background thread and return it (call .shutdown() to stop)."""
    routes = {"/terrain/": terrain_dir, "/buildings/": tileset_dir, "/": app_dir}
    handler = partial(TwinHandler, config=config or app_config(extent_bbox()), routes=routes,
                      analysis_dir=analysis_dir, vessels_path=vessels_path)
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def serve_feeds(host, port, feeds):
    """Serve each (handler, poll) feed in `feeds` at its path on ws://host:port until cancelled."""
    def process_request(connection, request):
        if urlsplit(request.path).path not in feeds:
            return connection.respond(HTTPStatus.NOT_FOUND, "No feed at this path\n")

    async def route(ws):
        handler, _ = feeds[urlsplit(ws.request.path).path]
        await handler(ws)

    async with serve(route, host, port, process_request=process_request):
        log.info("feeds on ws://%s:%d%s", host, port, ", ".join(feeds))
        await asyncio.gather(*(poll() for _, poll in feeds.values()))


async def serve_twin(host=HOST, http_port=HTTP_PORT, feed_port=FEED_PORT):
    """Serve the app over HTTP and the three feeds over one websocket port until cancelled."""
    server = start_http(host, http_port, config=app_config(extent_bbox(), feed_port))
    log.info("twin app on http://%s:%d", host, http_port)
    feeds = {FEED_PATHS["vessels"]: live_positions(),
             FEED_PATHS["tide"]: live_tide(),
             FEED_PATHS["storm_surge"]: live_storm_surge()}
    try:
        await serve_feeds(host, feed_port, feeds)
    finally:
        server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=HTTP_PORT, help="HTTP port for the app")
    parser.add_argument("--feed-port", type=int, default=FEED_PORT,
                        help="websocket port for the three feeds")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve_twin(args.host, args.port, args.feed_port))
    except KeyboardInterrupt:
        pass
