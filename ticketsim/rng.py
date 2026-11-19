"""Randomness: named channels and keyed per-entity streams.

Each concern draws from its own stream, so changing one behaviour cannot shift
the random numbers of another.  That is what makes *common random numbers*
work: two variants of a scenario see the same buyers, the same quantities and
the same decisions, so their difference is the effect of the change alone.

Port boundary: this is the **Python reference**; the Rust world core has the
same thing in ``core/world/src/rng.rs`` (keyed BLAKE2b, ``HashStream``, CPython's Mersenne
Twister and ``gauss``).
The two must stay bit-identical (``tests/test_backends.py`` compares them
event by event), so a change to the mechanism goes into both.
"""
from __future__ import annotations

import hashlib
import math
import random
from typing import Dict

#: Channels used by the simulator.
#:
#: Only two: the crowd's arrivals, speed tiers and quantities.  **Everything
#: else** (decision times, refund and payment draws, cell choices, retry delays)
#: is a keyed hash of the entity it belongs to (``HashStream``, ``hash_uniform``),
#: so it does not depend on the order of the draws.
BUYERS = "buyers"      # speed tier and first delay of every buyer
QTY = "qty"            # tickets every buyer wants


def channel_seed(seed: int, channel: str) -> int:
    """Derive a stable 64-bit seed for ``channel`` from the master ``seed``."""
    digest = hashlib.blake2b(f"{seed}:{channel}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class HashStream:
    """Per-entity randomness: the same key always yields the same sequence.

    Used wherever randomness must be *independent of the order in which it is
    drawn*: a buyer's decision about an order must not change because some
    other buyer acted earlier in one variant than in another (common random
    numbers).  Keying by entity rather than by draw order achieves that.
    """

    __slots__ = ("_prefix", "_counter")

    def __init__(self, seed: int, key: str) -> None:
        self._prefix = f"{seed}:{key}"
        self._counter = 0

    def uniform(self) -> float:
        """A uniform variate in [0, 1)."""
        digest = hashlib.blake2b(f"{self._prefix}:{self._counter}".encode("utf-8"),
                                 digest_size=8).digest()
        self._counter += 1
        return int.from_bytes(digest, "big") / 2.0 ** 64

    def normal(self) -> float:
        """A standard normal variate (Box–Muller)."""
        u1 = max(self.uniform(), 1e-12)
        u2 = self.uniform()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def hash_uniform(seed: int, key: str) -> float:
    """One uniform variate derived from ``seed`` and ``key`` (order independent)."""
    return hash_uniform_bytes(f"{seed}:{key}".encode("utf-8"))


def hash_uniform_bytes(prefix: bytes) -> float:
    """``hash_uniform`` for a key that is already encoded.

    Same digest, but the hot paths do not pay for the string formatting.
    """
    digest = hashlib.blake2b(prefix, digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0 ** 64


def stream_uniform(prefix: bytes, counter: int) -> float:
    """The ``counter``-th variate of the stream ``prefix``.

    Identical to ``HashStream(prefix).uniform()`` called ``counter`` times,
    without the object and without formatting the key again.
    """
    return stream_uniform_bytes(prefix, b":" + str(counter).encode("ascii"))


def stream_uniform_bytes(prefix: bytes, suffix: bytes) -> float:
    """``stream_uniform`` with the counter already encoded."""
    digest = hashlib.blake2b(prefix + suffix, digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2.0 ** 64


class Channels:
    """Lazily created, independent :class:`random.Random` streams."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._rngs: Dict[str, random.Random] = {}

    def rng(self, channel: str) -> random.Random:
        rng = self._rngs.get(channel)
        if rng is None:
            rng = random.Random(channel_seed(self.seed, channel))
            self._rngs[channel] = rng
        return rng
