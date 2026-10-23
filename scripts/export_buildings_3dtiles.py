"""
Export the extruded buildings as Cesium 3D Tiles 1.1.

  Input    DATA_DIR/building_3d.gpkg (from extrude_buildings.py; EPSG:2961, CGVD2013)
  Output   DATA_DIR/buildings_3dtiles/tileset.json + tiles/{col}_{row}.glb

Heights are shifted to the WGS84 ellipsoid by CGVD2013_TO_WGS84 (same as the
terrain). Buildings are grouped into TILE_SIZE grid cells by footprint centre;
each cell is one glb in a local east-north-up frame, placed by the tile transform.
Every building is a feature (EXT_mesh_features) with its attributes in a
property table (EXT_structural_metadata), so it can be picked and styled in
Cesium. The face edges (footprint, roof and wall corners, but not the triangle
diagonals) are stored as CESIUM_primitive_outline, which Cesium draws in the
tileset's outlineColor.

Needs geopandas, pyproj and mapbox-earcut (pip install geopandas mapbox-earcut).
"""

import json
import math
import struct

import geopandas as gpd
import mapbox_earcut as earcut
import numpy as np
from pyproj import Transformer

from load_rasters import DATA_DIR
from extrude_buildings import BUILDINGS_3D_PATH, CRS
from export_terrain import CGVD2013_TO_WGS84

TILESET_DIR = DATA_DIR / "buildings_3dtiles"
TILE_SIZE = 1000.0          # m, grid cell size in EPSG:2961
ROOT_GEOMETRIC_ERROR = 500  # m; tiles load within roughly 25 km of the camera
COLOR = [0.85, 0.83, 0.80, 1.0]

# Attributes carried into the property table: name -> 3D Tiles component type
PROPERTIES = {"feature_id": "STRING", "heightmax": "FLOAT32", "elevmin": "FLOAT32",
              "z_bottom": "FLOAT32", "z_top": "FLOAT32", "bldgarea": "FLOAT32"}

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3


def to_ecef(lon, lat, h):
    lon, lat = np.radians(lon), np.radians(lat)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    return np.column_stack([(n + h) * np.cos(lat) * np.cos(lon),
                            (n + h) * np.cos(lat) * np.sin(lon),
                            (n * (1 - WGS84_E2) + h) * np.sin(lat)])


def enu_frame(lon, lat):
    """Rows east, north, up of the local frame at (lon, lat) in degrees."""
    lo, la = math.radians(lon), math.radians(lat)
    return np.array([[-math.sin(lo), math.cos(lo), 0],
                     [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
                     [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]])


def newell_normal(p):
    n = np.sum(np.cross(p, np.roll(p, -1, axis=0)), axis=0)
    return n / (np.linalg.norm(n) or 1.0)


def triangulate_face(face):
    """Face vertices (k, 3) and triangle indices wound to match the face normal."""
    rings = [np.array(face.exterior.coords)[:-1]] + [np.array(r.coords)[:-1] for r in face.interiors]
    pts = np.vstack(rings)
    if len(rings) == 1 and len(pts) == 4:   # wall quad
        tris = np.array([[0, 1, 2], [0, 2, 3]])
    else:                                   # roof / floor are horizontal: triangulate in xy
        ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
        tris = earcut.triangulate_float64(pts[:, :2], ends).reshape(-1, 3)
    normal = newell_normal(pts)
    tn = np.cross(pts[tris[:, 1]] - pts[tris[:, 0]], pts[tris[:, 2]] - pts[tris[:, 0]])
    flip = tn @ normal < 0
    tris[flip] = tris[flip][:, ::-1]
    return pts, tris


def face_edges(face):
    """Line index pairs along the face's rings, numbered like triangulate_face's vertices."""
    edges, start = [], 0
    for ring in [face.exterior, *face.interiors]:
        k = len(ring.coords) - 1
        edges += [(start + i, start + (i + 1) % k) for i in range(k)]
        start += k
    return np.array(edges)


def _pad(b, n=8, fill=b"\x00"):
    return b + fill * (-len(b) % n)


class GlbBuilder:
    def __init__(self):
        self.bin = b""
        self.views, self.accessors = [], []

    def view(self, data, target=None):
        self.bin = _pad(self.bin)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, arr, gltf_type, component, target, minmax=False):
        a = {"bufferView": self.view(arr.tobytes(), target), "componentType": component,
             "count": len(arr), "type": gltf_type}
        if minmax:
            a["min"], a["max"] = arr.min(axis=0).tolist(), arr.max(axis=0).tolist()
        self.accessors.append(a)
        return len(self.accessors) - 1


