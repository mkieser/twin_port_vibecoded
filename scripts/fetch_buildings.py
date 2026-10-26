"""
Cut the project's building footprints out of the national NRCan building layer.

  Source   BUILDINGS_URL, NRCan automatically extracted buildings (auto_building_opti_2),
           one GeoParquet file for all of Canada (~2.4 GB). It is read in place over
           HTTP; only the parts DuckDB needs are fetched, nothing is saved.
  Extent   DATA_DIR/extent.gpkg  (EPSG:2961 polygon, the 10 km data square)
  Output   DATA_DIR/building_proj.gpkg, layer building
           EPSG:2961, every source attribute (extrude_buildings.py needs elevmin and
           heightmax), only the footprints that intersect the extent.

The source CRS is taken from the file's GeoParquet metadata (OGC:CRS84 if it names
none). When the file has a bbox covering column, rows are filtered on it first so
DuckDB can skip row groups outside the extent; the exact test is ST_Intersects.

Pass --source PATH to read a copy downloaded by hand instead of BUILDINGS_URL.

Needs duckdb (pip install duckdb).
"""

import argparse
import json

import duckdb

from load_rasters import DATA_DIR, EXTENT_PATH

BUILDINGS_URL = ("https://download-telecharger.services.geo.ca/pub/nrcan_rncan/extraction/"
                 "auto_building/auto_building_opti_2/auto_building_opti_2.parquet")
BUILDINGS_PATH = DATA_DIR / "building_proj.gpkg"  # extrude_buildings.BUILDINGS_PATH
CRS = "EPSG:2961"
REQUIRED = ("elevmin", "heightmax")


def geo_metadata(con, source):
    """The file's GeoParquet 'geo' metadata as a dict (empty if it has none)."""
    row = con.execute(
        "SELECT decode(value) FROM parquet_kv_metadata(?) WHERE decode(key) = 'geo'",
        [source]).fetchone()
    return json.loads(row[0]) if row else {}


def crs_code(crs):
    """'AUTH:CODE' for a GeoParquet PROJJSON CRS; missing means OGC:CRS84 per the spec."""
    if crs is None:
        return "OGC:CRS84"
    ident = crs.get("id") or {}
    if "authority" not in ident or "code" not in ident:
        raise ValueError(f"Can't tell the source CRS from its metadata: {crs.get('name', crs)}")
    return f"{ident['authority']}:{ident['code']}"


def fetch_buildings(source=BUILDINGS_URL, extent_path=EXTENT_PATH, dst_path=BUILDINGS_PATH):
    con = duckdb.connect()
    # x = easting/longitude everywhere, including the GDAL writer (required for SRS in DuckDB 2.0)
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; "
                "SET geometry_always_xy = true")

    geo = geo_metadata(con, str(source))
    col = geo.get("primary_column", "geometry")
    meta = geo.get("columns", {}).get(col, {})
    src_crs = crs_code(meta["crs"]) if "crs" in meta else "OGC:CRS84"

    columns = [r[0] for r in con.execute(
        "DESCRIBE SELECT * FROM read_parquet(?)", [str(source)]).fetchall()]
    missing = [c for c in REQUIRED if c not in columns]
    if missing:
        raise ValueError(f"Source has no {', '.join(missing)} column; it has {', '.join(columns)}")

    # Extent in the project CRS, and its bounding box in the source CRS for the bbox prefilter
    con.execute("CREATE TABLE extent AS SELECT ST_Union_Agg(geom) AS geom FROM ST_Read(?)",
                [str(extent_path)])
    xmin, ymin, xmax, ymax = con.execute(f"""
        SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g)
        FROM (SELECT ST_Transform(geom, '{CRS}', '{src_crs}', always_xy := true) AS g FROM extent)
    """).fetchone()

    prefilter = "true"
    bbox = meta.get("covering", {}).get("bbox")
    if bbox:
        c = {k: ".".join(f'"{p}"' for p in path) for k, path in bbox.items()}
        prefilter = (f"{c['xmax']} >= {xmin} AND {c['xmin']} <= {xmax} "
                     f"AND {c['ymax']} >= {ymin} AND {c['ymin']} <= {ymax}")
    drop = {col} | ({bbox["xmin"][0]} if bbox else set())
    keep = ", ".join(f'"{c}"' for c in columns if c not in drop)

    con.execute(f"""
        CREATE TABLE building AS
        SELECT {keep}, g AS geom
        FROM (SELECT *, ST_Transform("{col}", '{src_crs}', '{CRS}', always_xy := true) AS g
              FROM read_parquet(?) WHERE {prefilter}) b
        WHERE ST_Intersects(g, (SELECT geom FROM extent))
    """, [str(source)])
    count = con.execute("SELECT count(*) FROM building").fetchone()[0]

    dst_path.unlink(missing_ok=True)
    con.execute(f"""COPY building TO '{dst_path}'
                    (FORMAT GDAL, DRIVER 'GPKG', SRS '{CRS}', LAYER_NAME 'building')""")
    con.close()
    return count, src_crs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--source", default=BUILDINGS_URL,
                        help="GeoParquet path or URL (default: the NRCan file)")
    args = parser.parse_args()
    print(f"Reading {args.source}")
    count, src_crs = fetch_buildings(args.source)
    print(f"Wrote {count:,} buildings (from {src_crs}) to {BUILDINGS_PATH}")
