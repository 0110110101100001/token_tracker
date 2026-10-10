# tests/test_pricing.py
import unittest
from cost_meter import paths
from cost_meter.pricing import UnknownModel, load_pricing, price_event

PRICING = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    # A published cache-read rate of its own, below the usual tenth of input.
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
}


class TestPriceEvent(unittest.TestCase):
    def test_input_and_output_tokens(self):
        usd = price_event(PRICING, "claude-opus-5", 1_000_000, 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(usd, 30.0)

    def test_cache_read_is_a_tenth_of_input(self):
        usd = price_event(PRICING, "claude-opus-5", 0, 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(usd, 0.5)

    def test_a_models_own_cache_read_rate_replaces_the_tenth(self):
        # Fable 5.1 reads its cache at $0.25 per million, not at a tenth of its
        # $10 input rate; a table entry that says so is what is charged.
        usd = price_event(PRICING, "claude-fable-5-1", 0, 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(usd, 0.25)

    def test_the_cache_read_rate_leaves_the_other_rates_alone(self):
        usd = price_event(PRICING, "claude-fable-5-1", 1_000_000, 1_000_000,
                          1_000_000, 0, 0)
        self.assertAlmostEqual(usd, 10.0 + 50.0 + 10.0 * 1.25)

    def test_cache_writes_use_their_own_multipliers(self):
        usd = price_event(PRICING, "claude-opus-5", 0, 0, 1_000_000, 1_000_000, 0)
        self.assertAlmostEqual(usd, 5.0 * 1.25 + 5.0 * 2.0)

    def test_rates_are_per_model(self):
        usd = price_event(PRICING, "claude-sonnet-5", 1_000_000, 0, 0, 0, 0)
        self.assertAlmostEqual(usd, 3.0)

    def test_long_prompt_tier_applies_only_above_its_threshold(self):
        pricing = {"m": {"input": 1.0, "output": 2.0,
                         "long_prompt": {"over": 100, "input": 10.0, "output": 20.0}}}
        at = price_event(pricing, "m", 100, 1_000_000, 0, 0, 0)
        over = price_event(pricing, "m", 60, 1_000_000, 20, 0, 21)
        self.assertAlmostEqual(at, 100 / 1e6 + 2.0)
        self.assertAlmostEqual(over, (60 + 20 * 1.25 + 21 * 0.1) * 10 / 1e6 + 20.0)

    def test_unknown_model_raises_and_names_the_model(self):
        with self.assertRaises(UnknownModel) as ctx:
            price_event(PRICING, "claude-nonexistent-9", 100, 100, 0, 0, 0)
        self.assertEqual(ctx.exception.model, "claude-nonexistent-9")


class TestShippedTable(unittest.TestCase):
    """The real pricing.json, which is where a missing model actually goes wrong."""

    def setUp(self):
        self.pricing = load_pricing(paths.pricing_path())

    def test_opus_5_5_is_priced(self):
        # $4 / $20 per million, cache reads at $0.20 -- a twentieth of input,
        # so the entry needs its own `cache_read`; the tenth would read $0.40.
        usd = price_event(self.pricing, "claude-opus-5-5", 1_000_000, 1_000_000,
                          0, 0, 1_000_000)
        self.assertAlmostEqual(usd, 4.0 + 20.0 + 0.2)

    def test_sonnet_5_5_is_priced(self):
        # $2 / $10 per million, cache reads at $0.20 -- the usual tenth of
        # input, so the entry needs no `cache_read` of its own.
        usd = price_event(self.pricing, "claude-sonnet-5-5", 1_000_000, 1_000_000,
                          0, 0, 1_000_000)
        self.assertAlmostEqual(usd, 2.0 + 10.0 + 0.2)

    def test_haiku_5_5_is_priced_up_to_100k(self):
        # $0.10 / $0.50 per million while the prompt is 100K tokens or fewer;
        # cache reads are the usual tenth of input.
        usd = price_event(self.pricing, "claude-haiku-5-5", 50_000, 1_000_000,
                          0, 0, 50_000)
        self.assertAlmostEqual(usd, 0.005 + 0.5 + 0.0005)

    def test_haiku_5_5_over_100k_is_priced_at_the_long_prompt_rates(self):
        # Cache reads count toward the prompt: 1K fresh input over 100K cached
        # puts the whole message on $0.50 / $2.50, reads at $0.05.
        usd = price_event(self.pricing, "claude-haiku-5-5", 1_000, 1_000_000,
                          0, 0, 100_000)
        self.assertAlmostEqual(usd, 0.0005 + 2.5 + 0.005)


if __name__ == "__main__":
    unittest.main()
