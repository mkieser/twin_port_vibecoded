"""
Push the most recent tide level to browsers over a websocket.

  Input    the newest file in DATA_DIR/tide/levels (from tide_consumer.py)
  Output   ws://HOST:PORT, one JSON message per new reading

The levels folder is polled every POLL_SECONDS. When the newest reading's ts
changes it is broadcast to every connected browser, and a browser that connects
gets the current reading straight away. Each message looks like
  {"station": "00490", "ts": "2026-10-21T19:25:00Z", "date": "2026-10-21",
   "time": "19:25:00", "tide_level": 1.382, "series": "wlp", "qc_flag": "2",
   "trend": "rising", "change": 0.021, "previous_ts": "2026-10-21T19:20:00Z"}
with ts, date and time in UTC and tide_level in metres above chart datum.
series is 'wlo' for an observed level and 'wlp' for a predicted one. change is
tide_level minus the previous stored reading (m), and trend is 'rising' or
'falling', or 'steady' within STEADY_M. Both are null when there is no previous
reading of the same series within TREND_WINDOW, so a switch between observed and
predicted levels never reads as a trend.

In the browser:
  const ws = new WebSocket("ws://localhost:8766");
  ws.onmessage = (e) => {
    const tide = JSON.parse(e.data);
    label.textContent = `${tide.tide_level.toFixed(2)} m at ${new Date(tide.ts).toLocaleTimeString()}`;
  };

Needs duckdb and websockets (pip install duckdb websockets).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import duckdb
from websockets.asyncio.server import broadcast, serve

from tide_consumer import LEVELS_DIR, STATION_CODE

HOST = "localhost"
PORT = 8766            # ais_live.py uses 8765
POLL_SECONDS = 10
STEADY_M = 0.005                     # m between readings still counted as steady
TREND_WINDOW = timedelta(minutes=30)  # previous reading must be this recent

log = logging.getLogger("tide_live")

LATEST_LEVEL = """
    SELECT strftime(ts AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ') AS ts,
           CAST(date AS VARCHAR) AS date, CAST(time AS VARCHAR) AS time,
           tide_level, series, qc_flag
    FROM read_parquet(?)
    ORDER BY ts DESC
    LIMIT 2
"""
FIELDS = ["ts", "date", "time", "tide_level", "series", "qc_flag"]


def trend(latest, previous):
    """(trend, change) from the previous reading to the latest, or (None, None)."""
    gap = (datetime.fromisoformat(latest["ts"]) - datetime.fromisoformat(previous["ts"]))
    if previous["series"] != latest["series"] or gap > TREND_WINDOW:
        return None, None
    change = round(latest["tide_level"] - previous["tide_level"], 3)
    if abs(change) <= STEADY_M:
        return "steady", change
    return ("rising" if change > 0 else "falling"), change


def latest_tide(con, levels_dir=LEVELS_DIR, station=STATION_CODE):
    """The newest stored reading with its trend as a dict, or None if nothing is stored yet."""
    files = sorted(levels_dir.glob("levels_*.parquet"))[-2:]  # names sort by date
    if not files:
        return None
    # The previous reading can be in yesterday's file, just after midnight UTC
    rows = con.execute(LATEST_LEVEL, [[str(f) for f in files]]).fetchall()
    if not rows:
        return None
    latest = {"station": station, **dict(zip(FIELDS, rows[0]))}
    previous = dict(zip(FIELDS, rows[1])) if len(rows) > 1 else None
    latest["trend"], latest["change"] = trend(latest, previous) if previous else (None, None)
    latest["previous_ts"] = previous["ts"] if previous else None
    return latest


async def serve_live_tide(host=HOST, port=PORT, levels_dir=LEVELS_DIR,
                          poll_seconds=POLL_SECONDS):
    """Serve ws://host:port, pushing each new tide reading to every browser."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    current = {"ts": None, "message": None}

    async def handler(ws):
        log.info("browser connected from %s", ws.remote_address)
        if current["message"]:
            await ws.send(current["message"])
        await ws.wait_closed()

    async with serve(handler, host, port) as server:
        log.info("serving live tide on ws://%s:%d", host, port)
        while True:
            try:
                tide = latest_tide(con, levels_dir)
            except duckdb.Error as exc:
                log.warning("skipping this poll: %s", exc)
            else:
                if tide and tide["ts"] != current["ts"]:
                    current.update(ts=tide["ts"], message=json.dumps(tide))
                    broadcast(server.connections, current["message"])
                    log.info("pushed %.3f m (%s) at %s to %d browsers", tide["tide_level"],
                             tide["trend"], tide["ts"], len(server.connections))
            await asyncio.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve_live_tide())
    except KeyboardInterrupt:
        pass
