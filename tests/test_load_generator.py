"""Tests for the load generator's workload shaping and reporting."""

import pytest

from load_generator.generator import LoadRun, RunConfig, Sample, random_message


def cfg(**kw):
    base = dict(url="http://example", users=4, duration=1.0, ramp=0.0,
                min_len=10, max_len=40, min_interval=0.0, max_interval=0.1,
                feed_ratio=0.25, feed_limit=0, timeout=5.0,
                retry_duplicates=0.0, label="t")
    base.update(kw)
    return RunConfig(**base)


@pytest.mark.parametrize("lo,hi", [(1, 5), (10, 40), (200, 400)])
def test_message_length_stays_in_range(lo, hi):
    for _ in range(200):
        assert lo <= len(random_message(lo, hi)) <= hi


def test_message_length_actually_varies():
    lengths = {len(random_message(16, 256)) for _ in range(200)}
    assert len(lengths) > 20, "message length should be variable, not fixed"


def test_summary_percentiles_and_throughput():
    run = LoadRun(cfg())
    for i in range(100):
        run.samples.append(Sample(t=i * 0.01, endpoint="message",
                                  latency_ms=float(i + 1), status=200, ok=True,
                                  backend="Sys3"))
    s = run.summary()
    assert s["total_requests"] == 100
    assert s["overall"]["successful"] == 100
    assert s["overall"]["p50_ms"] == pytest.approx(50.5, abs=1.5)
    assert s["overall"]["p99_ms"] >= s["overall"]["p95_ms"] >= s["overall"]["p50_ms"]
    assert s["requests_per_backend"] == {"Sys3": 100}


def test_summary_counts_failures_and_duplicates():
    run = LoadRun(cfg())
    run.samples.append(Sample(0.1, "message", 10.0, 200, True, "Sys2", duplicate=False))
    run.samples.append(Sample(0.2, "message", 10.0, 200, True, "Sys2", duplicate=True))
    run.samples.append(Sample(0.3, "message", 0.0, 0, False, None))
    run.samples.append(Sample(0.4, "feed", 20.0, 200, True, "Sys3"))

    s = run.summary()
    assert s["overall"]["failed"] == 1
    assert s["overall"]["error_rate_pct"] == pytest.approx(25.0)
    assert s["duplicates_reported"] == 1
    assert s["message"]["requests"] == 3
    assert s["feed"]["requests"] == 1


def test_summary_handles_an_empty_run():
    s = LoadRun(cfg()).summary()
    assert s["total_requests"] == 0
    assert s["overall"]["error_rate_pct"] == 0.0
    assert s["overall"]["p95_ms"] == 0.0
