import pytest
from load_balancer.algorithms import (
    BackendNode,
    RoundRobinAlgorithm,
    WeightedRoundRobinAlgorithm,
    LeastConnectionsAlgorithm,
    IPHashAlgorithm,
    get_algorithm
)

def test_backend_node_lifecycle():
    node = BackendNode(node_id="Sys2", host="127.0.0.1", port=8001)
    assert node.is_healthy is True
    assert node.active_connections == 0
    assert node.total_requests == 0

    node.increment_connections()
    assert node.active_connections == 1
    assert node.total_requests == 1

    node.decrement_connections(success=True)
    assert node.active_connections == 0
    assert node.failed_requests == 0

    node.decrement_connections(success=False)
    assert node.failed_requests == 1

    node.mark_unhealthy()
    assert node.is_healthy is False

    node.mark_healthy(12.5)
    assert node.is_healthy is True
    assert node.last_latency_ms == 12.5

def test_round_robin_distribution():
    n1 = BackendNode("Sys2", "127.0.0.1", 8001)
    n2 = BackendNode("Sys3", "127.0.0.1", 8002)
    n3 = BackendNode("Sys4", "127.0.0.1", 8003)
    algo = RoundRobinAlgorithm([n1, n2, n3])

    assert algo.select_node().node_id == "Sys2"
    assert algo.select_node().node_id == "Sys3"
    assert algo.select_node().node_id == "Sys4"
    assert algo.select_node().node_id == "Sys2"
    assert algo.select_node().node_id == "Sys3"

def test_round_robin_skips_unhealthy_node():
    n1 = BackendNode("Sys2", "127.0.0.1", 8001)
    n2 = BackendNode("Sys3", "127.0.0.1", 8002)
    n3 = BackendNode("Sys4", "127.0.0.1", 8003)
    algo = RoundRobinAlgorithm([n1, n2, n3])

    n2.mark_unhealthy()

    selected = [algo.select_node().node_id for _ in range(4)]
    assert selected == ["Sys2", "Sys4", "Sys2", "Sys4"]

def test_least_connections_algorithm():
    n1 = BackendNode("Sys2", "127.0.0.1", 8001)
    n2 = BackendNode("Sys3", "127.0.0.1", 8002)
    algo = LeastConnectionsAlgorithm([n1, n2])

    n1.active_connections = 5
    n2.active_connections = 2

    assert algo.select_node().node_id == "Sys3"

    n2.active_connections = 6
    assert algo.select_node().node_id == "Sys2"

def test_ip_hash_consistency():
    n1 = BackendNode("Sys2", "127.0.0.1", 8001)
    n2 = BackendNode("Sys3", "127.0.0.1", 8002)
    algo = IPHashAlgorithm([n1, n2])

    ip1 = "192.168.1.50"
    ip2 = "10.0.0.100"

    target_ip1 = algo.select_node(ip1).node_id
    target_ip2 = algo.select_node(ip2).node_id

    for _ in range(10):
        assert algo.select_node(ip1).node_id == target_ip1
        assert algo.select_node(ip2).node_id == target_ip2
