"""Digitize supplied map exports into an approximate interactive study overlay.

Requires numpy, scipy, pillow, contourpy, shapely, rasterio and affine (conversion only).
Run from any directory. Original images and operational/model datasets are untouched.
"""
from pathlib import Path
import json
import sys
import sqlite3
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
if (ROOT / '.tmp/hazard-tools').exists():
    sys.path.insert(0, str(ROOT / '.tmp/hazard-tools'))
import numpy as np
from PIL import Image
import contourpy
from scipy.ndimage import distance_transform_edt, map_coordinates, minimum_filter
from scipy.optimize import minimize
from shapely import make_valid, wkb, set_precision
from shapely.geometry import shape, mapping, Polygon
from shapely.ops import unary_union

GIS = ROOT / 'Maasin_LandFlood/Maasin_LandFlood'
STAFF = ROOT / 'New/frontend/staff'
NAMES = ['Batuan', 'Cambooc', 'Combado', 'Hantag', 'Malapoc Norte',
         'Malapoc Sur', 'Matin-ao', 'Rizal', 'San Isidro', 'Tagnipa']
# Actual map-body colors, sampled from the exports (not the illustrative legend swatches).
CLASSES = [
    ('OUT', 'coverage', 'Outside assessed susceptibility zones', '#fb9c4c', (251,156,76)),
    ('LL', 'landslide', 'Low landslide susceptibility', '#feeb38', (254,235,56)),
    ('ML', 'landslide', 'Moderate landslide susceptibility', '#70ab4a', (112,171,74)),
    ('HL', 'landslide', 'High landslide susceptibility', '#e93526', (233,53,38)),
    ('F', 'flood', 'Susceptible to flooding', '#63336a', (99,51,106)),
]


def polygonal(geom):
    geom = make_valid(geom)
    if geom.geom_type in ('Polygon', 'MultiPolygon'):
        return geom
    return unary_union([polygonal(g) for g in getattr(geom, 'geoms', [])
                        if g.geom_type in ('Polygon', 'MultiPolygon', 'GeometryCollection')])


def classify(image):
    rgb = np.asarray(Image.open(image).convert('RGB'), dtype=np.float32)
    result = np.zeros(rgb.shape[:2], dtype=np.uint8)
    best = np.full(result.shape, 18.0 ** 2)
    samples = [(index, entry[4]) for index, entry in enumerate(CLASSES, 1)]
    # Symbol outlines use distinct, stable colors in every supplied export.
    samples += [(2, (248,211,59)), (3, (96,134,58)), (4, (153,43,28))]
    for index, color in samples:
        distance = np.sum((rgb - np.array(color, dtype=np.float32)) ** 2, axis=2)
        take = distance < best
        result[take] = index
        best[take] = distance[take]
    # Frame, north arrow, legend and all surrounding page content are excluded.
    result[:91] = 0; result[2377:] = 0
    result[:,123] = 0; result[:,:123] = 0; result[:,2316:] = 0
    return result


def vectorize(labels, transform, clip):
    x0, y0, sx, sy = transform  # lon = x0 + pixel_x*sx; lat = y0 - pixel_y*sy
    output = []
    for index in range(1, 6):
        mask = (labels == index).astype(float)
        points, offsets = contourpy.contour_generator(z=mask, fill_type='OuterOffset').filled(0.5, 1.5)
        polygons = []
        for coords, rings in zip(points, offsets):
            if len(coords) < 4:
                continue
            coords[:,0] = x0 + coords[:,0] * sx
            coords[:,1] = y0 - coords[:,1] * sy
            shell = coords[rings[0]:rings[1]]
            holes = [coords[rings[i]:rings[i+1]] for i in range(1,len(rings)-1)]
            poly = polygonal(Polygon(shell, holes))
            if not poly.is_empty and poly.intersects(clip):
                polygons.append(poly.intersection(clip))
        output.append(polygonal(unary_union(polygons)))
    return output


