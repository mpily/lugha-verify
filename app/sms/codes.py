from __future__ import annotations

import random

from .parser import CODE_ALPHABET


def new_code(taken: set[str], rng: random.Random) -> str:
    for _ in range(10_000):
        c = "".join(rng.choice(CODE_ALPHABET) for _ in range(4))
        if c not in taken:
            return c
    raise RuntimeError("no free task code")
