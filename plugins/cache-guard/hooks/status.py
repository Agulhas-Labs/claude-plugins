#!/usr/bin/env python3
"""The band's reading of the prompt cache, as JSON on stdout, from the guard's own functions.

stdin: {"transcript_path": str, "session_id": str, "agents": [usage, ...]}: the transcript is found
from the session id when its path is not given (cache_guard.transcript_of), and each usage is a
subagent turn's token counts with its `model`. stdout: when the last main-session turn was answered,
the cache lifetime it bought, the context size, what resending that context costs cold and warm, and
the subagents' usage priced at list rates. Nothing here is computed twice: the band only counts down
from these figures between turns.
"""
import json
import os
import sys

import cache_guard


def usage_cost(usage, env):
    """A turn's usage priced at list rates, or None when its model has no price.

    A cache write is priced at the five-minute lifetime, what a subagent buys; output at
    OUTPUT_MULTIPLIER times input.
    """
    write, read = cache_guard.prices(usage.get("model") or "", cache_guard.FIVE_MINUTES, env)
    if write is None:
        return None
    price_in = write / cache_guard.WRITE_MULTIPLIER_5M
    return (
        (usage.get("input_tokens") or 0) * price_in
        + (usage.get("output_tokens") or 0) * price_in * cache_guard.OUTPUT_MULTIPLIER
        + (usage.get("cache_read_input_tokens") or 0) * read
        + (usage.get("cache_creation_input_tokens") or 0) * write
    ) / 1_000_000


def status(request, env):
    out = {"disabled": str(env.get("CACHE_GUARD_DISABLE") or "").strip() == "1",
           "show_cost": str(env.get("CACHE_GUARD_SHOW_COST") or "").strip() != "0",
           "last_turn_at": None, "lifetime_s": None, "context_tokens": None,
           "cold_usd": None, "warm_usd": None, "agents_usd": None, "agents_unpriced": 0}
    agents = request.get("agents") or []
    priced = [usage_cost(u, env) for u in agents]
    if agents:
        out["agents_usd"] = sum(c for c in priced if c is not None)
        out["agents_unpriced"] = sum(1 for c in priced if c is None)
    path = cache_guard.transcript_of(request, env)
    if not path or not os.path.isfile(path):
        return out
    found = cache_guard.turns(cache_guard.read_tail(path))
    if not found:
        return out
    last = found[-1]
    lifetime = cache_guard.positive_int(env, "CACHE_GUARD_LIFETIME_SECONDS", 0) or cache_guard.lifetime_of(found)
    write, read = cache_guard.prices(last.model, lifetime, env)
    out.update({"last_turn_at": int(last.time.timestamp() * 1000), "lifetime_s": lifetime,
                "context_tokens": last.size})
    if write is not None:
        out["cold_usd"] = last.size * write / 1_000_000
        out["warm_usd"] = last.size * read / 1_000_000
    return out


def main():
    try:
        request = json.load(sys.stdin)
    except ValueError:
        request = {}
    print(json.dumps(status(request if isinstance(request, dict) else {}, os.environ)))


if __name__ == "__main__":
    main()