def build():
    fs = json.loads((STAFF / 'src/data/maasin_barangays.json').read_text())['features']
    boundaries = {f['properties']['ADM4_EN'].replace(' (Pob.)', ''): polygonal(shape(f['geometry']))
                  for f in fs if f['properties']['ADM4_EN'].replace(' (Pob.)', '') in NAMES}
    assert set(boundaries) == set(NAMES)
    study = unary_union(list(boundaries.values()))
    with sqlite3.connect(f'file:{(GIS / "DATA/StudyAreaFlood_Maasin.gpkg").as_posix()}?mode=ro', uri=True) as conn:
        flood_parts = []
        for (blob,) in conn.execute('SELECT geom FROM StudyAreaFlood_Maasin'):
            size = {0:0, 1:32, 2:48, 3:48, 4:64}[(blob[3] >> 1) & 7]
            flood_parts.append(polygonal(wkb.loads(blob[8+size:])))
        source_flood = polygonal(unary_union(flood_parts).intersection(study))
    with zipfile.ZipFile(GIS / 'PROJECTS/FLOODING_MAASIN.qgz') as archive:
        project = ET.fromstring(archive.read('FLOODING_MAASIN.qgs'))
    item = next(i for i in project.findall('.//LayoutItem') if i.get('id') == 'Map 1')
    extent = {k: float(v) for k,v in item.find('Extent').attrib.items()}
    px_per_mm = 300 / 25.4
    left, top = [float(n) * px_per_mm for n in item.get('position').split(',')[:2]]
    width, height = [float(n) * px_per_mm for n in item.get('size').split(',')[:2]]
    sx = (extent['xmax']-extent['xmin']) / width
    sy = (extent['ymax']-extent['ymin']) / height
    transform = (extent['xmin']-left*sx, extent['ymax']+top*sy, sx, sy)
    overview = classify(GIS / 'LandFlood_Maasin/10Brgys.png')
    # Close-ups are registered to the overview's visible hazard pattern. This
    # uses the supplied images, not guessed hazard shapes or inferred classes.
    distances = [np.minimum(distance_transform_edt(overview != index), 20).astype(np.float32)
                 for index in range(1,6)]
    overview_geoms = vectorize(overview, transform, study)
    registrations = []
    features = []
    for name in NAMES:
        boundary = boundaries[name]
        image_name = name.replace(' ', '-') + '.png'
        image_path = GIS / 'LandFlood_Maasin' / image_name
        geoms = overview_geoms
        source_image = '10Brgys.png'
        resolution = sx * 111000
        if image_path.exists():
            detail = classify(image_path)
            b = boundary.bounds
            ratio = width / height
            w = max(b[2]-b[0], (b[3]-b[1])*ratio)*1.1
            h = w / ratio
            dsx, dsy = w/width, h/height
            dx0 = (b[0]+b[2]-w)/2-left*dsx
            dy0 = (b[1]+b[3]+h)/2+top*dsy
            # Work in overview-pixel coordinates, centered on the detail map.
            ax, ay = dsx/sx, dsy/sy
            tx, ty = (dx0-transform[0])/sx, (transform[1]-dy0)/sy
            stable = (minimum_filter(detail, size=5) == detail) & (detail > 0)
            yy, xx = np.where(stable)
            indices = np.random.default_rng(42).choice(len(xx), min(10000,len(xx)), replace=False)
            xx, yy = xx[indices].astype(float), yy[indices].astype(float)
            values = detail[yy.astype(int), xx.astype(int)]
            cx, cy = left+width/2, top+height/2
            def positions(params):
                return (ax*(xx-cx)*params[2]+ax*cx+tx+params[0],
                        ay*(yy-cy)*params[3]+ay*cy+ty+params[1])
            def objective(params):
                ox, oy = positions(params)
                cost = 0.0
                for index in range(1,6):
                    sel = values == index
                    cost += map_coordinates(distances[index-1], [oy[sel], ox[sel]], order=1,
                                            mode='constant', cval=20).sum()
                return cost/len(values)
            fitted = minimize(objective, [0,0,1,1], method='Powell',
                              bounds=[(-15,15),(-15,15),(.88,1.12),(.88,1.12)],
                              options={'maxiter': 60, 'xtol': .0001, 'ftol': .0001})
            shift_x, shift_y, scale_x, scale_y = fitted.x
            ox,oy = positions(fitted.x)
            sampled = map_coordinates(overview, [oy,ox], order=0, mode='constant', cval=0)
            agreement = float(np.mean(sampled == values))
            known = sampled > 0
            known_agreement = float(np.mean(sampled[known] == values[known]))
            registration = {'barangay':name, 'image':image_name, 'overview_color_agreement':round(agreement,4),
                            'agreement_on_readable_overview':round(known_agreement,4),
                            'readable_overview_fraction':round(float(known.mean()),4),
                            'mean_color_distance_overview_pixels':round(float(fitted.fun),3),
                            'adjustment':fitted.x.tolist()}
            # Fall back to the georeferenced overview when close-up matching is poor.
            if known_agreement >= .90 and known.mean() >= .5 and fitted.fun < 2.5 and max(abs(shift_x),abs(shift_y)) < 14:
                dt = (transform[0]+(tx+ax*cx*(1-scale_x)+shift_x)*sx,
                      transform[1]-(ty+ay*cy*(1-scale_y)+shift_y)*sy,
                      dsx*scale_x, dsy*scale_y)
                geoms = vectorize(detail, dt, boundary)
                # Recover only actually visible overview pixels beneath close-up
                # annotations; never guess a class behind a pin in both images.
                missing = boundary.difference(unary_union(geoms))
                geoms = [polygonal(g.union(o.intersection(missing))) for g,o in zip(geoms,overview_geoms)]
                source_image = image_name
                resolution = dt[2]*111000
                registration['accepted'] = True
                registration['transform'] = list(dt)
            else:
                registration['accepted'] = False
            registrations.append(registration)
            print('Registration', name, registration, flush=True)
        # The QGIS flood vector survives in the supplied folder; it recovers flood
        # areas obscured by pins without deriving their geometry from pixels.
        geoms = list(geoms)
        geoms[-1] = source_flood
        for order, (geom, entry) in enumerate(zip(geoms, CLASSES)):
            code, kind, label, color, _ = entry
            geom = polygonal(geom.intersection(boundary)).simplify(resolution / 111000 * .3, preserve_topology=True)
            if geom.is_empty:
                continue
            features.append({'type':'Feature','id':f'{name}-{code}','geometry':mapping(geom),
                             'properties':{'category':'natural','kind':kind,'code':code,'barangay':name,
                                           'name':label,'color':color,'draw_order':order,
                                           'source_image':source_image if kind != 'flood' else 'StudyAreaFlood_Maasin.gpkg',
                                           'source':'Digitized supplied map export' if kind != 'flood' else 'Supplied QGIS flood vector',
                                           'approximate':kind != 'flood','image_pixel_meters':round(resolution,2)}})
        features.append({'type':'Feature','id':f'{name}-boundary','geometry':mapping(boundary),
                         'properties':{'category':'natural','kind':'boundary','code':'BOUNDARY','barangay':name,
                                       'name':f'{name} boundary','color':'#3a8038','draw_order':5}})
    metadata = {'title':'Image-derived hazard zones ? 10 study barangays','crs':'EPSG:4326',
                'bounds':list(study.bounds),'barangays':NAMES,'reference_image':'10Brgys.png',
                'overview_transform':list(transform),'registrations':registrations,
                'method':'Overview georeferenced from saved QGIS Map 1 extent and 300 DPI layout; close-ups aligned to overview hazard colors. Visible color pixels polygonized, clipped to existing barangay boundaries. Flood geometry retained from StudyAreaFlood_Maasin.gpkg.',
                'limitations':'Approximate image-derived zones, not original surveyed hazard vectors. Pixels hidden in all source images remain unclassified gaps; flood uses the original supplied vector. Orange comes only from orange pixels; gaps are not labeled safe or outside assessed zones. Existing app boundaries may differ slightly from the exports. Publication date unverified.'}
    result = {'type':'FeatureCollection','metadata':metadata,'features':features}
    output = STAFF / 'public/data/maasin_study_hazards.geojson'
    output.parent.mkdir(parents=True,exist_ok=True)
    # Seven decimals is finer than the source image; rounding keeps downloads small.
    def round_coordinates(value):
        if isinstance(value, (tuple, list)): return [round_coordinates(v) for v in value]
        return round(float(value), 7)
    snapped_boundaries = {name:set_precision(boundary, 0.0000001) for name,boundary in boundaries.items()}
    for feature in features:
        p = feature['properties']
        geom = set_precision(polygonal(shape(feature['geometry'])), 0.0000001)
        geom = polygonal(geom.intersection(snapped_boundaries[p['barangay']]))
        assert geom.is_valid and not geom.is_empty, feature['id']
        feature['geometry'] = mapping(geom)
        feature['geometry']['coordinates'] = round_coordinates(feature['geometry']['coordinates'])
    from repair_staff_hazard_icons import repair_dataset
    result = repair_dataset(result)
    output.write_text(json.dumps(result,separators=(',',':')),encoding='utf-8')
    (ROOT / 'New/backend/data/maasin_study_hazards.geojson').write_bytes(output.read_bytes())
    print(f'{output}: {len(result["features"])} features, {output.stat().st_size:,} bytes',flush=True)
    return result


if __name__ == '__main__':
    build()
