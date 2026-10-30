"""Validate the committed display dataset without network or a running database."""
import json
import hashlib
from pathlib import Path
import unittest

from shapely.geometry import shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[3]


class StudyHazardDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / 'New/frontend/staff/public/data/maasin_study_hazards.geojson').read_text(encoding='utf-8'))
        cls.features = cls.data['features']
        cls.boundaries = {f['properties']['barangay']: shape(f['geometry'])
                          for f in cls.features if f['properties']['kind'] == 'boundary'}

    def test_all_ten_barangays_have_valid_geographic_boundaries(self):
        self.assertEqual(len(self.boundaries), 10)
        self.assertEqual(set(self.boundaries), set(self.data['metadata']['barangays']))
        for feature in self.features:
            geom = shape(feature['geometry'])
            self.assertTrue(geom.is_valid, feature['id'])
            self.assertFalse(geom.is_empty, feature['id'])
            self.assertIn(geom.geom_type, ('Polygon', 'MultiPolygon'))
            x1, y1, x2, y2 = geom.bounds
            self.assertTrue(124.80 < x1 <= x2 < 124.86)
            self.assertTrue(10.12 < y1 <= y2 < 10.24)

    def test_zones_are_clipped_to_their_barangay(self):
        for feature in self.features:
            p = feature['properties']
            if p['kind'] == 'boundary':
                continue
            geom = shape(feature['geometry'])
            # Allow coordinate rounding by considerably less than a source pixel.
            self.assertLess(geom.difference(self.boundaries[p['barangay']].buffer(0.0000002)).area, 1e-12, feature['id'])

    def test_sources_and_unknown_coverage_are_not_misrepresented(self):
        zones = [f for f in self.features if f['properties']['kind'] != 'boundary']
        self.assertEqual({f['properties']['code'] for f in zones}, {'LL', 'ML', 'HL', 'OUT', 'F'})
        for feature in zones:
            p = feature['properties']
            self.assertTrue(p['source_image'])
            if p['code'] == 'F':
                self.assertEqual(p['source_image'], 'StudyAreaFlood_Maasin.gpkg')
                self.assertFalse(p['approximate'])
            else:
                self.assertTrue(p['approximate'])
            if p['code'] == 'OUT':
                self.assertEqual(p['name'], 'Outside assessed susceptibility zones')
        self.assertIn('unclassified gaps', self.data['metadata']['limitations'])

    def test_icon_repairs_preserve_original_polygons_and_flood(self):
        original = [f for f in self.features if not f['properties'].get('inferred')]
        digest = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, 'a9b8711aa58dd0f369ce25ac84a1e2d9e22baf6f524743e31dd2e1e6d91befe6')
        self.assertEqual(digest, self.data['metadata']['icon_repair']['original_features_sha256'])
        self.assertEqual(len(original), 60)
        estimates = [f for f in self.features if f['properties'].get('inferred')]
        self.assertGreater(len(estimates), 0)
        observed = {name: unary_union([shape(f['geometry']) for f in original
                    if f['properties']['barangay'] == name and f['properties']['kind'] != 'boundary'])
                    for name in self.boundaries}
        for feature in estimates:
            props = feature['properties']
            self.assertNotEqual(props['code'], 'F')
            self.assertTrue(props['approximate'])
            self.assertIn('Estimated continuation', props['source'])
            self.assertLess(shape(feature['geometry']).intersection(observed[props['barangay']]).area,
                            1e-12, feature['id'])
        areas = self.data['metadata']['icon_repair']['areas']
        # Some coastal gaps have no non-flood donor within 150 m and remain unknown.
        self.assertGreater(sum(a['filled_area_degrees2'] for a in areas) /
                           sum(a['target_area_degrees2'] for a in areas), .99)


if __name__ == '__main__':
    unittest.main()
