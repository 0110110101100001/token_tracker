# tests/test_pricing.py
import unittest
from cost_meter.pricing import UnknownModel, price_event

PRICING = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
}


class TestPriceEvent(unittest.TestCase):
    def test_input_and_output_tokens(self):
        usd = price_event(PRICING, "claude-opus-5", 1_000_000, 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(usd, 30.0)

    def test_cache_read_is_a_tenth_of_input(self):
        usd = price_event(PRICING, "claude-opus-5", 0, 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(usd, 0.5)

    def test_cache_writes_use_their_own_multipliers(self):
        usd = price_event(PRICING, "claude-opus-5", 0, 0, 1_000_000, 1_000_000, 0)
        self.assertAlmostEqual(usd, 5.0 * 1.25 + 5.0 * 2.0)

    def test_rates_are_per_model(self):
        usd = price_event(PRICING, "claude-sonnet-5", 1_000_000, 0, 0, 0, 0)
        self.assertAlmostEqual(usd, 3.0)

    def test_unknown_model_raises_and_names_the_model(self):
        with self.assertRaises(UnknownModel) as ctx:
            price_event(PRICING, "claude-nonexistent-9", 100, 100, 0, 0, 0)
        self.assertEqual(ctx.exception.model, "claude-nonexistent-9")


if __name__ == "__main__":
    unittest.main()
