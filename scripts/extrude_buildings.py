"""
Extrude the building footprints into closed 3D solids.

  Input    DATA_DIR/building_proj.gpkg  (EPSG:4617 footprints, NRCan automatically
           extracted buildings)
  Output   DATA_DIR/building_3d.gpkg, layer building_3d
           EPSG:2961, CGVD2013 heights (same as the DTM)

Each building becomes a MultiPolygon Z of floor + roof + one quad per wall edge,
with faces oriented so their normals point out of the solid.
  bottom   elevmin               (ground elevation, m CGVD2013)
  top      elevmin + heightmax   (heightmax is height above ground, m)
Buildings with heightmax <= 0 are dropped.

Needs geopandas (pip install geopandas).
"""

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from load_rasters import DATA_DIR

BUILDINGS_PATH = DATA_DIR / "building_proj.gpkg"
BUILDINGS_3D_PATH = DATA_DIR / "building_3d.gpkg"
CRS = "EPSG:2961"


def _ring_xy(ring):
    """Ring coordinates without the closing point or repeated vertices."""
    pts = []
    for x, y in list(ring.coords)[:-1]:
        if not pts or (x, y) != pts[-1]:
            pts.append((x, y))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def extrude(footprint, bottom, top):
    """Closed solid between `bottom` and `top` over a (Multi)Polygon footprint."""
    parts = footprint.geoms if isinstance(footprint, MultiPolygon) else [footprint]
    faces = []
    for poly in parts:
        poly = orient(poly, 1.0)  # exterior CCW, holes CW
        rings = [_ring_xy(poly.exterior)] + [_ring_xy(r) for r in poly.interiors]
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            continue
        ext, holes = rings[0], rings[1:]
        # Roof faces up (CCW from above); floor faces down (reversed)
        faces.append(Polygon([(x, y, top) for x, y in ext],
                             [[(x, y, top) for x, y in h] for h in holes]))
        faces.append(Polygon([(x, y, bottom) for x, y in reversed(ext)],
                             [[(x, y, bottom) for x, y in reversed(h)] for h in holes]))
        # Walls: a0 -> b0 -> b1 -> a1 points outward for CCW exteriors and CW holes
        for ring in rings:
            for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
                faces.append(Polygon([(ax, ay, bottom), (bx, by, bottom),
                                      (bx, by, top), (ax, ay, top)]))
    return MultiPolygon(faces)


def extrude_buildings(src_path=BUILDINGS_PATH, dst_path=BUILDINGS_3D_PATH):
    bldg = gpd.read_file(src_path).to_crs(CRS)
    dropped = (bldg.heightmax <= 0).sum()
    bldg = bldg[bldg.heightmax > 0].copy()
    bldg["z_bottom"] = bldg.elevmin
    bldg["z_top"] = bldg.elevmin + bldg.heightmax
    bldg["geometry"] = [extrude(g, b, t) for g, b, t
                        in zip(bldg.geometry, bldg.z_bottom, bldg.z_top)]
    bldg.to_file(dst_path, layer="building_3d", driver="GPKG")
    return bldg, dropped


if __name__ == "__main__":
    bldg, dropped = extrude_buildings()
    print(f"Extruded {len(bldg):,} buildings ({dropped} with heightmax <= 0 dropped)")
    print(f"  bottom   : {bldg.z_bottom.min():.2f} to {bldg.z_bottom.max():.2f} m CGVD2013")
    print(f"  top      : {bldg.z_top.min():.2f} to {bldg.z_top.max():.2f} m CGVD2013")
    print(f"  faces    : {sum(len(g.geoms) for g in bldg.geometry):,}")
    print(f"Wrote {BUILDINGS_3D_PATH}")
