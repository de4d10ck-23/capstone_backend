"""Continue visible classes ONLY beneath detected printed water-source icons.

This is a display estimate, not new official hazard data. Original polygons and
flood geometry are retained unchanged. Requires the conversion-only workspace
packages documented by build_staff_study_hazards.py, plus rasterio.
"""
from pathlib import Path
import hashlib
import json
import math
import sys

ROOT = Path(__file__).resolve().parents[3]
if (ROOT / '.tmp/hazard-tools').exists():
    sys.path.insert(0, str(ROOT / '.tmp/hazard-tools'))

import numpy as np
from scipy.ndimage import (binary_dilation, binary_fill_holes, distance_transform_edt,
                           find_objects, label)
from shapely import make_valid, set_precision
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


def continue_classes(observed, targets, max_distance_pixels):
    """Nearest visible class; never overwrite observations or fill other gaps."""
    estimated = np.zeros_like(observed)
    if not np.any(observed):
        return estimated
    distance, nearest = distance_transform_edt(observed == 0, return_indices=True)
    allowed = targets & (observed == 0) & (distance <= max_distance_pixels)
    estimated[allowed] = observed[tuple(nearest[:, allowed])]
    return estimated


def polygonal(geom):
    geom = make_valid(geom)
    if geom.geom_type in ('Polygon', 'MultiPolygon'):
        return geom
    return unary_union([polygonal(g) for g in getattr(geom, 'geoms', [])
                        if g.geom_type in ('Polygon', 'MultiPolygon', 'GeometryCollection')])


def icon_mask(image_path):
    from PIL import Image
    rgb = np.asarray(Image.open(image_path).convert('RGB'), dtype=np.int16)
    red, green, blue = rgb.transpose(2, 0, 1)
    blue_core = (red < 60) & (green > 55) & (green < 190) & (blue > 180) & (blue-green > 60)
    blue_core[:91] = False; blue_core[2377:] = False
    blue_core[:, :123] = False; blue_core[:, 2316:] = False
    components, _ = label(blue_core)
    counts = np.bincount(components.ravel())
    accepted = np.zeros(counts.size, dtype=bool)
    for number, bbox in enumerate(find_objects(components), 1):
        if bbox is None:
            continue
        # Reject ocean/background patches; retain individual and overlapping pins.
        h, w = (s.stop-s.start for s in bbox)
        accepted[number] = 20 <= counts[number] <= 40000 and h <= 500 and w <= 500
    core = accepted[components]
    # Include black outlines, the droplet center and the icon's printed shadow.
    yy, xx = np.ogrid[-7:8, -7:8]
    return binary_fill_holes(binary_dilation(core, structure=xx*xx+yy*yy <= 49))


