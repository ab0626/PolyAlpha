"""Conservative maker research: queue-ahead plus observed aggressive trade volume."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class MakerQueue:
    side: str
    price: Decimal
    remaining: Decimal
    queue_ahead: Decimal

    def __post_init__(self):
        if (
            self.side not in ("BUY", "SELL")
            or not 0 < self.price < 1
            or self.remaining <= 0
            or self.queue_ahead < 0
        ):
            raise ValueError("invalid maker queue")

    def on_trade(self, aggressor, price, quantity):
        if quantity < 0:
            raise ValueError("negative volume")
        expected = "SELL" if self.side == "BUY" else "BUY"
        # Touching a quote never fills. Only matching public aggressive volume does.
        if aggressor != expected or price != self.price:
            return Decimal(0)
        queue_consumed = min(quantity, self.queue_ahead)
        self.queue_ahead -= queue_consumed
        fill = min(self.remaining, quantity - queue_consumed)
        self.remaining -= fill
        return fill
