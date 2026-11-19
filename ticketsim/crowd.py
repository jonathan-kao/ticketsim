"""The crowd of buyers in one session - two interchangeable implementations.

``ObjectCrowd`` keeps one object per buyer: readable, and used as the
*reference* implementation.  ``SyntheticCrowd`` keeps exactly the same
information in parallel arrays (~20 bytes per buyer instead of ~280, and no
per-buyer objects for the garbage collector to walk).

Both must produce **identical** results - that is what
``tests/test_crowd.py`` checks (event by event, not just in aggregate).

The buyers are interchangeable for the ticket pool: only the order of their
requests matters.

Port boundary: this is the **Python reference**; the Rust world core has the
same thing in ``core/world/src/crowd.rs``.
The two must stay bit-identical (``tests/test_backends.py`` compares them
event by event), so a change to the mechanism goes into both.
"""
from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from typing import Iterator, List, Tuple

from .rng import (BUYERS, QTY, Channels, HashStream, hash_uniform, stream_uniform_bytes)


@dataclass
class BuyerData:
    """One buyer (the object flavour of the crowd)."""

    index: int
    tier: str
    tier_median: float
    tier_spread: float
    tier_floor: float
    first_delay: float
    desired_qty: int


def _draws(session: str, count: int, config, channels: Channels,
           max_qty: int = 4) -> Iterator[Tuple]:
    """Yield ``(tier, median, spread, floor, first_delay, desired)`` per buyer.

    The draws happen in arrival order, which is what makes the two crowd
    implementations (and every variant of a scenario) see the same buyers.
    """
    mc = config.model_constants
    rng_buyers = channels.rng(f"{BUYERS}:{session}")
    rng_qty = channels.rng(f"{QTY}:{session}")
    mix = tuple(mc.buyer_mix)
    # a single order cannot exceed the session's purchase limit, nor the longest
    # order the size distribution knows (four tickets by default)
    weights = tuple(mc.buyer_qty_weights[:max(1, min(max_qty, len(mc.buyer_qty_weights)))])
    weight_total = sum(weights) or 1.0
    jitter = mc.count_jitter
    total = max(0, int(round(count * (1.0 + rng_buyers.uniform(-jitter, jitter))))) if count else 0
    for index in range(1, total + 1):
        u = rng_buyers.random()
        acc = 0.0
        tier = mix[-1]
        for entry in mix:
            acc += entry[1]
            if u <= acc:
                tier = entry
                break
        _, _, median, spread, floor = tier
        delay = max(floor, median * math.exp(rng_buyers.gauss(0.0, 1.0) * spread))
        u = rng_qty.random() * weight_total
        acc = 0.0
        desired = len(weights)
        for i, weight in enumerate(weights):
            acc += weight
            if u <= acc:
                desired = i + 1
                break
        yield (tier[0], median, spread, floor, delay, desired)


class Crowd:
    """The buyers of one session, addressed by index (0 … len-1)."""

    def __init__(self, session: str, count: int, config, channels: Channels,
                 max_qty: int = 4) -> None:
        self.session = session
        self.config = config
        self.channels = channels
        self.seed = config.model_constants.seed
        self.max_qty = max_qty
        #: the constant part of the buyer's delay key (the same for every buyer)
        self._key_prefix = f"{self.seed}:b:{session}:"

    def __len__(self) -> int:
        raise NotImplementedError

    def desired(self, index: int) -> int:
        """How many tickets this buyer wants (before the down-adjust)."""
        raise NotImplementedError

    def first_delay(self, index: int) -> float:
        """The delay from the opening observation to this buyer's first order."""
        raise NotImplementedError

    def next_delay(self, index: int, retries: int) -> float:
        """The delay before the next attempt (a fresh draw, keyed by the buyer)."""
        raise NotImplementedError

    def hot(self, index: int) -> bool:
        """Is this buyer a "most popular seats first" buyer?

        The draw is keyed by the buyer, so it does not depend on the order in
        which buyers act.
        """
        return (hash_uniform(self.seed, f"b:{self.session}:{index + 1}:hot")
                < self.config.behavior.hot_buyer_frac)

    def _delay_params(self, index: int) -> Tuple[float, float, float]:
        raise NotImplementedError

    def _draw_delay(self, index: int, retries: int) -> float:
        median, spread, floor = self._delay_params(index)
        prefix = (f"{self._key_prefix}{index + 1}:submitdelay:{retries}".encode("utf-8"))
        u1 = max(stream_uniform_bytes(prefix, b":0"), 1e-12)
        u2 = stream_uniform_bytes(prefix, b":1")
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        raw = median * math.exp(z * spread)
        return raw if raw > floor else floor


