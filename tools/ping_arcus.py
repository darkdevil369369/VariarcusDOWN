#!/usr/bin/env python3
"""Measure your round trip to Arcus. Put the p50 into paper.rtt_ms in config.local.yaml."""

import asyncio
import statistics
import sys
import time
from pathlib import Path

import aiohttp
import yaml

LOCAL = Path(__file__).resolve().parent.parent / "config.local.yaml"

URL = "https://api.arcus.xyz/v1/time"


async def main(n: int = 30) -> None:
    samples = []
    async with aiohttp.ClientSession() as s:
        async with s.get(URL) as r:          # warm the TLS connection
            await r.read()
        for _ in range(n):
            t0 = time.perf_counter()
            async with s.get(URL) as r:
                await r.read()
            samples.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.2)
    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[int(len(samples) * 0.95) - 1]
    print(f"Arcus RTT over {n} requests: p50 {p50:.0f} ms, p95 {p95:.0f} ms, min {samples[0]:.0f} ms")
    if "--write" in sys.argv:
        data = yaml.safe_load(LOCAL.read_text()) if LOCAL.exists() else {}
        data = data or {}
        data.setdefault("paper", {})["rtt_ms"] = round(p50)
        LOCAL.write_text(yaml.safe_dump(data, sort_keys=False))
        print(f"-> wrote paper.rtt_ms: {round(p50)} to {LOCAL.name}")
    else:
        print(f"-> set  paper.rtt_ms: {round(p50)}  in config.local.yaml  (or rerun with --write)")


if __name__ == "__main__":
    asyncio.run(main())