def repair_dataset(data):
    from affine import Affine
    from rasterio.features import rasterize, shapes

    original = [f for f in data['features'] if not f['properties'].get('inferred')]
    baseline_hash = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    boundaries = {f['properties']['barangay']: shape(f['geometry']) for f in original
                  if f['properties']['kind'] == 'boundary'}
    study = unary_union(list(boundaries.values()))
    images = [('10Brgys.png', data['metadata']['overview_transform'])]
    images.extend((r['image'], r['transform']) for r in data['metadata']['registrations'] if r['accepted'])
    masks = []
    for image_name, transform in images:
        x0, y0, sx, sy = transform
        mask = icon_mask(ROOT / 'Maasin_LandFlood/Maasin_LandFlood/LandFlood_Maasin' / image_name)
        affine = Affine(sx, 0, x0, 0, -sy, y0)
        polygons = [shape(g) for g, _ in shapes(mask.astype(np.uint8), mask=mask, transform=affine)]
        masks.append(polygonal(unary_union(polygons).intersection(study)))
    marker_mask = polygonal(unary_union(masks))
    result = list(original)
    audit = []
    # About 2 metres per pixel; the original overview pixels are about 5 metres.
    pixel_meters = 2.0
    sx = pixel_meters / (111320 * math.cos(math.radians(10.18)))
    sy = pixel_meters / 111320
    max_distance_meters = 150
    codes = ['OUT', 'LL', 'ML', 'HL']
    for name, boundary in boundaries.items():
        features = [f for f in original if f['properties']['barangay'] == name and f['properties']['kind'] != 'boundary']
        observed_geom = polygonal(unary_union([shape(f['geometry']) for f in features]))
        target = polygonal(marker_mask.intersection(boundary).difference(observed_geom))
        if target.is_empty:
            continue
        xmin, ymin, xmax, ymax = boundary.bounds
        affine = Affine(sx, 0, xmin-sx, 0, -sy, ymax+sy)
        dimensions = (math.ceil((ymax-ymin)/sy)+2, math.ceil((xmax-xmin)/sx)+2)
        donors = [(f['geometry'], codes.index(f['properties']['code'])+1) for f in features if f['properties']['code'] in codes]
        if not donors:
            continue
        observed = rasterize(donors, out_shape=dimensions, transform=affine, dtype='uint8')
        target_pixels = rasterize([(mapping(target), 1)], out_shape=dimensions, transform=affine,
                                  dtype='uint8', all_touched=True).astype(bool)
        # Cover the whole gap, including cells that straddle its edge. Original
        # observations are protected by the final exact vector intersection.
        observed[target_pixels] = 0
        estimates = continue_classes(observed, target_pixels, max_distance_meters / pixel_meters)
        parts = {i: [] for i in range(1, 5)}
        for geom, value in shapes(estimates, mask=estimates > 0, transform=affine):
            parts[int(value)].append(shape(geom))
        filled = []
        for index, polygons in parts.items():
            if not polygons:
                continue
            # Clip after precision reduction to preserve the
            # original zones exactly, including the original flood boundaries.
            geom = set_precision(polygonal(unary_union(polygons)), 1e-7)
            geom = polygonal(set_precision(geom, 0).intersection(target))
            if geom.is_empty:
                continue
            source = next(f for f in features if f['properties']['code'] == codes[index-1])
            props = {**source['properties'], 'inferred': True, 'approximate': True,
                     'source': 'Estimated continuation beneath printed water-source icons',
                     'estimate_method': 'Nearest visible class in the same barangay; original flood geometry takes precedence',
                     'max_inference_distance_m': max_distance_meters}
            result.append({'type': 'Feature', 'id': f'{name}-{codes[index-1]}-icon-estimate',
                           'geometry': mapping(geom), 'properties': props})
            filled.append(geom)
        audit.append({'barangay': name, 'target_area_degrees2': target.area,
                      'filled_area_degrees2': unary_union(filled).area})
    data['features'] = result
    data['metadata']['icon_repair'] = {
        'method': 'Detected blue marker silhouettes plus 7 image pixels for their outlines; nearest visible same-barangay non-flood class continued only inside existing gaps under those silhouettes.',
        'original_features_sha256': baseline_hash, 'original_feature_count': len(original),
        'estimated_feature_count': len(result)-len(original), 'max_distance_meters': max_distance_meters,
        'grid_resolution_meters': pixel_meters, 'images': [name for name, _ in images], 'areas': audit,
    }
    data['metadata']['limitations'] = ('Approximate image-derived zones, not original surveyed hazard vectors. '
        'Icon-covered patches are estimated from nearby visible classes and marked inferred; these are not verified hazard assessments. '
        'Other unclassified gaps remain. Original flood geometry and original visible polygons are unchanged. '
        'Orange is an outside-assessed classification, not a safety label. Publication date unverified.')
    print('Icon repair:', json.dumps(data['metadata']['icon_repair']), flush=True)
    return data


if __name__ == '__main__':
    path = ROOT / 'New/frontend/staff/public/data/maasin_study_hazards.geojson'
    data = repair_dataset(json.loads(path.read_text(encoding='utf-8')))
    path.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
    (ROOT / 'New/backend/data/maasin_study_hazards.geojson').write_bytes(path.read_bytes())