def write_glb(path, positions, normals, feature_ids, indices, outlines, props):
    """positions/normals in ENU (east, north, up); written y-up as glTF requires."""
    yup = lambda v: np.column_stack([v[:, 0], v[:, 2], -v[:, 1]]).astype("float32")
    g = GlbBuilder()
    attrs = {"POSITION": g.accessor(yup(positions), "VEC3", 5126, 34962, minmax=True),
             "NORMAL": g.accessor(yup(normals), "VEC3", 5126, 34962),
             "_FEATURE_ID_0": g.accessor(feature_ids.astype("float32"), "SCALAR", 5126, 34962)}
    idx = g.accessor(indices.astype("uint32").ravel(), "SCALAR", 5125, 34963)
    outline = g.accessor(outlines.astype("uint32").ravel(), "SCALAR", 5125, 34963)

    count = len(next(iter(props.values())))
    table = {}
    for name, ctype in PROPERTIES.items():
        if ctype == "STRING":
            enc = [str(s).encode("utf-8") for s in props[name]]
            offsets = np.concatenate([[0], np.cumsum([len(s) for s in enc])]).astype("uint32")
            table[name] = {"values": g.view(b"".join(enc)), "stringOffsets": g.view(offsets.tobytes()),
                           "stringOffsetType": "UINT32"}
        else:
            table[name] = {"values": g.view(np.asarray(props[name], dtype="float32").tobytes())}
    classes = {"building": {"properties": {
        n: {"type": "STRING"} if t == "STRING" else {"type": "SCALAR", "componentType": t}
        for n, t in PROPERTIES.items()}}}

    gltf = {
        "asset": {"version": "2.0", "generator": "3d_port export_buildings_3dtiles.py"},
        "extensionsUsed": ["EXT_mesh_features", "EXT_structural_metadata",
                           "CESIUM_primitive_outline"],
        "extensions": {"EXT_structural_metadata": {
            "schema": {"id": "buildings", "classes": classes},
            "propertyTables": [{"class": "building", "count": count, "properties": table}]}},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{
            "attributes": attrs, "indices": idx, "material": 0, "mode": 4,
            "extensions": {
                "EXT_mesh_features": {"featureIds": [
                    {"featureCount": count, "attribute": 0, "propertyTable": 0}]},
                "CESIUM_primitive_outline": {"indices": outline}}}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": COLOR, "metallicFactor": 0.0,
                                                "roughnessFactor": 1.0}}],
        "buffers": [{"byteLength": len(_pad(g.bin, 4))}],
        "bufferViews": g.views, "accessors": g.accessors,
    }
    js = _pad(json.dumps(gltf, separators=(",", ":")).encode(), 4, b" ")
    bn = _pad(g.bin, 4)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bn)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(bn), 0x004E4942) + bn)


def build_tile(bldg, to_lonlat):
    """Mesh, ENU frame and region for one tile's buildings."""
    pts, tris, edges, fids, face_ranges = [], [], [], [], []
    n = 0
    for fid, geom in enumerate(bldg.geometry):
        for face in geom.geoms:
            p, t = triangulate_face(face)
            pts.append(p)
            tris.append(t + n)
            edges.append(face_edges(face) + n)
            fids.append(np.full(len(p), fid))
            face_ranges.append((n, n + len(p)))
            n += len(p)
    pts, tris, edges, fids = np.vstack(pts), np.vstack(tris), np.vstack(edges), np.concatenate(fids)

    lon, lat = to_lonlat.transform(pts[:, 0], pts[:, 1])
    h = pts[:, 2] + CGVD2013_TO_WGS84
    ecef = to_ecef(lon, lat, h)
    lon0, lat0 = (lon.min() + lon.max()) / 2, (lat.min() + lat.max()) / 2
    origin = to_ecef(np.array([lon0]), np.array([lat0]), np.array([0.0]))[0]
    rot = enu_frame(lon0, lat0)
    enu = (ecef - origin) @ rot.T

    normals = np.empty_like(enu)
    for a, b in face_ranges:
        normals[a:b] = newell_normal(enu[a:b])

    transform = np.eye(4)
    transform[:3, :3] = rot.T          # columns east, north, up in ECEF
    transform[:3, 3] = origin
    region = [math.radians(lon.min()), math.radians(lat.min()),
              math.radians(lon.max()), math.radians(lat.max()), float(h.min()), float(h.max())]
    return enu, normals, fids, tris, edges, transform.T.ravel().tolist(), region  # column-major


def export_tileset(src_path=BUILDINGS_3D_PATH, out_dir=TILESET_DIR):
    bldg = gpd.read_file(src_path)
    if bldg.crs.to_string() != CRS:
        bldg = bldg.to_crs(CRS)
    to_lonlat = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)

    c = bldg.geometry.centroid
    x0, y0 = math.floor(c.x.min() / TILE_SIZE) * TILE_SIZE, math.floor(c.y.min() / TILE_SIZE) * TILE_SIZE
    bldg["_col"] = ((c.x - x0) // TILE_SIZE).astype(int)
    bldg["_row"] = ((c.y - y0) // TILE_SIZE).astype(int)

    (out_dir / "tiles").mkdir(parents=True, exist_ok=True)
    children, n_tris = [], 0
    for (col, row), group in bldg.groupby(["_col", "_row"]):
        enu, normals, fids, tris, edges, transform, region = build_tile(group, to_lonlat)
        uri = f"tiles/{col}_{row}.glb"
        write_glb(out_dir / uri, enu, normals, fids, tris, edges,
                  {name: group[name].tolist() for name in PROPERTIES})
        children.append({"boundingVolume": {"region": region}, "geometricError": 0,
                         "transform": transform, "content": {"uri": uri}})
        n_tris += len(tris)

    regions = np.array([ch["boundingVolume"]["region"] for ch in children])
    root_region = [*regions[:, :2].min(axis=0), *regions[:, 2:4].max(axis=0),
                   regions[:, 4].min(), regions[:, 5].max()]
    tileset = {
        "asset": {"version": "1.1"},
        "geometricError": ROOT_GEOMETRIC_ERROR,
        "root": {"boundingVolume": {"region": [float(v) for v in root_region]},
                 "geometricError": ROOT_GEOMETRIC_ERROR, "refine": "ADD", "children": children},
    }
    (out_dir / "tileset.json").write_text(json.dumps(tileset, indent=1))
    return len(bldg), len(children), n_tris, root_region


if __name__ == "__main__":
    n_bldg, n_tiles, n_tris, region = export_tileset()
    print(f"Exported {n_bldg:,} buildings in {n_tiles} tiles ({n_tris:,} triangles)")
    print(f"  heights  : {region[4]:.2f} to {region[5]:.2f} m WGS84 ellipsoid")
    print(f"Wrote {TILESET_DIR / 'tileset.json'}")
