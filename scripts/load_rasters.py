"""
Load the DTM (land elevation) and NONNA-10 (bathymetry) GeoTIFFs.

Both rasters cover the same area:
  CRS        NAD83(CSRS) / UTM zone 20N  (EPSG:2961)
  Size       DTM = 10000 x 10000 pixels at 1 m, NONNA-10 = 1000 x 1000 at 10 m
             (NONNA-10 is resampled bilinearly onto the 1 m DTM grid before merging)
  Extent     x 450000-460000, y 4940000-4950000
  NoData     DTM = -32767, NONNA-10 = -9999 (reset to -32767 on load)
  Datum      DTM = CGVD2013, NONNA-10 = local Chart Datum
             (shifted -1.41 m to CGVD2013 on load)

Loading uses rasterio if installed (pip install rasterio); otherwise falls back
to Pillow + numpy, reading the georeferencing straight from the GeoTIFF tags.
Resampling and writing need rasterio.
"""

from dataclasses import dataclass
from pathlib import Path
import sys

import numpy as np

DATA_DIR = Path("/Users/michaelkieser/projects/3d_port/")
DTM_PATH = DATA_DIR / "dtm_1m_utm20_w_4_94.tif"
NONNA_PATH = DATA_DIR / "nonna10_proj.tif"
MERGED_PATH = DATA_DIR / "dtm_nonna10_merged.tif"

# Vertical offset from local Chart Datum to CGVD2013 for the NONNA-10 data (m), i.e. the
# height of chart datum zero in CGVD2013. CHS IWLS station 00490 (Halifax) puts chart
# datum at -21.68 m NAD83(CSRS) ellipsoidal; the geoid there is at -20.27 m
# (export_terrain.CGVD2013_TO_WGS84), so chart datum is 1.41 m below CGVD2013 zero.
CD_TO_CGVD2013 = -1.41


@dataclass
class Raster:
    name: str
    data: np.ma.MaskedArray      # NoData cells are masked
    transform: tuple             # (x_origin, pixel_w, 0, y_origin, 0, -pixel_h)
    crs: str
    nodata: float

    @property
    def shape(self):
        return self.data.shape

    @property
    def bounds(self):
        x0, pw, _, y0, _, ph = self.transform
        rows, cols = self.shape
        return (x0, y0 + ph * rows, x0 + pw * cols, y0)  # (left, bottom, right, top)

    def set_nodata(self, value):
        """Write `value` into every NoData cell and make it the NoData value."""
        self.data = np.ma.masked_array(self.data.filled(value),
                                       mask=self.data.mask.copy(), fill_value=value)
        self.nodata = value

    def xy(self, row, col):
        """Map coordinates of a pixel centre."""
        x0, pw, _, y0, _, ph = self.transform
        return x0 + (col + 0.5) * pw, y0 + (row + 0.5) * ph


def _load_rasterio(path, name):
    import importlib
    rasterio = importlib.import_module("rasterio")
    with rasterio.open(path) as src:
        data = src.read(1, masked=True).astype("float32")
        t = src.transform
        return Raster(name, data, (t.c, t.a, t.b, t.f, t.d, t.e),
                      src.crs.to_string(), src.nodata)


def _load_pillow(path, name):
    from PIL import Image
    with Image.open(path) as im:
        tags = im.tag_v2
        arr = np.array(im, dtype="float32")
        sx, sy, _ = tags[33550]                 # ModelPixelScale
        _, _, _, x0, y0, _ = tags[33922]        # ModelTiepoint (pixel 0,0 -> x0,y0)
        nodata = float(str(tags.get(42113, "nan")).strip("\x00"))
        crs = str(tags.get(34737, "unknown")).split("|")[0]
    data = np.ma.masked_equal(arr, nodata) if not np.isnan(nodata) else np.ma.masked_invalid(arr)
    return Raster(name, data, (x0, sx, 0.0, y0, 0.0, -sy), crs, nodata)


def load_raster(path, name):
    path = Path(path)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    try:
        return _load_rasterio(path, name)
    except ImportError:
        return _load_pillow(path, name)


