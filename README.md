# Distributed Group Chat System with Custom Load Balancer

A production-grade, distributed real-time messaging platform featuring a high-performance **Reverse-Proxy Load Balancer**, **Scalable FastAPI WebSocket Backend Cluster**, end-to-end **AES-GCM Encryption & Ed25519 Digital Signatures**, **SQLite Persistence**, **Vite Frontend**, and a multi-threaded **Benchmarking Load Generator**.

---

## 📑 Table of Contents
- [System Architecture](#system-architecture)
- [Key Features](#key-features)
- [Distributed Cluster Deployment Topology](#distributed-cluster-deployment-topology)
- [Quick Start: Local Development](#quick-start-local-development)
- [Distributed Lab Deployment & Operations](#distributed-lab-deployment--operations)
- [Load Balancer Architecture & Algorithms](#load-balancer-architecture--algorithms)
- [Security & Cryptographic Verification](#security--cryptographic-verification)
- [Empirical Lab Performance & Benchmarking Results](#empirical-lab-performance--benchmarking-results)
- [WebSocket & REST API Protocols](#websocket--rest-api-protocols)
- [Testing & Quality Assurance](#testing--quality-assurance)
- [Project Directory Structure](#project-directory-structure)

---

## 🏛 System Architecture

```text
                               +-----------------------------+
                               |     Browser Clients / UI    |
                               |    (Vite Vanilla JS :5000)  |
                               +--------------+--------------+
                                              | HTTP & WebSocket (/ws)
                                              v
+-----------------------------+  HTTP REST   +-----------------------------+
|       Load Generator        | -----------> |     Sys1: Load Balancer     |
|   (Multi-threaded Bench)    |              |  - Port 8000 (Reverse Proxy)|
+-----------------------------+              |  - IP-Hash Sticky WS Routing|
                                             |  - Active Health Checks (3s)|
                                             |  - Automatic Failover       |
                                             +--------------+--------------+
                                                            |
                    +---------------------------------------+---------------------------------------+
                    |                                       |                                       |
                    v                                       v                                       v
     +-----------------------------+         +-----------------------------+         +-----------------------------+
     |   Sys2: Backend Server 1    |         |   Sys3: Backend Server 2    |         |   Sys4: Backend Server 3    |
     | - FastAPI + WebSocket       |         | - FastAPI + WebSocket       |         | - FastAPI + WebSocket       |
     | - Port 8000 (or 8001 local) |         | - Port 8000 (or 8002 local) |         | - Port 8000 (or 8003 local) |
     | - AES-GCM & Ed25519 Crypto  |         | - AES-GCM & Ed25519 Crypto  |         | - AES-GCM & Ed25519 Crypto  |
     | - SQLite DB Persistence     |         | - SQLite DB Persistence     |         | - SQLite DB Persistence     |
     +-----------------------------+         +-----------------------------+         +-----------------------------+
```

---

## 🌟 Key Features

### 1. Real-Time Chat & Rooms
- **Global Channel:** Default instant public room on connection.
- **Private Rooms & Codes:** Create private rooms with shareable 6-character access codes (e.g. `X8K9P2`).
- **Real-Time Presence:** Live user list and participant counts per room.
- **Responsive UI:** Dark theme layout with mobile drawer navigation and touch optimization.

### 2. Custom Distributed Load Balancer (`load_balancer/`)
- **Multi-Algorithm Support:**
  - `ip_hash`: Consistent hash routing per client IP (preserves WebSocket session state).
  - `round_robin`: Uniform cyclical distribution.
  - `weighted_round_robin`: Capacity-weighted request distribution.
  - `least_connections`: Routes to the backend with fewest active requests.
- **Active & Passive Health Checking:** Background heartbeat daemon probes `/health` every 3 seconds; passive failover retries the next healthy node upon connection drops.
- **Bi-Directional WebSocket Tunneling:** Transparent TCP upgrade proxying with HTTP 101 handshake handling and non-blocking duplex byte relay.
- **Cluster Observability:** Real-time `/lb/status` telemetry endpoint with per-backend latencies, request counters, and health states.

### 3. End-to-End Cryptographic Security (`server/crypto_utils.py`)
- **Confidentiality:** AES-GCM (256-bit) authenticated encryption with random nonces.
- **Integrity & Authenticity:** Ed25519 asymmetric keypairs generate digital signatures verifying message origin.
- **Tamper Detection:** Stored messages are decrypted and signature-verified upon history retrieval; tampered records trigger visual `[TAMPERED]` warning badges.

### 4. High-Concurrency Benchmarking Engine (`load_generator/`)
- Thread-pool worker client measuring Requests Per Second (RPS), error rates, and full latency percentiles (Min, Avg, P50, P90, P95, P99, Max).

---

## 🌐 Distributed Cluster Deployment Topology

### 4-Machine Lab Cluster (`10.1.75.79`)

| System | Role in Cluster | SSH Access | Internal Lab IP | Service Port |
| :--- | :--- | :--- | :--- | :--- |
| **Sys1** | **Load Balancer** | `ssh -p 2237 student@10.1.75.79` | `172.17.0.38` | `8000` |
| **Sys2** | **Backend Server 1** | `ssh -p 2238 student@10.1.75.79` | `172.17.0.39` | `8000` |
| **Sys3** | **Backend Server 2** | `ssh -p 2239 student@10.1.75.79` | `172.17.0.40` | `8000` |
| **Sys4** | **Backend Server 3** | `ssh -p 2240 student@10.1.75.79` | `172.17.0.41` | `8000` |

---

## 🚀 Quick Start: Local Development

### 1. Prerequisites
- **Node.js** (v18+)
- **Python** (3.10+)

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/maharshisoni24/web_chat.git
cd web_chat

# Install Node dependencies
npm install

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Launch All Services (Local 3-Backend + Load Balancer + UI)

#### Option A: Unified Terminal (`npm run dev`)
```bash
npm run dev
```
Starts:
- Backend 1: `http://localhost:8001`
- Backend 2: `http://localhost:8002`
- Backend 3: `http://localhost:8003`
- Load Balancer: `http://localhost:8000`
- Frontend UI: `http://localhost:5000`

#### Option B: Windows Multi-Window Launcher
Double-click `start.bat` (opens dedicated color-coded windows for each service).

---

## 📡 Distributed Lab Deployment & Operations

The repository includes an automated SSH deployment orchestrator using `paramiko`:

### 1. One-Click Remote Deployment
```bash
python deploy.py
```
This automatically:
1. Connects to `Sys2`, `Sys3`, and `Sys4` via SSH.
2. Uploads backend code, sets up environments, and starts FastAPI services on `0.0.0.0:8000`.
3. Connects to `Sys1` via SSH.
4. Uploads load balancer modules and starts the reverse proxy on `0.0.0.0:8000` targeting the internal cluster IPs (`172.17.0.39-41`).
5. Performs verification health checks across all 4 nodes.

### 2. Checking Cluster Status
```bash
python deploy.py --status
```

### 3. Live Cluster Log Streaming
```bash
npm run logs
# or: python logs.py
```
Streams color-coded live logs from all 4 remote machines in a single terminal window:
- `[Sys1 (LB) ]` — Request routing, latencies, and WebSocket tunnels.
- `[Sys2 (BE1)]` / `[Sys3 (BE2)]` / `[Sys4 (BE3)]` — FastAPI backend event logs.

### 4. Running the Local Client against the Remote Cluster
1. **Open the SSH Tunnel** (keep this terminal open):
   ```cmd
   tunnel.bat
   ```
2. **Start the Frontend UI**:
   ```bash
   npm run frontend
   ```
3. **Open [http://localhost:5000](http://localhost:5000)** in your browser.

---

## 📊 Empirical Lab Performance & Benchmarking Results

Benchmark experiments were conducted on the distributed lab cluster comparing a single backend instance (Sys2) against a 3-backend cluster (Sys2, Sys3, Sys4) routed through the Sys1 Load Balancer under identical load conditions (**300 requests, 15 concurrent threads**).

### Performance Comparison

| Performance Metric | Single Backend (Sys2) | 3-Backend Cluster (Sys2, Sys3, Sys4) | Improvement |
| :--- | :--- | :--- | :--- |
| **Active Nodes** | 1 (`172.17.0.39`) | 3 (`172.17.0.39-41`) | **+2 Backends** |
| **Workload** | 300 Requests / 15 Threads | 300 Requests / 15 Threads | Identical |
| **Success Rate** | 300 / 0 (100%) | 300 / 0 (100%) | 0 errors |
| **Total Test Duration** | **4.779 s** | **2.628 s** | **45.0% Faster** |
| **Throughput (RPS)** | **62.78 req/s** | **114.16 req/s** | **1.82x Speedup** |
| **Average Latency** | **228.87 ms** | **102.02 ms** | **55.4% Reduction** |
| **Median Latency (P50)**| **58.77 ms** | **41.82 ms** | **28.9% Reduction** |
| **90th Percentile (P90)**| **681.88 ms** | **194.28 ms** | **71.5% Reduction** |
| **95th Percentile (P95)**| **1067.69 ms** | **337.22 ms** | **68.4% Reduction** |
| **99th Percentile (P99)**| **2292.44 ms** | **1087.76 ms** | **52.5% Reduction** |
| **Max Latency** | **3121.91 ms** | **1150.64 ms** | **63.1% Reduction** |

### Traffic Distribution (Round Robin Verification)
```text
Sys2 (172.17.0.39:8000): 100 requests (33.33%)  ||||||||||
Sys3 (172.17.0.40:8000): 100 requests (33.33%)  ||||||||||
Sys4 (172.17.0.41:8000): 100 requests (33.33%)  ||||||||||
```

### Running Benchmark Experiments
```bash
# Run automated benchmark suite
python experiments/run_all.py

# Or run individual experiments:
python load_generator/generator.py --url http://127.0.0.1:8000 --requests 300 --concurrency 15
```

---

## 🔌 WebSocket & REST API Protocols

### WebSocket API (`/ws`)

| Message Type | Direction | Payload Structure | Description |
| :--- | :--- | :--- | :--- |
| `join` | Client → Server | `{"type": "join", "username": "Alice"}` | Register user and join `global` room |
| `create_room` | Client → Server | `{"type": "create_room", "name": "Team"}` | Create new private room with 6-char code |
| `join_room` | Client → Server | `{"type": "join_room", "code": "X8K9P2"}` | Join private room by code |
| `switch_room` | Client → Server | `{"type": "switch_room", "roomId": "..."}` | Switch active channel |
| `message` | Client → Server | `{"type": "message", "text": "Hello!"}` | Encrypt, sign, persist, and broadcast message |
| `room_entered` | Server → Client | `{"type": "room_entered", "room": {...}, "history": [...], "users": [...]}` | Initial room state & verified history |

### REST & Health Endpoints

| Endpoint | Method | Response / Purpose |
| :--- | :--- | :--- |
| `/health` | `GET` | `{"status": "ok", "total_clients": 0}` — Probed by LB health checker |
| `/lb/status` | `GET` | Cluster metrics, algorithm, active connections, and backend latencies |

---

## 🧪 Testing & Quality Assurance

The project includes an automated `pytest` test suite covering cryptographic integrity, database persistence, and load balancing algorithms:

```bash
python -m pytest tests/
```

**Test Coverage (10/10 Passed):**
- `test_database_initialization`: SQLite schema creation and default General room validation.
- `test_crypto_encryption_and_decryption`: AES-GCM ciphertext randomness and lossless decryption.
- `test_crypto_signing_and_verification`: Ed25519 digital signature creation and public key verification.
- `test_backend_node_lifecycle`: Connection counting and state tracking.
- `test_round_robin_algorithm`: Deterministic rotation.
- `test_least_connections_algorithm`: Dynamic routing to least loaded node.
- `test_ip_hash_algorithm`: Consistent routing determinism by client IP.
- `test_failover_recovery`: Node unhealthiness marking and automatic traffic bypass.
- `test_metrics_calculation`: Percentile and throughput aggregation math.

---

## 📁 Project Directory Structure

```text
Local-Web-Chat/
├── index.html                  # Main Web Chat User Interface
├── main.js                     # UI state management, modals, and event handling
├── websocket.js                # WebSocket client communication layer
├── style.css                   # Responsive dark-theme styling
├── vite.config.js              # Vite server & reverse-proxy configuration
├── package.json                # NPM scripts and dependencies
├── requirements.txt            # Unified Python dependencies
├── deploy.py                   # Automated SSH cluster deployer (Sys1-Sys4)
├── tunnel.bat                  # Local SSH port forwarder (localhost:8000 -> Sys1)
├── logs.py                     # Multi-node live log streamer
├── start.bat                   # Local 3-backend + LB + UI launcher
│
├── server/                     # FastAPI Messaging Backend
│   ├── main.py                 # WebSocket endpoints & RoomConnectionManager
│   ├── database.py             # SQLite persistence & encrypted query layer
│   ├── crypto_utils.py         # AES-GCM encryption & Ed25519 signatures
│   ├── secret.key              # Shared encryption key
│   └── requirements.txt        # Backend dependencies
│
├── load_balancer/              # Custom Load Balancer (Sys1)
│   ├── balancer.py             # Reverse-proxy server & WS tunnel handler
│   ├── algorithms.py           # Round Robin, IP-Hash, Least Conn algorithms
│   └── health_checker.py       # Background health probe daemon (3s interval)
│
├── load_generator/             # High-Concurrency Benchmarking Client
│   ├── generator.py            # Multi-threaded HTTP benchmark runner
│   └── metrics.py              # Latency percentile & throughput calculator
│
├── config/                     # Cluster & single-machine configurations
│   ├── distributed_lab.json    # Lab network IP map & ports
│   └── single_machine.json     # Local port mappings
│
├── experiments/                # Automated Benchmarking Experiments
│   ├── run_all.py              # Full experiment suite runner
│   ├── run_experiment1_single.py # Single backend benchmark
│   ├── run_experiment2_three.py  # 3-backend cluster benchmark
│   └── compare_experiments.py  # Comparison & metrics report generator
│
└── tests/                      # Automated Unit Test Suite
    ├── test_backend.py         # DB & Cryptography tests
    ├── test_load_balancer.py   # Load balancing algorithms & failover tests
    └── test_load_generator.py  # Metric calculation tests
```

---

## 👥 Authors & Acknowledgments

- **Distributed Systems Lab Project**
- Developed by **Abhigyan Sharma** & Team
- Deployed on **IIT Bhilai Distributed Lab Cluster** (`10.1.75.79`)
