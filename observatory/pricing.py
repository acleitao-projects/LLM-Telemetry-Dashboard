"""Decimal-safe pricing validation and cost calculation (G03).

All pricing values are stored as canonical fixed-point decimal strings in TEXT
columns. No binary float is ever used in the pricing path.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional


class PricingValidationError(Exception):
    """Raised when a pricing value is invalid (not blank/null)."""

    def __init__(self, model_id: int, field: str, raw_value: str):
        self.model_id = model_id
        self.field = field
        self.raw_value = raw_value
        super().__init__(f"invalid {field} for model {model_id}: {raw_value!r}")


def validate_price(value, model_id: int, field: str) -> Optional[str]:
    """Return canonical fixed-point string for a valid price, None for blank/null.

    Raises PricingValidationError for invalid input (non-numeric, NaN, Infinity,
    negative). Blank/null is a valid value and normalizes to None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise PricingValidationError(model_id, field, s)
    if d.is_nan() or d.is_infinite():
        raise PricingValidationError(model_id, field, s)
    if d < 0:
        raise PricingValidationError(model_id, field, s)
    return format(d, "f")


def compute_costs(model, prompt_tokens: float, gen_tokens: float) -> dict:
    """Compute decimal-string costs from stored rates and token counts.

    Uses full-precision Decimal arithmetic. API output is formatted to a
    consistent 8-decimal-place policy; no intermediate rounding occurs.
    """
    in_rate = (Decimal(model.input_price_per_million)
               if model.input_price_per_million else Decimal("0"))
    out_rate = (Decimal(model.output_price_per_million)
                if model.output_price_per_million else Decimal("0"))
    input_cost = (Decimal(str(prompt_tokens)) / Decimal("1000000")) * in_rate
    output_cost = (Decimal(str(gen_tokens)) / Decimal("1000000")) * out_rate
    total_cost = input_cost + output_cost
    return {
        "input_price_per_million": model.input_price_per_million or "0.00000000",
        "output_price_per_million": model.output_price_per_million or "0.00000000",
        "input_cost": format(input_cost, ".8f"),
        "output_cost": format(output_cost, ".8f"),
        "total_cost": format(total_cost, ".8f"),
    }