def adjust_nonna(nonna: Raster, nodata):
    """Shift NONNA-10 from Chart Datum to CGVD2013 and set its NoData to `nodata`."""
    nonna.data = nonna.data + CD_TO_CGVD2013  # valid cells only
    nonna.set_nodata(nodata)
    return nonna


def resample_to(r: Raster, like: Raster):
    """Bilinearly resample `r` onto the grid of `like` (needs rasterio)."""
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, reproject

    def affine(t):
        x0, pw, rx, y0, ry, ph = t
        return Affine(pw, rx, x0, ry, ph, y0)

    out = np.full(like.shape, r.nodata, dtype="float32")
    reproject(r.data.filled(r.nodata).astype("float32"), out,
              src_transform=affine(r.transform), src_crs=r.crs, src_nodata=r.nodata,
              dst_transform=affine(like.transform), dst_crs=like.crs, dst_nodata=r.nodata,
              resampling=Resampling.bilinear)
    data = np.ma.masked_equal(out, r.nodata)
    return Raster(r.name, data, like.transform, like.crs, r.nodata)


def merge_rasters(dtm: Raster, nonna: Raster, name="DTM + NONNA-10 (CGVD2013)"):
    """Combine land and bathymetry on their shared grid.

    NONNA-10 wins wherever it has data (the DTM is a flat water surface there);
    the DTM fills every other cell. Cells missing from both stay NoData.
    """
    if not (dtm.shape == nonna.shape
            and np.allclose(dtm.transform, nonna.transform)
            and dtm.crs == nonna.crs):
        raise ValueError("DTM and NONNA-10 are not on the same grid")
    use_nonna = ~nonna.data.mask
    values = np.where(use_nonna, nonna.data.data, dtm.data.data)
    mask = dtm.data.mask & nonna.data.mask
    data = np.ma.masked_array(values, mask=mask, fill_value=dtm.nodata)
    merged = Raster(name, data, dtm.transform, dtm.crs, dtm.nodata)
    merged.set_nodata(dtm.nodata)
    return merged


def write_raster(r: Raster, path):
    """Write a single-band float32 GeoTIFF (needs rasterio)."""
    import rasterio
    from rasterio.transform import Affine
    x0, pw, rx, y0, ry, ph = r.transform
    with rasterio.open(path, "w", driver="GTiff", height=r.shape[0], width=r.shape[1],
                       count=1, dtype="float32", crs=r.crs, nodata=r.nodata,
                       transform=Affine(pw, rx, x0, ry, ph, y0), compress="deflate") as dst:
        dst.write(r.data.filled(r.nodata).astype("float32"), 1)


def summarize(r: Raster):
    valid = r.data.compressed()
    print(f"{r.name}")
    print(f"  CRS      : {r.crs}")
    print(f"  Shape    : {r.shape}  (rows, cols)")
    print(f"  Pixel    : {r.transform[1]} x {abs(r.transform[5])} m")
    print(f"  Bounds   : {r.bounds}")
    print(f"  NoData   : {r.nodata}  ({r.data.mask.sum():,} cells masked)")
    print(f"  Values   : min {valid.min():.2f}  median {np.median(valid):.2f}  max {valid.max():.2f} m")


if __name__ == "__main__":
    dtm = load_raster(DTM_PATH, "DTM (land elevation)")
    nonna = load_raster(NONNA_PATH, "NONNA-10 (bathymetry)")
    adjust_nonna(nonna, dtm.nodata)  # Chart Datum -> CGVD2013, NoData -9999 -> -32767

    summarize(dtm)
    summarize(nonna)

    nonna = resample_to(nonna, dtm)  # 10 m -> DTM's 1 m grid
    overlap = ~dtm.data.mask & ~nonna.data.mask
    print(f"\nCells with both DTM and bathymetry: {overlap.sum():,}")

    merged = merge_rasters(dtm, nonna)
    print()
    summarize(merged)
    write_raster(merged, MERGED_PATH)
    print(f"\nWrote {MERGED_PATH}")
