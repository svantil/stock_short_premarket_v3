"""Order-price precision shared by historical and live execution."""

from decimal import Decimal, ROUND_CEILING


def order_price(price: float) -> float:
    """Round an order limit upward using the live broker's price precision.

    Dollar prices use cents; prices below one dollar use four decimals. This
    applies to submitted limits, not fill averages or stop/target thresholds.
    """
    tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return float(Decimal(str(price)).quantize(tick, rounding=ROUND_CEILING))
