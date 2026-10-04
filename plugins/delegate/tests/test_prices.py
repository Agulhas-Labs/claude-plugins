"""The agent band's price table agrees with cache-guard's. Run: python3 -m unittest discover -s plugins/delegate/tests"""
import json
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = os.path.join(HERE, "..", "hooks", "prices.json")
CACHE_GUARD_HOOKS = os.path.join(HERE, "..", "..", "cache-guard", "hooks")
sys.path.insert(0, os.path.abspath(CACHE_GUARD_HOOKS))

import cache_guard  # noqa: E402

FIVE_MINUTES = 300


def load():
    with open(PRICES, encoding="utf-8") as f:
        return json.load(f)


class PricesAgreeWithCacheGuard(unittest.TestCase):
    def test_same_families_in_the_same_order(self):
        # The band matches families first-to-last, as cache-guard does, so the order is part of the table.
        self.assertEqual(list(load()["families"]), list(cache_guard.INPUT_PRICES))

    def test_rates_match_cache_guard(self):
        for family, rates in load()["families"].items():
            with self.subTest(family=family):
                write, read = cache_guard.prices(family, FIVE_MINUTES, {})
                self.assertTrue(math.isclose(rates["input"], cache_guard.INPUT_PRICES[family]))
                self.assertTrue(math.isclose(rates["cache_write"], write), (rates["cache_write"], write))
                self.assertTrue(math.isclose(rates["cache_read"], read), (rates["cache_read"], read))

    def test_every_family_has_four_positive_rates(self):
        for family, rates in load()["families"].items():
            with self.subTest(family=family):
                self.assertEqual(set(rates), {"input", "output", "cache_read", "cache_write"})
                self.assertTrue(all(value > 0 for value in rates.values()))

    def test_says_where_the_numbers_come_from(self):
        self.assertTrue(load()["measured"].strip())


if __name__ == "__main__":
    unittest.main()
