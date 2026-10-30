"""Check that continuation stays local and cannot rewrite observed classes."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from repair_staff_hazard_icons import continue_classes
import numpy as np


class IconContinuationTests(unittest.TestCase):
    def test_nearest_classes_continue_across_a_marker_without_filling_other_gaps(self):
        observed = np.zeros((7, 9), dtype=np.uint8)
        observed[:, 1] = 1
        observed[:, 7] = 4
        targets = np.zeros_like(observed, dtype=bool)
        targets[2:5, 1:8] = True
        before = observed.copy()
        filled = continue_classes(observed, targets, 10)
        self.assertEqual(filled[3, 2], 1)
        self.assertEqual(filled[3, 6], 4)
        self.assertFalse(np.any(filled[~targets]))
        self.assertFalse(np.any(filled[observed > 0]))
        np.testing.assert_array_equal(observed, before)

    def test_no_donor_or_distant_donor_leaves_unknown_pixels(self):
        observed = np.zeros((10, 10), dtype=np.uint8)
        targets = np.ones_like(observed, dtype=bool)
        self.assertFalse(np.any(continue_classes(observed, targets, 2)))
        observed[0, 0] = 3
        filled = continue_classes(observed, targets, 2)
        self.assertEqual(filled[0, 1], 3)
        self.assertEqual(filled[9, 9], 0)


if __name__ == '__main__':
    unittest.main()
