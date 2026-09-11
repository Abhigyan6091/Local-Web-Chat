import asyncio
import aiohttp
import time

URL = "http://10.1.75.79:4237/message"

async def send_one(session, idx):
    payload = {"client-name": f"test-{idx}", "msg": f"hello-{idx}"}
    t0 = time.perf_counter()
    try:
        async with session.post(URL, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            text = await resp.text()
            dt = (time.perf_counter() - t0) * 1000
            return resp.status, dt, text[:100]
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return type(e).__name__, dt, str(e)

async def test_concurrency(n=250):
    connector = aiohttp.TCPConnector(limit=n)
    async with aiohttp.ClientSession(connector=connector) as session:
        t_all_start = time.perf_counter()
        tasks = [send_one(session, i) for i in range(n)]
        results = await asyncio.gather(*tasks)
        total_time = time.perf_counter() - t_all_start
    
    statuses = {}
    for st, dt, sample in results:
        statuses[st] = statuses.get(st, 0) + 1
    print(f"Results for {n} concurrent requests (took {total_time:.2f}s):")
    for st, cnt in statuses.items():
        print(f"  Status {st}: {cnt}")
    for st, dt, sample in results[:5]:
        print(f"  Sample {st} ({dt:.0f}ms): {sample}")

if __name__ == "__main__":
    asyncio.run(test_concurrency(250))
