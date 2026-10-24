import struct
import json
import os
import sqlite3
import shapely.wkb
import shapely.geometry
from shapely.geometry import box, Polygon, MultiPolygon
from shapely.ops import unary_union

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(CURRENT_DIR)
DATA_OUT_DIR = os.path.join(BACKEND_DIR, "data")
FRONTEND_STAFF_DATA = os.path.join(BACKEND_DIR, "..", "frontend", "staff", "src", "data")
FRONTEND_RESIDENT_DATA = os.path.join(BACKEND_DIR, "..", "frontend", "resident", "src", "data")

os.makedirs(DATA_OUT_DIR, exist_ok=True)
os.makedirs(FRONTEND_STAFF_DATA, exist_ok=True)
os.makedirs(FRONTEND_RESIDENT_DATA, exist_ok=True)

# ----------------------------------------------------
# 1. PROCESS FLOOD HAZARDS (StudyAreaFlood_Maasin.gpkg)
# ----------------------------------------------------
gpkg_path = r"c:\Users\johnk\Documents\Capstone\Maasin_LandFlood\Maasin_LandFlood\DATA\StudyAreaFlood_Maasin.gpkg"
conn = sqlite3.connect(gpkg_path)
c = conn.cursor()
c.execute("SELECT fid, floodsusc, geom FROM StudyAreaFlood_Maasin")
flood_features = []
total_bounds = [999.0, 999.0, -999.0, -999.0]

for fid, susc, geom_blob in c.fetchall():
    flags = geom_blob[3]
    env_type = (flags >> 1) & 0x07
    env_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    header_len = 8 + env_sizes.get(env_type, 0)
    geom = shapely.wkb.loads(geom_blob[header_len:])

    b = geom.bounds
    total_bounds[0] = min(total_bounds[0], b[0])
    total_bounds[1] = min(total_bounds[1], b[1])
    total_bounds[2] = max(total_bounds[2], b[2])
    total_bounds[3] = max(total_bounds[3], b[3])

    label = "High Flood" if susc == "HF" else ("Moderate Flood" if susc == "MF" else "Low Flood")
    weight = 1.0 if susc == "HF" else (0.65 if susc == "MF" else 0.35)
    color = "#ef4444" if susc == "HF" else ("#f59e0b" if susc == "MF" else "#06b6d4")

    flood_features.append({
        "type": "Feature",
        "id": f"flood_{fid}",
        "geometry": shapely.geometry.mapping(geom),
        "properties": {
            "id": fid,
            "hazard_type": "flood",
            "code": susc,
            "risk_level": label,
            "weight": weight,
            "color": color,
            "description": f"{label} Susceptibility Zone (DENR-MGB / Study Area)"
        }
    })

flood_geojson = {
    "type": "FeatureCollection",
    "metadata": {"total_features": len(flood_features), "bounds": total_bounds},
    "features": flood_features
}

flood_out_path = os.path.join(DATA_OUT_DIR, "maasin_flood_hazards.json")
with open(flood_out_path, "w", encoding="utf-8") as f:
    json.dump(flood_geojson, f)

print(f"Saved Flood GeoJSON to {flood_out_path} ({len(flood_features)} features, {os.path.getsize(flood_out_path)/1024:.1f} KB)")


# ----------------------------------------------------
# 2. PROCESS LANDSLIDE HAZARDS (sad.shp)
# ----------------------------------------------------
study_box = box(total_bounds[0] - 0.02, total_bounds[1] - 0.02, total_bounds[2] + 0.02, total_bounds[3] + 0.02)
shp_path = r"c:\Users\johnk\Documents\Capstone\Maasin_LandFlood\Maasin_LandFlood\sad.shp"

landslide_features = []
levels_info = {
    1: {"label": "Low Landslide", "code": "LL", "weight": 0.35, "color": "#22c55e"},
    2: {"label": "Moderate Landslide", "code": "ML", "weight": 0.65, "color": "#f97316"},
    3: {"label": "High Landslide", "code": "HL", "weight": 1.0, "color": "#9333ea"},
}

with open(shp_path, "rb") as f:
    f.seek(100)
    for level in [1, 2, 3]:
        info = levels_info[level]
        rec_num, content_len = struct.unpack(">2i", f.read(8))
        raw = f.read(content_len * 2)
        shape_type = struct.unpack("<i", raw[:4])[0]
        bbox = struct.unpack("<4d", raw[4:36])
        num_parts, num_points = struct.unpack("<2i", raw[36:44])
        parts = struct.unpack(f"<{num_parts}i", raw[44:44+num_parts*4])
        pts_offset = 44 + num_parts * 4

        kept_polys = []
        min_bx, min_by, max_bx, max_by = study_box.bounds
        for p_idx in range(num_parts):
            start = parts[p_idx]
            end = parts[p_idx+1] if p_idx+1 < num_parts else num_points
            part_pts = []
            min_x, max_x = 999.0, -999.0
            min_y, max_y = 999.0, -999.0
            for pt_i in range(start, end):
                x, y = struct.unpack("<2d", raw[pts_offset + pt_i*16 : pts_offset + pt_i*16 + 16])
                min_x = min(min_x, x); max_x = max(max_x, x)
                min_y = min(min_y, y); max_y = max(max_y, y)
                part_pts.append((x, y))

            if not (max_x < min_bx or min_x > max_bx or max_y < min_by or min_y > max_by):
                if len(part_pts) >= 4:
                    poly = Polygon(part_pts)
                    if poly.is_valid and poly.intersects(study_box):
                        clipped = poly.intersection(study_box)
                        if not clipped.is_empty:
                            kept_polys.append(clipped)

        print(f"Level {level} ({info['label']}) kept polygons: {len(kept_polys)}")
        if kept_polys:
            merged = unary_union(kept_polys)
            simplified = merged.simplify(0.00008, preserve_topology=True)
            landslide_features.append({
                "type": "Feature",
                "id": f"landslide_{level}",
                "geometry": shapely.geometry.mapping(simplified),
                "properties": {
                    "id": level,
                    "hazard_type": "landslide",
                    "code": info["code"],
                    "risk_level": info["label"],
                    "weight": info["weight"],
                    "color": info["color"],
                    "description": f"{info['label']} Susceptibility Zone (DENR-MGB)"
                }
            })

landslide_geojson = {
    "type": "FeatureCollection",
    "metadata": {"total_features": len(landslide_features)},
    "features": landslide_features
}

landslide_out_path = os.path.join(DATA_OUT_DIR, "maasin_landslide_hazards.json")
with open(landslide_out_path, "w", encoding="utf-8") as f:
    json.dump(landslide_geojson, f)

print(f"Saved Landslide GeoJSON to {landslide_out_path} ({len(landslide_features)} features, {os.path.getsize(landslide_out_path)/1024:.1f} KB)")

# Also copy to frontend staff and resident data directories for fast static import
for dest_dir in [FRONTEND_STAFF_DATA, FRONTEND_RESIDENT_DATA]:
    with open(os.path.join(dest_dir, "maasin_flood_hazards.json"), "w", encoding="utf-8") as f:
        json.dump(flood_geojson, f)
    with open(os.path.join(dest_dir, "maasin_landslide_hazards.json"), "w", encoding="utf-8") as f:
        json.dump(landslide_geojson, f)
    print(f"Copied hazard JSONs to {dest_dir}")

print("Hazard data conversion complete!")
