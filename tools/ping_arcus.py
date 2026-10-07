#!/usr/bin/env python3
"""Measure your round trip to Arcus. Put the p50 into paper.rtt_ms in config.local.yaml."""

import asyncio
import statistics
import time

import aiohttp

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
    print(f"-> set  paper.rtt_ms: {round(p50)}  in config.local.yaml")


if __name__ == "__main__":
    asyncio.run(main())
