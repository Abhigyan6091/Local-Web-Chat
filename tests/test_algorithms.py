"""Unit tests for backend bookkeeping and the routing policies."""

import pytest

from load_balancer.algorithms import (
    AdaptiveThresholdAlgorithm, BackendNode, IPHashAlgorithm,
    LeastConnectionsAlgorithm, LeastLoadAlgorithm, RoundRobinAlgorithm,
    ScoreConfig, WeightedRoundRobinAlgorithm, get_algorithm,
)


def make_nodes(n=3):
    return [BackendNode(f"Sys{i + 2}", "127.0.0.1", 4000 + i) for i in range(n)]


def test_request_accounting():
    node = BackendNode("Sys2", "127.0.0.1", 4000)
    assert node.is_healthy and node.active_connections == 0

    node.begin_request()
    assert node.active_connections == 1
    assert node.total_requests == 1

    node.end_request(12.0, success=True)
    assert node.active_connections == 0
    assert node.failed_requests == 0
    assert node.ewma_rtt_ms == pytest.approx(12.0)

    node.begin_request()
    node.end_request(50.0, success=False)
    assert node.failed_requests == 1


def test_health_requires_consecutive_failures():
    """One blip must not evict a backend; two consecutive failures must."""
    node = BackendNode("Sys2", "127.0.0.1", 4000)
    assert node.report_health_fail(fail_threshold=2) is False
    assert node.is_healthy is True
    assert node.report_health_fail(fail_threshold=2) is True
    assert node.is_healthy is False

    assert node.report_health_ok({"cpu": 0.1}, 3.0, rise_threshold=2) is False
    assert node.report_health_ok({"cpu": 0.1}, 3.0, rise_threshold=2) is True
    assert node.is_healthy is True


def test_load_score_rises_with_cpu_and_connections():
    cfg = ScoreConfig()
    node = BackendNode("Sys2", "127.0.0.1", 4000)
    idle = node.load_score(cfg)

    node.cpu = 0.9
    busy_cpu = node.load_score(cfg)
    assert busy_cpu > idle

    for _ in range(10):
        node.begin_request()
    assert node.load_score(cfg) > busy_cpu


def test_adaptive_sticks_below_threshold():
    """Below the threshold the policy must not bounce between backends."""
    nodes = make_nodes()
    algo = AdaptiveThresholdAlgorithm(nodes, ScoreConfig(), threshold=0.65)
    first = algo.select_node()
    for _ in range(20):
        assert algo.select_node() is first
    assert algo.switch_count == 1  # only the initial selection


def test_adaptive_switches_when_threshold_exceeded():
    nodes = make_nodes()
    algo = AdaptiveThresholdAlgorithm(nodes, ScoreConfig(), threshold=0.30)
    first = algo.select_node()

    first.cpu = 1.0                      # push it well past the threshold
    for other in nodes:
        if other is not first:
            other.cpu = 0.01

    chosen = algo.select_node()
    assert chosen is not first
    assert algo.switch_count >= 2


def test_adaptive_never_returns_unhealthy_backend():
    nodes = make_nodes()
    algo = AdaptiveThresholdAlgorithm(nodes, ScoreConfig(), threshold=0.65)
    for node in nodes[:-1]:
        node.is_healthy = False
    for _ in range(10):
        assert algo.select_node() is nodes[-1]

    nodes[-1].is_healthy = False
    assert algo.select_node() is None


def test_adaptive_picks_least_loaded_when_all_saturated():
    nodes = make_nodes()
    algo = AdaptiveThresholdAlgorithm(nodes, ScoreConfig(), threshold=0.10)
    for i, node in enumerate(nodes):
        node.cpu = 0.9 - i * 0.1         # nodes[2] is the least loaded
    algo.select_node()
    assert algo.select_node() is nodes[2]
    assert algo.saturated_selections > 0


def test_least_load_tracks_the_cheapest_backend():
    nodes = make_nodes()
    algo = LeastLoadAlgorithm(nodes, ScoreConfig())
    nodes[0].cpu, nodes[1].cpu, nodes[2].cpu = 0.9, 0.2, 0.5
    assert algo.select_node() is nodes[1]


def test_round_robin_cycles_and_skips_unhealthy():
    nodes = make_nodes()
    algo = RoundRobinAlgorithm(nodes)
    assert [algo.select_node().node_id for _ in range(6)] == \
           ["Sys2", "Sys3", "Sys4", "Sys2", "Sys3", "Sys4"]

    nodes[1].is_healthy = False
    picked = {algo.select_node().node_id for _ in range(6)}
    assert "Sys3" not in picked


def test_least_connections_prefers_the_idle_node():
    nodes = make_nodes()
    algo = LeastConnectionsAlgorithm(nodes)
    nodes[0].active_connections = 5
    nodes[1].active_connections = 2
    nodes[2].active_connections = 9
    assert algo.select_node() is nodes[1]


def test_ip_hash_is_stable_per_client():
    nodes = make_nodes()
    algo = IPHashAlgorithm(nodes)
    first = algo.select_node(client_ip="10.1.2.3")
    for _ in range(10):
        assert algo.select_node(client_ip="10.1.2.3") is first


def test_weighted_round_robin_respects_weights():
    nodes = make_nodes(2)
    nodes[0].weight = 3
    algo = WeightedRoundRobinAlgorithm(nodes)
    picks = [algo.select_node().node_id for _ in range(8)]
    assert picks.count("Sys2") == 6
    assert picks.count("Sys3") == 2


def test_get_algorithm_resolves_names_and_defaults():
    nodes = make_nodes()
    assert isinstance(get_algorithm("round_robin", nodes), RoundRobinAlgorithm)
    assert isinstance(get_algorithm("ip-hash", nodes), IPHashAlgorithm)
    assert isinstance(get_algorithm("lc", nodes), LeastConnectionsAlgorithm)
    # Unknown names fall back to the policy this deployment submits.
    assert isinstance(get_algorithm("nonsense", nodes), AdaptiveThresholdAlgorithm)
