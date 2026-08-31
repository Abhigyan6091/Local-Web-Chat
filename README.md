# Distributed Group Chat System with Custom Load Balancer

A real-time web-based group chat application built with **FastAPI, WebSockets, Vite, SQLite, AES-GCM encryption, Ed25519 digital signatures, and a custom Python load balancer**.

The project supports deployment across multiple backend servers, with a dedicated frontend/load-balancer server distributing HTTP and WebSocket traffic among backend instances.

---

## Features

### Chat Application

- Real-time group chat using WebSockets
- Global chat room
- Private rooms
- Six-character private room codes
- Room switching
- Online-user presence
- Persistent message history

### Security

- AES-GCM authenticated encryption for stored messages
- Ed25519 digital signatures
- Message integrity verification
- Tamper detection

### Load Balancer

- Round-robin routing
- IP-hash routing
- Weighted round-robin
- Least-connections routing
- Automatic backend health checking
- Unhealthy backend detection
- Backend recovery detection
- HTTP reverse proxying
- WebSocket tunnelling
- Backend statistics
- Latency information

### Load Generator

The project includes a load generator for measuring:

- Successful requests
- Failed requests
- Throughput (RPS)
- Dropout percentage
- P50 latency
- P95 latency
- P99 latency

---

# System Architecture

The application can be deployed using one frontend/load-balancer server and multiple backend servers.

```text
                         Browser
                            |
                            | HTTP / WebSocket
                            v
                  +----------------------+
                  | Frontend + Load      |
                  | Balancer Server      |
                  +----------+-----------+
                             |
              +--------------+--------------+
              |              |              |
              v              v              v
        +-----------+  +-----------+  +-----------+
        | Backend 1 |  | Backend 2 |  | Backend 3 |
        | FastAPI   |  | FastAPI   |  | FastAPI   |
        +-----------+  +-----------+  +-----------+
```

The load balancer distributes requests among healthy backend servers.

---

# Requirements

## Frontend / Load Balancer Server

- Node.js
- npm
- Python 3
- Vite
- Python dependencies from `requirements.txt`

## Backend Servers

- Python 3
- FastAPI
- Uvicorn
- Dependencies from `requirements.txt`

---

# Installation

Clone the repository:

```bash
git clone https://github.com/maharshisoni24/web_chat.git
cd web_chat
```

Install Node.js dependencies:

```bash
npm install
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Install the Python dependencies on each backend server as well.

---

# Deployment

The project can be configured for any network topology.

For example:

```text
Frontend / Load Balancer
        |
        +----> Backend 1
        |
        +----> Backend 2
        |
        +----> Backend 3
```

Replace the backend addresses in the load-balancer configuration with the addresses and ports of your own servers.

Example:

```text
http://<BACKEND_1_HOST>:<BACKEND_1_PORT>
http://<BACKEND_2_HOST>:<BACKEND_2_PORT>
http://<BACKEND_3_HOST>:<BACKEND_3_PORT>
```

---

# Running the Backend

On each backend server, run:

```bash
cd ~/web_chat
python3 -m uvicorn server.main:app --host 0.0.0.0 --port <PORT>
```

Replace `<PORT>` with the port assigned to that backend.

Test the backend:

```bash
curl http://<BACKEND_HOST>:<PORT>/health
```

Expected response:

```json
{
  "status": "ok",
  "total_clients": 0
}
```

---

# Running the Load Balancer

The load balancer is implemented in:

```text
load_balancer/balancer.py
```

Start it using:

```bash
python3 load_balancer/balancer.py \
  --host 0.0.0.0 \
  --port <LB_PORT> \
  --backends http://<BACKEND_1_HOST>:<PORT>,http://<BACKEND_2_HOST>:<PORT>,http://<BACKEND_3_HOST>:<PORT> \
  --algorithm round_robin
```

---

# Load Balancing Algorithms

The load balancer supports the following algorithms.

## Round Robin

Requests are distributed sequentially:

```text
Backend 1
Backend 2
Backend 3
Backend 1
Backend 2
Backend 3
...
```

Use:

```bash
--algorithm round_robin
```

## IP Hash

Requests are assigned according to the client's IP address.

```bash
--algorithm ip_hash
```

## Weighted Round Robin

Backends can receive traffic according to configured weights.

```bash
--algorithm weighted_round_robin
```

## Least Connections

New requests are sent toward the backend with the fewest active connections.

```bash
--algorithm least_connections
```

---

# Health Checking

The load balancer periodically checks the health of configured backends using:

```text
GET /health
```

A backend responding successfully is considered healthy.

If a backend becomes unavailable, the load balancer marks it unhealthy and stops routing normal traffic to it.

When the backend becomes available again, it is automatically detected and added back into the healthy backend pool.

Check the load balancer status using:

```bash
curl http://<LB_HOST>:<LB_PORT>/lb/status
```

The status endpoint provides information about:

- Backend health
- Active connections
- Request counts
- Failed requests
- Backend latency
- Number of healthy backends

---

# WebSocket Support

The chat application uses WebSockets through:

```text
/ws
```

The load balancer supports WebSocket upgrade requests and tunnels the connection to the selected backend.

The connection flow is:

```text
Browser
   |
   | WebSocket
   v
