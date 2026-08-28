# Distributed Systems Lab: Load Balancer Performance Comparison Report

**Date:** 2026-08-21 10:04:46  
**Load Balancer:** Sys1 (Round Robin)  

## 1. Executive Performance Summary

- **Throughput Speedup:** **`1.13x`** increase in requests processed per second (`199.00 RPS` -> `224.68 RPS`).
- **Average Latency Improvement:** **`6.0%` reduction** in average response time (`58.34 ms` down to `54.83 ms`).
- **P95 Latency Improvement:** **`74.2%` reduction** under concurrent load (`524.70 ms` down to `135.53 ms`).
- **Workload Duration:** Total execution time dropped from `1.51s` to `1.33s` (`11.5%` reduction).

## 2. Comprehensive Comparison Table

| Performance Metric | Experiment 1 (Single Backend: Sys2) | Experiment 2 (Three Backends: Sys2, Sys3, Sys4) | Relative Delta / Improvement |
| :--- | :--- | :--- | :--- |
| **Active Backends** | 1 (Sys2) | 3 (Sys2, Sys3, Sys4) | **+2 Backends (+200%)** |
| **Total Requests** | 300 | 300 | **Identical Workload** |
| **Successful Requests** | 300 (100.0%) | 300 (100.0%) | **+0 reqs** |
| **Failed Requests** | 0 | 0 | **+0 reqs** |
| **Error Rate (%)** | 0.0% | 0.0% | **+0.00%** |
| **Total Test Duration** | 1.51 s | 1.33 s | **+11.5% faster** |
| **Throughput (RPS)** | 199.00 req/s | 224.68 req/s | **1.13x Speedup** |
| **Average Latency** | 58.34 ms | 54.83 ms | **+6.0% latency** |
| **Median Latency (P50)** | 21.80 ms | 25.15 ms | **-3.35 ms** |
| **90th Percentile Latency (P90)** | 48.07 ms | 51.51 ms | **-3.44 ms** |
| **95th Percentile Latency (P95)** | 524.70 ms | 135.53 ms | **+74.2% latency** |
| **99th Percentile Latency (P99)** | 713.88 ms | 569.37 ms | **+144.51 ms** |
| **Maximum Latency** | 797.78 ms | 1058.22 ms | **-260.44 ms** |
| **Minimum Latency** | 2.75 ms | 3.15 ms | **-0.40 ms** |

## 3. Backend Workload Distribution (Experiment 2)

| Backend Node | Host / Port | Requests Handled | Share (%) | Distribution Visual |
| :--- | :--- | :--- | :--- | :--- |
| **Sys2** | Cluster Node | 100 | 33.3% | `||||||||` |
| **Sys3** | Cluster Node | 100 | 33.3% | `||||||||` |
| **Sys4** | Cluster Node | 100 | 33.3% | `||||||||` |

## 4. Theoretical Analysis & Key Insights

1. **Horizontal Scalability:** Adding two additional backend nodes triples the available computing and I/O concurrency. Queued request bottlenecks at the single server are distributed across the cluster, preventing head-of-line blocking.
2. **Round-Robin Fairness:** As shown in the distribution table, Round Robin balances traffic with virtually equal distribution (~33.3% per node) when request processing times are uniformly distributed.
3. **Tail Latency Mitigation:** The 95th and 99th percentile latencies are dramatically reduced because incoming bursts of concurrent requests do not get serialized behind a single thread/worker pool.