class ObjectCrowd(Crowd):
    """One object per buyer - the readable reference implementation."""

    def __init__(self, session: str, count: int, config, channels: Channels,
                 max_qty: int = 4) -> None:
        super().__init__(session, count, config, channels, max_qty)
        self.buyers: List[BuyerData] = [
            BuyerData(index=i, tier=tier, tier_median=median, tier_spread=spread,
                      tier_floor=floor, first_delay=delay, desired_qty=desired)
            for i, (tier, median, spread, floor, delay, desired)
            in enumerate(_draws(session, count, config, channels, max_qty))
        ]

    def __len__(self) -> int:
        return len(self.buyers)

    def desired(self, index: int) -> int:
        return self.buyers[index].desired_qty

    def first_delay(self, index: int) -> float:
        return self.buyers[index].first_delay

    def next_delay(self, index: int, retries: int) -> float:
        return self._draw_delay(index, retries)

    def _delay_params(self, index: int) -> Tuple[float, float, float]:
        buyer = self.buyers[index]
        return buyer.tier_median, buyer.tier_spread, buyer.tier_floor


class SyntheticCrowd(Crowd):
    """The same information in parallel arrays - the scalable implementation."""

    def __init__(self, session: str, count: int, config, channels: Channels,
                 max_qty: int = 4) -> None:
        super().__init__(session, count, config, channels, max_qty)
        mix = tuple(config.model_constants.buyer_mix)
        self._tier_median = array("d", [m[2] for m in mix])
        self._tier_spread = array("d", [m[3] for m in mix])
        self._tier_floor = array("d", [m[4] for m in mix])
        self._desired = array("i")
        self._first = array("d")
        self._tier = bytearray()
        self._hot = bytearray()
        for tier, _median, _spread, _floor, delay, desired in _draws(session, count, config,
                                                                    channels, max_qty):
            self._tier.append(mix.index(next(m for m in mix if m[0] == tier)))
            self._desired.append(desired)
            self._first.append(delay)
            self._hot.append(1 if Crowd.hot(self, len(self._hot)) else 0)

    def __len__(self) -> int:
        return len(self._desired)

    def desired(self, index: int) -> int:
        return self._desired[index]

    def first_delay(self, index: int) -> float:
        return self._first[index]

    def next_delay(self, index: int, retries: int) -> float:
        return self._draw_delay(index, retries)

    def hot(self, index: int) -> bool:
        return bool(self._hot[index])

    def _delay_params(self, index: int) -> Tuple[float, float, float]:
        tier = self._tier[index]
        return self._tier_median[tier], self._tier_spread[tier], self._tier_floor[tier]


CROWDS = {"object": ObjectCrowd, "synthetic": SyntheticCrowd}


def make_crowd(kind: str, session: str, count: int, config, channels: Channels,
               max_qty: int = 4) -> Crowd:
    """Build the crowd implementation called ``kind``."""
    if kind not in CROWDS:
        raise KeyError(f"unknown crowd implementation {kind!r}; available: {sorted(CROWDS)}")
    return CROWDS[kind](session, count, config, channels, max_qty)
