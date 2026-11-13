import copy
import json
import tempfile
import unittest
from pathlib import Path


def load(name):
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / name).read_text(encoding='utf-8'))


from latency_tile_store.histogram import validate_bins, merge_histograms, threshold_bounds, inspect


class BusinessTests(unittest.TestCase):
    def test_count_conservation(self):
        self.assertEqual(inspect(load('histograms.json'))['count'], 15)

    def test_merge_order(self):
        groups = [[[0, 10, 3]], [[0, 10, 4]]]
        self.assertEqual(merge_histograms(groups), merge_histograms(groups[::-1]))

    def test_ambiguous_threshold(self):
        self.assertEqual(threshold_bounds([[0, 10, 5]], 4), {'lower': 0, 'upper': 5})

    def test_nonmatching_edges(self):
        with self.assertRaises(ValueError):
            merge_histograms([[[0, 1, 1]], [[0, 2, 1]]])

    def test_overlap(self):
        with self.assertRaises(ValueError):
            validate_bins([[0, 2, 1], [1, 3, 1]])

    def test_bad_frequency(self):
        for count in [-1, 1.5, True]:
            with self.assertRaises(ValueError):
                validate_bins([[0, 1, count]])