Load Balancer
   |
   +----> Backend 1
   |
   +----> Backend 2
   |
   +----> Backend 3
```

---

# Frontend Configuration

The frontend is implemented using Vite.

The proxy configuration is located in:

```text
vite.config.js
```

The frontend forwards application traffic to the load balancer:

```text
/ws      -> Load Balancer
/health  -> Load Balancer
/api     -> Load Balancer
/lb      -> Load Balancer
```

Run the frontend with:

```bash
npm run frontend
```

---

# Combined Frontend and Load Balancer

The `package.json` can provide a combined command for starting the frontend and load balancer:

```bash
npm run app
```

This starts the frontend and load balancer together.

The backend servers should be started separately.

---

# Database

The application uses SQLite for persistent storage.

The database layer is implemented in:

```text
server/database.py
```

The database stores:

- Rooms
- Messages
- Encrypted message data
- Message signatures
- Timestamps

Each backend instance currently maintains its own local SQLite database.

Therefore, the current implementation does not provide a globally shared database across backend machines.

---

# Application State

Each backend maintains its own WebSocket connection manager.

Therefore, WebSocket connections connected to different backend instances have independent in-memory connection state.

For example:

```text
Client A
   |
   v
Backend 1

Client B
   |
   v
Backend 2
```

A message broadcast by Backend 1 is currently sent only to WebSocket clients connected to Backend 1.

A fully distributed chat system would require a shared database and an inter-backend messaging mechanism such as a message broker or Pub/Sub system.

---

# Load Testing

The project includes a load generator for performance testing.

Example:

```bash
python3 load_generator.py \
  --url http://<LB_HOST>:<LB_PORT>/health \
  --requests 5000 \
  --concurrency 40 \
  --timeout 5 \
  --experiment three-backends \
  --out three-backends.json \
  --csv comparison.csv
```

The load generator reports:

```text
Requests
Concurrency
Successful
Failed
Elapsed time
Throughput
Dropout
P50
P95
P99
```

Results can be saved as JSON and CSV files.

---

# Failure Testing

The load balancer can be tested by stopping one backend.

Example process:

```text
1. Start all backend servers.
2. Verify all backends are healthy.
3. Generate requests through the load balancer.
4. Stop one backend.
5. Check /lb/status.
6. Verify the backend becomes unhealthy.
7. Continue generating requests.
8. Restart the backend.
9. Verify that it becomes healthy again.
```

Expected behavior:

```text
Backend 1 ✓
Backend 2 ✗
Backend 3 ✓

Traffic continues through:

Backend 1
Backend 3
```

After recovery:

```text
Backend 1 ✓
Backend 2 ✓
Backend 3 ✓
```

---

# API Endpoints

## Health Check

```text
GET /health
```

Returns the backend health status and current number of connected clients.

## Load Balancer Status

```text
GET /lb/status
```

Returns load balancer and backend statistics.

## WebSocket

```text
/ws
```

Used for real-time chat communication.

---

# Project Structure

```text
web_chat/
│
├── index.html
├── main.js
├── websocket.js
├── style.css
├── vite.config.js
├── package.json
├── package-lock.json
├── requirements.txt
│
├── server/
│   ├── main.py
│   ├── database.py
│   └── crypto_utils.py
│
├── load_balancer/
│   ├── balancer.py
│   ├── algorithms.py
│   └── health_checker.py
│
└── load-generator/
    ├── load_generator.py
    └── ...
```

---

# Important Files

| File | Purpose |
|---|---|
| `main.js` | Frontend application |
| `websocket.js` | WebSocket client |
| `vite.config.js` | Frontend proxy configuration |
| `server/main.py` | FastAPI and WebSocket backend |
| `server/database.py` | SQLite persistence |
| `server/crypto_utils.py` | Encryption and digital signatures |
| `load_balancer/balancer.py` | Load balancer and reverse proxy |
| `load_balancer/algorithms.py` | Load-balancing algorithms |
| `load_balancer/health_checker.py` | Backend health monitoring |
| `load-generator/load_generator.py` | Load testing |
| `package.json` | Project commands |

---

# Quick Start

### 1. Start each backend

```bash
python3 -m uvicorn server.main:app --host 0.0.0.0 --port <PORT>
```

### 2. Configure the backend addresses in the load balancer

```text
http://<BACKEND_1_HOST>:<PORT>
http://<BACKEND_2_HOST>:<PORT>
http://<BACKEND_3_HOST>:<PORT>
```

### 3. Start the load balancer

```bash
python3 load_balancer/balancer.py \
  --host 0.0.0.0 \
  --port <LB_PORT> \
  --backends <BACKEND_URLS> \
  --algorithm round_robin
```

### 4. Start the frontend

```bash
npm run frontend
```

Or start the frontend and load balancer together:

```bash
npm run app
```

### 5. Open the frontend

```text
http://<FRONTEND_HOST>:<FRONTEND_PORT>/
```

---

## License

This project is intended for educational and experimental use.
