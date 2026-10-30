import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from models.hazard import HazardCreate, HazardUpdate


class HazardBarangayTests(unittest.TestCase):
    def test_new_hazards_require_a_nonblank_designation(self):
        payload = dict(name='Mapped poultry', hazard_type='poultry_farm',
                       geometry_type='Point', coordinates=[124.83, 10.18])
        for extra in ({}, {'barangay': None}, {'barangay': '  '}):
            with self.assertRaises(ValidationError):
                HazardCreate(**payload, **extra)
        self.assertEqual(HazardCreate(**payload, barangay=' Hantag ').barangay, 'Hantag')

    def test_existing_hazards_can_receive_designation_without_changing_other_fields(self):
        update = HazardUpdate(barangay=' Rizal ')
        self.assertEqual(update.model_dump(exclude_unset=True), {'barangay': 'Rizal'})
        self.assertIsNone(HazardUpdate(notes='Reviewed').barangay)
        for value in (None, '', '  '):
            with self.assertRaises(ValidationError):
                HazardUpdate(barangay=value)


if __name__ == '__main__':
    unittest.main()
