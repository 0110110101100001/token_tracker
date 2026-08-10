# cost_meter/pricing.py
"""Pure pricing arithmetic. No I/O beyond loading the rate table."""

import json

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
TOKENS_PER_UNIT = 1_000_000


class UnknownModel(Exception):
    """Raised when a model has no entry in the pricing table."""

    def __init__(self, model):
        super().__init__(f"no pricing entry for model {model!r}")
        self.model = model


def load_pricing(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def price_event(
    pricing,
    model,
    input_tokens,
    output_tokens,
    cache_write_5m,
    cache_write_1h,
    cache_read,
):
    """Return the USD cost of one assistant message."""
    rates = pricing.get(model)
    if rates is None:
        raise UnknownModel(model)
    per_input = rates["input"] / TOKENS_PER_UNIT
    per_output = rates["output"] / TOKENS_PER_UNIT
    return (
        input_tokens * per_input
        + output_tokens * per_output
        + cache_write_5m * per_input * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h * per_input * CACHE_WRITE_1H_MULTIPLIER
        + cache_read * per_input * CACHE_READ_MULTIPLIER
    )
