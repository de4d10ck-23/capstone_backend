"""Shared study-map hazard assessment. No database or environment access on import.

Weights express an uncalibrated risk adjustment, never a laboratory result.
"""
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.affinity import scale
from shapely.prepared import prep
from shapely.errors import ShapelyError

DATA = Path(__file__).resolve().parents[1] / 'data'
STUDY_PATH = DATA / 'maasin_study_hazards.geojson'
WEIGHTS_PATH = DATA / 'hazard_risk_weights.json'
HAZARD_MODEL_VERSION = 'study-hazards-v1'
LABELS = {'F': 'Flood susceptible', 'HL': 'High landslide susceptibility',
          'ML': 'Moderate landslide susceptibility', 'LL': 'Low landslide susceptibility',
          'OUT': 'Outside assessed susceptibility zones', 'livestock': 'Poultry & livestock',
          'sanitation': 'Septic tanks & sanitation', 'agriculture': 'Agriculture',
          'waste': 'Waste & wastewater', 'waterways': 'Waterways & drainage', 'other': 'Other mapped sources'}


def barangay_key(value):
    value = re.sub(r'^(barangay|brgy\.?)\s*', '', str(value or '').strip().lower())
    value = re.sub(r'\(pob\.?\)', '', value)
    return re.sub(r'[^a-z0-9]', '', value)


def hazard_category(value):
    value = re.sub(r'[_-]', ' ', str(value or '').lower())
    for category, pattern in [('livestock', 'poultry|piggery|livestock|animal|cattle|swine|chicken'),
                              ('sanitation', 'septic|latrine|toilet|sanitation'),
                              ('waste', 'waste|sewage|landfill|dump'),
                              ('agriculture', 'agricultur|farmland|farm|crop'),
                              ('waterways', 'river|stream|drain|canal|creek')]:
        if re.search(pattern, value):
            return category
    return 'other'


@lru_cache(maxsize=2)
def _read_study(path, modified):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not data.get('features') or not data.get('metadata', {}).get('barangays'):
        raise RuntimeError('Study hazard dataset is invalid')
    return data


def load_study_data():
    # Missing deployment data must fail visibly, never fall back to legacy maps.
    return _read_study(str(STUDY_PATH), STUDY_PATH.stat().st_mtime_ns)


@lru_cache(maxsize=2)
def _prepared_study(modified):
    return [(f['properties'], prep(shape(f['geometry']))) for f in load_study_data()['features']]


def load_weights():
    config = json.loads(WEIGHTS_PATH.read_text(encoding='utf-8'))
    for weight in list(config['natural'].values()) + [v['weight'] for v in config['sources'].values()]:
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError('Hazard weights must be between 0 and 1')
    for value in config['sources'].values():
        if not math.isfinite(value['radius_m']) or value['radius_m'] <= 0:
            raise ValueError('Hazard search radii must be positive')
    for key in ('inferred_factor', 'maximum_combined_weight', 'dry_weather_factor'):
        if not math.isfinite(config[key]) or not 0 <= config[key] <= 1:
            raise ValueError(f'Invalid {key}')
    if not math.isfinite(config['full_runoff_rainfall_48h_mm']) or config['full_runoff_rainfall_48h_mm'] <= 0:
        raise ValueError('Rainfall scale must be positive')
    return config


def fetch_hazard_records():
    from core.database import get_supabase
    sb = get_supabase()
    records = []
    while True:
        page = sb.table('contamination_hazards').select('*').order('id').range(len(records), len(records)+499).execute().data
        if page is None:
            raise RuntimeError('Mapped hazard inventory unavailable')
        records.extend(page)
        if len(page) < 500:
            return records


