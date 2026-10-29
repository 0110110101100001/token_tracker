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
    """Return the USD cost of one assistant message.

    Cache reads are a tenth of the input rate unless the model's entry names
    its own `cache_read` rate per million tokens. The tenth is what every model
    charged until Fable 5.1, which publishes cache reads at $0.25 per million
    against a $10 input rate; one multiplier for all of them would overcharge
    that model's reads four times over, and reads are most of a long session.

    An entry may carry a `long_prompt` tier, `{"over": N, "input": ...,
    "output": ...}`, whose rates replace the base ones for the whole message
    once its prompt -- input, cache writes and cache reads together -- is more
    than N tokens. Haiku 5.5 is priced this way, five times higher above 100K,
    and a Claude Code session is past 100K for most of its turns.
    """
    rates = pricing.get(model)
    if rates is None:
        raise UnknownModel(model)
    tier = rates.get("long_prompt")
    prompt_tokens = input_tokens + cache_write_5m + cache_write_1h + cache_read
    if tier is not None and prompt_tokens > tier["over"]:
        rates = tier
    per_input = rates["input"] / TOKENS_PER_UNIT
    per_output = rates["output"] / TOKENS_PER_UNIT
    per_cache_read = (
        rates["cache_read"] / TOKENS_PER_UNIT if "cache_read" in rates
        else per_input * CACHE_READ_MULTIPLIER
    )
    return (
        input_tokens * per_input
        + output_tokens * per_output
        + cache_write_5m * per_input * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h * per_input * CACHE_WRITE_1H_MULTIPLIER
        + cache_read * per_cache_read
    )
