# Port and Coastal Operations Twin (vibe-coded)

A digital twin of Halifax Harbour, Nova Scotia, built with [Claude Code](https://claude.com/claude-code).
It brings together live vessel positions (AIS), the CHS tide level, the MSC coastal flooding risk
index, topo-bathymetric terrain and 3D buildings in a CesiumJS app with three panels:

- **Live** (2D): vessels as icons pointing along their heading, coloured by ship type, and the
  current tide level with a rising or falling arrow.
- **Replay** (2D): berth occupancy for the past week, with a day slider. Circles go from green to
  red as dwell time increases, and hovering shows each vessel's details.
- **Flood** (3D): terrain and buildings with a water plane at the current tide level. A slider
  raises and lowers the water, and buildings whose ground is at or below it turn red. The panel
  also shows the current storm surge flooding risk.

Almost all of the code was written by Claude Code from the prompts below, with the decisions it
made along the way recorded under [Decisions](#decisions).

## Contents

- [Repository layout](#repository-layout)
- [Setup](#setup)
- [Running it](#running-it)
- [The app](#the-app)
- [Scripts](#scripts)
- [Data and vertical datums](#data-and-vertical-datums)
- [How it was built](#how-it-was-built)
- [Decisions](#decisions)

## Repository layout

```
scripts/
  load_rasters.py               load and merge the DTM and NONNA-10 bathymetry
  export_terrain.py             merged raster -> Cloud Optimized GeoTIFF + Cesium terrain tiles
  extrude_buildings.py          building footprints -> 3D solids
  export_buildings_3dtiles.py   3D solids -> Cesium 3D Tiles
  ais_consumer.py               live AIS from aisstream.io -> GeoParquet, plus berth analysis
  ais_live.py                   websocket feed of the latest vessel positions
  tide_consumer.py              CHS IWLS tide level -> Parquet every 5 minutes
  tide_live.py                  websocket feed of the latest tide level and trend
  storm_surge_consumer.py       MSC coastal flooding risk index -> GeoParquet every 30 minutes
  storm_surge_live.py           websocket feed of the current storm surge risk
  twin_app.py                   serves the CesiumJS app, terrain, tiles and the three feeds
  twin_app/                     the app: index.html, app.js, style.css
  test_*.py                     unit tests (python -m unittest)
```

## Setup

All data lives outside the repository, in `DATA_DIR`, set at the top of
[scripts/load_rasters.py](scripts/load_rasters.py) (`/Users/michaelkieser/projects/3d_port/`).
Change it to your own folder. The inputs it expects are:

| File | What it is |
|---|---|
| `dtm_1m_utm20_w_4_94.tif` | 1 m lidar DTM, EPSG:2961, CGVD2013 heights |
| `nonna10_proj.tif` | CHS NONNA-10 bathymetry reprojected to EPSG:2961, chart datum heights |
| `building_proj.gpkg` | NRCan automatically extracted building footprints with `elevmin` and `heightmax` |
| `extent.gpkg` | the project extent, a 10 km square polygon in EPSG:2961 |
| `berth.gpkg` | named berth polygons, for the berth occupancy analysis |

Python packages:

- Services and app: `pip install duckdb websockets`
- Terrain and building exports: `pip install rasterio quantized-mesh-encoder pydelatin geopandas pyproj mapbox-earcut`

The project was developed with Python 3.14 for the services and Python 3.10 for the exports.

The AIS consumer needs a free API key from [aisstream.io](https://aisstream.io) in the
`AISSTREAM_API_KEY` environment variable. The CHS and MSC APIs need no key.

## Running it

Run everything from `scripts/`.

**1. Build the terrain and buildings (once, or when the source data changes):**

```bash
python export_terrain.py            # merge DTM + NONNA-10, write the COG and DATA_DIR/terrain/
python extrude_buildings.py         # write DATA_DIR/building_3d.gpkg
python export_buildings_3dtiles.py  # write DATA_DIR/buildings_3dtiles/
```

`python load_rasters.py` on its own prints a summary of both rasters and writes the merged raster.

**2. Start the three consumers (each runs until stopped with Ctrl+C):**

```bash
AISSTREAM_API_KEY=... python ais_consumer.py
python tide_consumer.py
python storm_surge_consumer.py
```

**3. Run the berth analysis once a day** (for example from cron), for the Replay panel:

```bash
python ais_consumer.py --compact             # yesterday (UTC)
python ais_consumer.py --compact 2026-10-22  # a given day
```

**4. Start the app and open http://localhost:8000:**

```bash
python twin_app.py
```

`twin_app.py` serves the app over HTTP on port 8000 and runs the three websocket feeds itself,
on ports 8765 (vessels), 8766 (tide) and 8767 (storm surge). Each feed can also run on its own,
for example `python tide_live.py`.

**Tests:**

```bash
python -m unittest
```

## The app

The app is plain HTML and JavaScript on a single CesiumJS 1.146 viewer, loaded from a CDN.
Switching tabs changes the scene between 2D and 3D. OpenStreetMap is the base map, so no Cesium
ion token is needed. Open a panel directly with `?tab=live`, `?tab=replay` or `?tab=flood`.

### Live

- **Vessels:** each vessel's latest position from the last 10 minutes of AIS files, updated as
  the consumer writes new files. The icon points along the reported heading, or the course over
  ground while the vessel is moving. Vessels with neither show as a circle.
- **Colours:** Cargo & tanker (blue), Passenger & ferry (orange), Tug, pilot & service (green), and
  grey for every other type or a type not yet reported. The legend counts each group, and
  hovering a vessel shows its name, exact type, MMSI, navigational status, speed, course, heading
  and when it last reported.
- **Tide:** the latest level at Halifax (station 00490) in metres above chart datum, with an
  arrow for rising, falling or steady. The card says whether the level is observed or predicted,
  and flags a stale reading.

### Replay

- One circle per vessel and berth for each of the past 7 days, at the vessel's mean position in
  the berth.
- The colour runs from green (short stay) to red (24 hours) on a fixed 0–24 hour scale.
  Hovering a circle shows the vessel, its type, the berth and the hours moored.
- The slider steps through the days and the play button animates them. Days without an
  analysis file show as not analysed.

### Flood

- **Scene:** topo-bathymetric terrain and 3D buildings, with a translucent water plane over the
  project extent at the current tide level.
- **Water slider:** raises or lowers the plane from −2 m to +6 m relative to the current tide.
  The panel shows the water level in chart datum and in CGVD2013, and how many of the loaded
  buildings are at or below the water.
- **Buildings:** black with grey outlines. A building turns red when its ground elevation
  (`z_bottom`) is at or below the water. Hovering a building shows its ground elevation, its
  height and how far it is above or below the water.
- **Storm surge:** the current coastal flooding risk where storm surge is a factor, as the
  maximum risk level with its impact, likelihood and forecast period. The risk areas are draped
  on the terrain in orange. The index gives risk levels, not surge heights.

## Scripts

### Terrain and buildings

**[load_rasters.py](scripts/load_rasters.py)** loads the 1 m DTM and the 10 m NONNA-10
bathymetry. It shifts NONNA-10 from chart datum to CGVD2013 (`CD_TO_CGVD2013 = -1.41` m) and
resamples it onto the DTM grid. The merge takes bathymetry wherever NONNA-10 has data and the DTM
everywhere else. It also defines `DATA_DIR`.

**[export_terrain.py](scripts/export_terrain.py)** writes the merged raster as a Cloud Optimized
GeoTIFF, plus Cesium quantized-mesh terrain tiles for levels 0–16 with vertex normals. Heights
are shifted from CGVD2013 to the WGS84 ellipsoid by a constant −20.27 m.

**[extrude_buildings.py](scripts/extrude_buildings.py)** extrudes each footprint from its ground
elevation (`elevmin`) by its height (`heightmax`) into a closed solid in EPSG:2961.

**[export_buildings_3dtiles.py](scripts/export_buildings_3dtiles.py)** writes the solids as
3D Tiles 1.1, grouped into 1 km tiles. Each building is a pickable feature with its attributes
(`feature_id`, `heightmax`, `elevmin`, `z_bottom`, `z_top`, `bldgarea`). Each building's edges
are stored with `CESIUM_primitive_outline`, so Cesium can draw outlines.

### Consumers and feeds

**[ais_consumer.py](scripts/ais_consumer.py)** subscribes to aisstream.io for the extent's
bounding box and clips positions exactly to the extent polygon.
- **Positions:** flushed every 60 s as date-partitioned GeoParquet to `DATA_DIR/ais/positions/`.
- **Ship types:** each vessel's latest type, call sign and IMO number, from the static data
  messages, go to `DATA_DIR/ais/vessels.parquet`.
- **`--compact`:** merges one day's position files into a single file and writes berth
  occupancy to `DATA_DIR/ais/analysis_daily/`: one row per vessel and berth, counting the clock
  hours the vessel was moored in that berth (speed under 1 kn).

**[ais_live.py](scripts/ais_live.py)** sends each vessel's latest position from the last
10 minutes of position files, with its ship type, as a GeoJSON FeatureCollection whenever a new
file appears.

**[tide_consumer.py](scripts/tide_consumer.py)** polls the CHS IWLS API for station 00490 every
5 minutes. It stores the latest observed level (`wlo`), or the predicted level (`wlp`) when the
gauge has no recent data, in one Parquet file per day under `DATA_DIR/tide/levels/`. Each row has
`ts`, `date`, `time`, `tide_level`, `series`, `qc_flag` and `fetched_at`. A reading that's already
stored is skipped.

**[tide_live.py](scripts/tide_live.py)** sends the latest tide reading with its trend:
`rising`, `falling` or `steady`, compared with the previous reading of the same series within
30 minutes.

**[storm_surge_consumer.py](scripts/storm_surge_consumer.py)** polls the MSC GeoMet
`coastal-flooding-risk-index` collection every 30 minutes.
- **Clipping:** keeps the risk cells that touch the extent polygon.
- **Snapshots:** writes a GeoParquet snapshot to `DATA_DIR/coastal_flooding/risk_index/` only
  when the set of features changes.
- **`latest.json`:** records the current snapshot, or that there is no risk, after every poll.

**[storm_surge_live.py](scripts/storm_surge_live.py)** sends the current snapshot's features
where storm surge drives the risk and the forecast period hasn't ended, as GeoJSON. Each message
includes the time of the last poll.

**[twin_app.py](scripts/twin_app.py)** serves the app, `DATA_DIR/terrain/` and
`DATA_DIR/buildings_3dtiles/` over HTTP, and runs the three feeds. It also has two JSON routes:
- `/api/config` gives the extent, the datum offsets and the feed ports.
- `/api/replay?days=7` gives berth occupancy per day from the analysis files.

## Data and vertical datums

| Source | Used for | Heights |
|---|---|---|
| Lidar DTM (1 m) | land terrain, building ground elevation | CGVD2013 |
| CHS NONNA-10 | bathymetry | chart datum, shifted −1.41 m to CGVD2013 |
| NRCan automatically extracted buildings | 3D buildings | CGVD2013 |
| [aisstream.io](https://aisstream.io) | vessel positions and ship types | – |
| [CHS IWLS API](https://api-iwls.dfo-mpo.gc.ca) | tide level, station 00490 Halifax | chart datum |
| [MSC GeoMet OGC API](https://api.weather.gc.ca/collections/coastal-flooding-risk-index) | coastal flooding risk index | – |

To compare tide levels with the terrain and buildings, everything is brought to one height
reference:

- **Chart datum** zero at Halifax is at −1.41 m CGVD2013. IWLS gives chart datum at −21.68 m
  ellipsoidal (NAD83(CSRS)), and the geoid there is at −20.27 m.
- **Terrain and buildings** are shifted from CGVD2013 to the WGS84 ellipsoid by −20.27 m.
- **The water plane** is therefore at tide level − 1.41 m in CGVD2013, and at a further −20.27 m
  on the ellipsoid.

## How it was built

The raster loading and merging, terrain export, building extrusion and 3D Tiles export, and the
original AIS consumer and live feed were built before the session recorded here, so their prompts
aren't included. The rest was built in one Claude Code session (Claude Opus 5.5, October 2026)
from these prompts:

1. > For my Port and Coastal Operations Twin, I want to build a service that stores the current
   > tide level using the CHS IWLS API. I'll need a Python websocket consumer that pulls the tide
   > level for CHS station 00490 every 5 minutes and stores the date, time and tide level. Then
   > can you write a function that can push the most recent tide level to a browser over a
   > websocket.

   Result: `tide_consumer.py`, `tide_live.py` and their tests.

2. > For my Port and Coastal Operations Twin, I want to build a service that stores the current
   > storm surge published by the MSC GeoMet-OGC-API (collections/coastal-flooding-risk-index/items).
   > I'll need a Python websocket consumer that pulls the storm surge within the project extent
   > (/Users/michaelkieser/projects/3d_port/extent.gpkg) every 30 minutes and stores the storm
   > surge as GeoParquet. Then can you write a function that can push the most recent storm surge
   > to a browser over a websocket.

   Result: `storm_surge_consumer.py`, `storm_surge_live.py` and their tests.

3. > Okay, so now I want to bring everything together into a CesiumJS app. I want the app to have
   > three panels, called "Live", "Replay" and "Flood".
   > The first panel, called "Live", plots in 2D the current vessels using the latest ais-positions
   > Geoparquet file as oriented icons coloured by type, and I also want to show a the current tide
   > level using the latest tide-levels Geoparquet file with a arrow of upward or downward trend in
   > the tide.
   > The second panel, called "Replay", plots in 2D each vessel location based on the
   > ais-analysis_daily data for the past week and a timeslider. Each vessel location will be
   > styled with a circle coloured green to red based on the shorter to longer dwell time, with on
   > hover info for each vessel.
   > The third panel, call "Flood", plots in 3D the topo-bathy terrain-tiles and buildings_3dtiles,
   > with a water plane at the current tide level. I also want to show the current maximum storm
   > surge height using the latest coastal_flooding data. The user should be able to adjust a
   > slider to raise and lower the water plane displaying the height above the current tide level.
   > The 3D buildings should be styled black with grey outline and those that are touched by the
   > water plane styled red with grey outline.

   Result: `twin_app.py` and `twin_app/`, ship types in the AIS consumer and feed, the tide trend,
   building outlines, and the terrain winding fix. Claude Code checked each panel in headless
   Chrome against the real data.

4. Claude Code found that the chart datum offset for NONNA-10 had the wrong sign and asked
   whether to fix it.

   > Yes, make that change.

   Result: `CD_TO_CGVD2013` changed from +1.44 m to −1.41 m, and the terrain rebuilt.

5. > Can you connect to my Github account (https://github.com/mkieser) and push this to a new repo
   > called twin_port_vibecoded.

6. > Let's remove the ais_live.html

   Result: the standalone Leaflet map was removed; the Live panel replaces it.

7. > Can you populate the README file, that summarizes the creation of this repo using Claude Code,
   > including the prompts, the decisions, the different scripts, their functions, how to run the
   > scripts and the functionality in the app.

   Result: this README.

## Decisions

These are the choices Claude Code made where the request and the data didn't line up, or where
there was more than one reasonable approach.

**Tide**

- **Polling instead of a websocket consumer.** The IWLS and GeoMet APIs only answer plain HTTP
  requests, so the consumers poll them. Websockets are used only to push data to the browser.
- **Predicted tide as a fallback.** Halifax's observed water level series has been empty since
  about November 2025. The consumer stores the predicted level when there is no observation, and
  the `series` column records which one each row is.
- **Trend only within one series.** The trend compares readings of the same series no more than
  30 minutes apart, so a switch between observed and predicted never shows up as a rise or fall.

**Storm surge**

- **No surge heights.** The MSC coastal flooding risk index gives risk, impact and likelihood
  levels per grid cell, and whether storm surge, tide or waves drive the risk. It has no surge
  height. The consumer stores every risk cell in the extent, and the app shows the maximum risk
  level where storm surge is a factor, not a height.
- **Snapshots only on change.** Products are issued about daily, so a new snapshot is written
  only when the features change instead of every 30 minutes.
- **`latest.json`.** DuckDB can't write a valid GeoParquet file with no rows, so "no risk" is
  recorded in `latest.json` instead of in an empty file. This also means a cancelled warning
  disappears straight away instead of lingering until it expires.

**App**

- **Ship types from static data.** AIS position reports don't include the ship type, so the
  consumer also stores the static data messages. A vessel shows grey until its type arrives,
  usually within 6 minutes for larger vessels.
- **Three type colours.** Only three colours stay distinguishable for every pair on a map,
  including for colour-blind viewers (checked with a palette validator). So types are grouped into
  Cargo & tanker, Passenger & ferry, and Tug, pilot & service, with grey for the rest. The exact
  type is in the hover details.
- **Last 10 minutes of positions.** The Live panel uses each vessel's latest position from the
  last 10 minutes of AIS files, not only the newest file, so vessels that report infrequently
  don't disappear.
- **Dwell colours.** The green-to-red dwell scale is fixed at 0–24 hours, so colours mean the
  same thing on every day.
- **"Touched by the water".** A building counts as touched when its ground elevation (`z_bottom`)
  is at or below the water plane.

**Fixes to the existing pipeline**

- **Hidden terrain surface.** `export_terrain.py` wrote every terrain triangle clockwise, but
  quantized-mesh needs counter-clockwise, so Cesium culled the terrain surface. The winding was
  reversed and the tiles regenerated.
- **Chart datum sign.** NONNA-10 was shifted +1.44 m from chart datum to CGVD2013, but CHS puts
  Halifax chart datum 1.41 m below CGVD2013, so the bathymetry was about 2.85 m too shallow. The
  offset is now −1.41 m, shared with the app's water plane.
- **Building outlines.** Cesium draws outlines on 3D Tiles only from `CESIUM_primitive_outline`
  data in the tiles, so the exporter now writes each building's edges and the tiles were
  regenerated.