def assess_station_hazards(lat, lng, barangay, records, *, features=None, weights=None):
    lat, lng = float(lat), float(lng)
    if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ValueError('Invalid station coordinates')
    config = weights or load_weights()
    point, area = Point(lng, lat), barangay_key(barangay)
    zones = _prepared_study(STUDY_PATH.stat().st_mtime_ns) if features is None else [
        (f['properties'], prep(shape(f['geometry']))) for f in features]
    matches = []
    covered = False
    for props, polygon in zones:
        if area and barangay_key(props.get('barangay')) != area:
            continue
        if not polygon.covers(point):
            continue
        if props['kind'] == 'boundary':
            covered = True
        else:
            matches.append(props)
    contributions = []
    # At most one contribution per natural type; observed pixels win over estimates.
    for kind in ('flood', 'landslide'):
        candidates = [p for p in matches if p['kind'] == kind]
        if not candidates:
            continue
        observed = [p for p in candidates if not p.get('inferred')]
        chosen = max(observed or candidates, key=lambda p: config['natural'].get(p['code'], 0))
        weight = config['natural'][chosen['code']]
        factor = config['inferred_factor'] if chosen.get('inferred') else 1.0
        contributions.append(dict(category='natural', kind=kind, code=chosen['code'],
            label=LABELS[chosen['code']], weight=weight, exposure=1.0, confidence_factor=factor,
            contribution=round(weight*factor, 6), inferred=bool(chosen.get('inferred')),
            source=chosen.get('source'), barangay=chosen.get('barangay')))
    nearest = {}
    invalid = 0
    for record in records:
        # Match the staff map's designated-barangay scope; no guessed assignments.
        if not area or barangay_key(record.get('barangay')) != area:
            continue
        category = hazard_category(record.get('hazard_type'))
        try:
            geom = shape({'type': record.get('geometry_type') or 'Point', 'coordinates': record['coordinates']})
            if geom.is_empty or not geom.is_valid or not all(math.isfinite(x) for x in geom.bounds):
                raise ValueError('Invalid geometry')
            # Local metric projection supports points, lines, polygons AND holes.
            projected = scale(geom, xfact=111320*math.cos(math.radians(lat)), yfact=111320, origin=(lng, lat))
            distance = projected.distance(point)
        except (ValueError, TypeError, KeyError, IndexError, ShapelyError):
            invalid += 1
            continue
        rule = config['sources'][category]
        exposure = max(0.0, 1-distance/rule['radius_m'])
        if exposure <= 0:
            continue
        item = dict(category=category, code=category, label=LABELS[category],
                    hazard_id=record.get('id'), name=record.get('name'), barangay=record.get('barangay'),
                    distance_m=round(distance, 1), radius_m=rule['radius_m'], weight=rule['weight'],
                    exposure=round(exposure, 6), confidence_factor=1.0,
                    contribution=round(rule['weight']*exposure, 6), inferred=False)
        # Repeated/overlapping records do not multiply the same category's weight.
        if category not in nearest or item['contribution'] > nearest[category]['contribution']:
            nearest[category] = item
    contributions.extend(nearest.values())
    flood = next((c for c in contributions if c.get('kind') == 'flood'), None)
    landslide = next((c for c in contributions if c.get('kind') == 'landslide'), None)
    coverage = ('outside_assessed' if any(p['code'] == 'OUT' for p in matches) and not flood and not landslide
                else 'mapped' if flood or landslide else 'unclassified' if covered else 'no_data')
    return dict(version=config['version'], calibration=config['calibration'], coverage=coverage,
        contributions=contributions, combined_weight=round(min(config['maximum_combined_weight'],
            sum(c['contribution'] for c in contributions)), 6), invalid_inventory_records=invalid,
        flood_label=flood['label'] if flood else 'Unknown / not mapped',
        landslide_label=landslide['label'] if landslide else 'Unknown / not mapped',
        flood_level=1 if flood else 0,
        landslide_level={'LL': 0, 'ML': 1, 'HL': 2}.get(landslide['code'], 0) if landslide else 0)


def apply_hazard_adjustment(base_probability, assessment, rainfall_48h, *, weights=None):
    config = weights or load_weights()
    base, rain = float(base_probability), float(rainfall_48h)
    if not math.isfinite(base) or not 0 <= base <= 1 or not math.isfinite(rain):
        raise ValueError('Invalid model probability or rainfall')
    runoff = min(1.0, max(0.0, rain)/config['full_runoff_rainfall_48h_mm'])
    weather_factor = config['dry_weather_factor'] + (1-config['dry_weather_factor'])*runoff
    adjustment = assessment['combined_weight']*weather_factor
    score = round(base+(1-base)*adjustment, 4)
    return dict(risk_score=score, base_model_probability=round(base, 4),
                hazard_adjustment=round(score-base, 4), weather_factor=round(weather_factor, 4),
                score_kind='uncalibrated_hybrid_risk_index')
