"""
Export the merged DTM + NONNA-10 raster as a Cloud Optimized GeoTIFF and as
Cesium quantized-mesh terrain tiles.

  COG      DATA_DIR/dtm_nonna10_merged_cog.tif   (EPSG:2961, as merged)
  Terrain  DATA_DIR/terrain/{z}/{x}/{y}.terrain + layer.json
           Cesium geographic tiling scheme (EPSG:4326, TMS rows), levels 0-16,
           with octvertexnormals for lighting. Tiles are written uncompressed.

The COG keeps CGVD2013 heights. Terrain heights are shifted to the WGS84
ellipsoid by a constant CGVD2013_TO_WGS84 offset. Tile cells outside the 10 km
data square are set to 0 m CGVD2013 before that shift.

Needs rasterio, quantized-mesh-encoder and pydelatin
(pip install rasterio quantized-mesh-encoder pydelatin).
"""

import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from pydelatin import Delatin
import quantized_mesh_encoder as qme

from load_rasters import (DATA_DIR, DTM_PATH, NONNA_PATH, MERGED_PATH,
                          adjust_nonna, load_raster, merge_rasters, resample_to,
                          write_raster)

COG_PATH = DATA_DIR / "dtm_nonna10_merged_cog.tif"
TERRAIN_DIR = DATA_DIR / "terrain"

MAX_ZOOM = 16          # ~0.85 x 1.2 m vertex spacing at 256 cells/tile, close to the 1 m source
TILE_CELLS = 256       # height samples per tile edge = TILE_CELLS + 1
MAX_ERROR = 0.5        # mesh simplification error at MAX_ZOOM (m), doubled per level up
OUTSIDE_HEIGHT = 0.0   # height used where a tile extends past the data (CGVD2013)

# Vertical offset from CGVD2013 to WGS84 ellipsoidal heights for the terrain (m)
CGVD2013_TO_WGS84 = -20.270


def write_cog(src_path, dst_path):
    rio_copy(src_path, dst_path, driver="COG", compress="DEFLATE", predictor=3,
             overview_resampling="average", blocksize=512)


def tile_bounds(z, x, y):
    """(west, south, east, north) of a TMS tile in Cesium's geographic scheme."""
    size = 180.0 / 2 ** z
    return (-180 + x * size, -90 + y * size, -180 + (x + 1) * size, -90 + (y + 1) * size)


def tile_range(z, bounds):
    """Inclusive (x0, y0, x1, y1) of the tiles at level z that cover `bounds`."""
    w, s, e, n = bounds
    size = 180.0 / 2 ** z
    return (int((w + 180) // size), int((s + 90) // size),
            int(math.ceil((e + 180) / size)) - 1, int(math.ceil((n + 90) / size)) - 1)


def sample_tile(src, bounds):
    """Heights on a (TILE_CELLS+1)^2 vertex grid whose outer vertices sit on the tile edges."""
    w, s, e, n = bounds
    dx, dy = (e - w) / TILE_CELLS, (n - s) / TILE_CELLS
    size = TILE_CELLS + 1
    out = np.full((size, size), src.nodata, dtype="float32")
    reproject(rasterio.band(src, 1), out,
              dst_transform=from_bounds(w - dx / 2, s - dy / 2, e + dx / 2, n + dy / 2, size, size),
              dst_crs="EPSG:4326", dst_nodata=src.nodata, resampling=Resampling.bilinear)
    out[out == src.nodata] = OUTSIDE_HEIGHT
    return out + CGVD2013_TO_WGS84


def write_tile(path, heights, bounds, max_error):
    w, s, e, n = bounds
    tin = Delatin(heights, max_error=max_error)
    cols, rows = tin.vertices[:, 0], tin.vertices[:, 1]
    positions = np.column_stack([
        w + cols * (e - w) / TILE_CELLS,
        n - rows * (n - s) / TILE_CELLS,
        heights[rows.astype(int), cols.astype(int)],
    ]).astype("float64")
    # Delatin winds triangles counter-clockwise in raster rows (y down), i.e. clockwise
    # on the map; quantized-mesh needs counter-clockwise seen from above or Cesium culls them
    indices = tin.triangles[:, ::-1].astype("uint32")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        qme.encode(f, positions, indices, bounds=bounds,
                   extensions=[qme.VertexNormalsExtension(positions=positions, indices=indices)])


def write_terrain(src_path, out_dir):
    with rasterio.open(src_path) as src:
        data_bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        available = []
        for z in range(MAX_ZOOM + 1):
            # Cesium always requests both level-0 tiles
            x0, y0, x1, y1 = (0, 0, 1, 0) if z == 0 else tile_range(z, data_bounds)
            max_error = MAX_ERROR * 2 ** (MAX_ZOOM - z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    b = tile_bounds(z, x, y)
                    write_tile(out_dir / str(z) / str(x) / f"{y}.terrain",
                               sample_tile(src, b), b, max_error)
            available.append([{"startX": x0, "startY": y0, "endX": x1, "endY": y1}])
            print(f"  level {z:2d}: {(x1 - x0 + 1) * (y1 - y0 + 1)} tiles")

    layer = {
        "tilejson": "2.1.0",
        "name": "DTM + NONNA-10 (CGVD2013)",
        "format": "quantized-mesh-1.0",
        "version": "1.0.0",
        "scheme": "tms",
        "tiles": ["{z}/{x}/{y}.terrain?v={version}"],
        "projection": "EPSG:4326",
        "bounds": [-180, -90, 180, 90],
        "minzoom": 0,
        "maxzoom": MAX_ZOOM,
        "extensions": ["octvertexnormals"],
        "available": available,
    }
    (out_dir / "layer.json").write_text(json.dumps(layer, indent=2))


if __name__ == "__main__":
    dtm = load_raster(DTM_PATH, "DTM (land elevation)")
    nonna = adjust_nonna(load_raster(NONNA_PATH, "NONNA-10 (bathymetry)"), dtm.nodata)
    write_raster(merge_rasters(dtm, resample_to(nonna, dtm)), MERGED_PATH)
    del dtm, nonna
    print(f"Wrote {MERGED_PATH}")

    write_cog(MERGED_PATH, COG_PATH)
    print(f"Wrote {COG_PATH}")

    print(f"Writing quantized-mesh tiles to {TERRAIN_DIR}")
    write_terrain(MERGED_PATH, TERRAIN_DIR)
    print(f"Wrote {TERRAIN_DIR / 'layer.json'}")
