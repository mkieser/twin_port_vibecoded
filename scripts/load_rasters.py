"""
Load the DTM (land elevation) and NONNA-10 (bathymetry) GeoTIFFs, clipped to the
project extent.

Inputs, all in the repository's data/ folder (DATA_DIR):
  Extent     extent.gpkg, a polygon in NAD83(CSRS) / UTM zone 20N (EPSG:2961)
  DTM        dtm_1m_utm20_w_4_94.tif, 1 m, EPSG:2961, CGVD2013 heights. Downloaded
             from DTM_URL (NRCan HRDEM) on first use if it is not already in DATA_DIR.
  NONNA-10   NONNA10_*.tiff, three CHS tiles in WGS 84 (EPSG:4326) at 0.0001 deg,
             chart datum heights. They come from the CHS NONNA download cart, so
             they are kept in DATA_DIR rather than downloaded.

Both rasters are clipped to the extent's bounding box:
  Extent     x 450000-460000, y 4940000-4950000 (EPSG:2961)
  Size       DTM = 10000 x 10000 pixels at 1 m. The NONNA-10 tiles are mosaicked
             and resampled bilinearly onto the 1 m DTM grid (and so into
             EPSG:2961) before merging.
  NoData     DTM = -32767, NONNA-10 = 3.4028235e+38 (reset to -32767 on load)
  Datum      DTM = CGVD2013, NONNA-10 = local Chart Datum
             (shifted -1.41 m to CGVD2013 on load)

A single GeoTIFF loads with rasterio if installed (pip install rasterio);
otherwise it falls back to Pillow + numpy, reading the georeferencing straight
from the GeoTIFF tags. Mosaicking, resampling and writing need rasterio.
"""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import struct
import sys
import urllib.request

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EXTENT_PATH = DATA_DIR / "extent.gpkg"
DTM_URL = ("https://download-telecharger.services.geo.ca/pub/elevation/dem_mne/"
           "highresolution_hauteresolution/dtm_mnt/1m/NS/NSDNR_2019_3/utm20/"
           "dtm_1m_utm20_w_4_94.tif")
DTM_PATH = DATA_DIR / "dtm_1m_utm20_w_4_94.tif"
NONNA_PATHS = [DATA_DIR / "NONNA10_4460N06360W.tiff",
               DATA_DIR / "NONNA10_4460N06370W.tiff",
               DATA_DIR / "NONNA10_4470N06370W.tiff"]
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


def read_extent(path=EXTENT_PATH):
    """Bounding box (left, bottom, right, top) and CRS of every geometry in a GeoPackage.

    Reads the envelope from each GeoPackage geometry header, so no vector library is needed.
    """
    path = Path(path)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table, column, srs_id = con.execute(
            "SELECT table_name, column_name, srs_id FROM gpkg_geometry_columns").fetchone()
        blobs = [b for (b,) in con.execute(f'SELECT "{column}" FROM "{table}"') if b]
    finally:
        con.close()
    boxes = []
    for b in blobs:
        flags = b[3]
        if not (flags >> 1) & 0b111:
            raise ValueError(f"{path}: geometry has no envelope in its header")
        minx, maxx, miny, maxy = struct.unpack("<dddd" if flags & 1 else ">dddd", b[8:40])
        boxes.append((minx, miny, maxx, maxy))
    lefts, bottoms, rights, tops = zip(*boxes)
    return (min(lefts), min(bottoms), max(rights), max(tops)), f"EPSG:{srs_id}"


def download(url, path):
    """Download `url` to `path` unless it is already there."""
    path = Path(path)
    if path.exists():
        return path
    print(f"Downloading {url}")
    tmp = path.with_name(path.name + ".part")
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        sys.exit(f"Download failed ({e}). Fetch {url} by hand and save it as {path}")
    tmp.replace(path)
    return path


def clip(r: Raster, bounds):
    """Crop `r` to the whole pixels inside `bounds` (left, bottom, right, top), in r's CRS."""
    x0, pw, _, y0, _, ph = r.transform
    left, bottom, right, top = bounds
    rows, cols = r.shape
    c0 = max(0, int(round((left - x0) / pw)))
    c1 = min(cols, int(round((right - x0) / pw)))
    r0 = max(0, int(round((top - y0) / ph)))
    r1 = min(rows, int(round((bottom - y0) / ph)))
    if c0 >= c1 or r0 >= r1:
        raise ValueError(f"{r.name} does not overlap {bounds}")
    data = r.data[r0:r1, c0:c1]
    return Raster(r.name, data, (x0 + c0 * pw, pw, 0.0, y0 + r0 * ph, 0.0, ph), r.crs, r.nodata)


def load_mosaic(paths, name, bounds=None, bounds_crs=None):
    """Mosaic several GeoTIFFs in the same CRS, optionally limited to `bounds` (needs rasterio).

    `bounds` (left, bottom, right, top) is in `bounds_crs`, or in the tiles' CRS if that is None.
    """
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import transform_bounds

    missing = [str(p) for p in paths if not Path(p).exists()]
    if missing:
        sys.exit("File not found: " + ", ".join(missing))
    srcs = [rasterio.open(p) for p in paths]
    try:
        crs, nodata = srcs[0].crs, srcs[0].nodata
        if bounds is not None and bounds_crs is not None:
            bounds = transform_bounds(bounds_crs, crs, *bounds)
        # NaN for the gaps: merge() rejects NONNA's float32-max NoData and fills with 0
        arr, t = merge(srcs, bounds=bounds, nodata=np.nan, dtype="float32")
    finally:
        for src in srcs:
            src.close()
    data = np.ma.masked_invalid(arr[0])
    return Raster(name, data, (t.c, t.a, t.b, t.f, t.d, t.e), crs.to_string(), nodata)


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


def load_inputs():
    """The DTM and the datum-shifted NONNA-10 mosaic, both clipped to the project extent.

    The DTM is downloaded first if needed. NONNA-10 stays in WGS 84; resample_to()
    puts it on the DTM grid.
    """
    bounds, crs = read_extent()
    dtm = load_raster(download(DTM_URL, DTM_PATH), "DTM (land elevation)")
    dtm = clip(dtm, bounds)  # the DTM and the extent are both EPSG:2961
    nonna = load_mosaic(NONNA_PATHS, "NONNA-10 (bathymetry)", bounds, crs)
    adjust_nonna(nonna, dtm.nodata)  # Chart Datum -> CGVD2013, NoData -> -32767
    return dtm, nonna


def summarize(r: Raster):
    valid = r.data.compressed()
    print(f"{r.name}")
    print(f"  CRS      : {r.crs}")
    print(f"  Shape    : {r.shape}  (rows, cols)")
    print(f"  Pixel    : {r.transform[1]} x {abs(r.transform[5])} (CRS units)")
    print(f"  Bounds   : {r.bounds}")
    print(f"  NoData   : {r.nodata}  ({r.data.mask.sum():,} cells masked)")
    print(f"  Values   : min {valid.min():.2f}  median {np.median(valid):.2f}  max {valid.max():.2f} m")


if __name__ == "__main__":
    dtm, nonna = load_inputs()

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
