import pytest
from load_generator.metrics import MetricsCollector, RequestResult

def test_metrics_calculation():
    collector = MetricsCollector("TestExperiment")
    collector.start()

    collector.record(RequestResult(success=True, status_code=200, latency_ms=10.0, backend_id="Sys2"))
    collector.record(RequestResult(success=True, status_code=200, latency_ms=20.0, backend_id="Sys3"))
    collector.record(RequestResult(success=True, status_code=200, latency_ms=30.0, backend_id="Sys4"))
    collector.record(RequestResult(success=True, status_code=200, latency_ms=40.0, backend_id="Sys2"))
    collector.record(RequestResult(success=False, status_code=500, latency_ms=50.0, backend_id="Sys3", error_message="Server error"))
    collector.finish()

    summary = collector.calculate_summary()
    assert summary["total_requests"] == 5
    assert summary["successful_requests"] == 4
    assert summary["failed_requests"] == 1
    assert summary["error_rate_pct"] == 20.0

    lat = summary["latency_ms"]
    assert lat["min"] == 10.0
    assert lat["max"] == 50.0
    assert lat["avg"] == 30.0
    assert lat["p50"] == 30.0

    dist = summary["backend_distribution"]
    assert dist["Sys2"] == 2
    assert dist["Sys3"] == 2
    assert dist["Sys4"] == 1
