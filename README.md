# Distributed Secure Group Chat with a Performance-Based Load Balancer

A secure, persistent group-chat application deployed across four lab containers.
Three of them run the chat backend; the fourth runs a custom load balancer that
picks a backend from live performance measurements rather than a fixed rotation.

Clients only ever talk to the load balancer.

## Submission

| | |
|---|---|
| **Load balancer URL** | **http://10.1.75.79:4237** |
| Submit a message | `POST http://10.1.75.79:4237/message` — body `{"client-name": "...", "msg": "..."}` |
| Read all messages | `GET  http://10.1.75.79:4237/feed` |
| Chat UI | http://10.1.75.79:4237/ |
| Repository | https://github.com/Abhigyan6091/Local-Web-Chat |
| Report | [`REPORT.md`](REPORT.md) · [`report/report.html`](report/report.html) (open in a browser, print to PDF) |

Reachable from the IIT Bhilai network.

```bash
curl -X POST http://10.1.75.79:4237/message \
     -H 'Content-Type: application/json' \
     -d '{"client-name":"alice","msg":"hello"}'

curl http://10.1.75.79:4237/feed
```

## Topology

```
                      client
                        |
                        v
        +-------------------------------+
        |  Sys1  172.17.0.38  :4000     |   load balancer + chat UI
        |  public http://10.1.75.79:4237|   adaptive threshold routing
        +-------------------------------+
              |          |          |
              v          v          v
        +----------+ +----------+ +----------+
        | Sys2     | | Sys3     | | Sys4     |   backend nodes
        | .39:4000 | | .40:4000 | | .41:4000 |   FastAPI + WebSockets
        +----------+ +----------+ +----------+
              |          |          |
              +----------+----------+
                         v
              PostgreSQL 16 on Sys2:5432
              one shared database, message_id PRIMARY KEY
```

Every container is capped by its cgroup at **one CPU and 512 MB** — `nproc`
reports the host's 120 cores, which is why the balancer scores nodes from
cgroup accounting rather than `psutil`.

### Port forwarding

The lab host publishes only internal ports **3000, 4000, 5000, 6000, 7000**, and
the external port keeps the container's SSH suffix:

| Container | SSH | internal | public |
|---|---|---|---|
| Sys1 | 2237 | 4000 | **4237** |
| Sys2 | 2238 | 4000 | 4238 |
| Sys3 | 2239 | 4000 | 4239 |
| Sys4 | 2240 | 4000 | 4240 |

Ports 8000, 8080 and 9000 are **not** forwarded.

## Features

### Chat application (unchanged from the previous submission)
- Real-time group chat over WebSockets, with a global room and private rooms
- Six-character private room codes, room switching, presence and user lists
- Persistent message history

### Security
- AES-GCM authenticated encryption for every stored message — plaintext is never
  written to disk
- Ed25519 signature per message, verified on read; tamper detection on both the
  ciphertext and the signature
- The AES key and each user's signing key are derived with HKDF from one cluster
  secret, so any node can read and verify another node's rows

### Shared persistence and de-duplication
- All three backends read and write one PostgreSQL database
- `message_id` is the primary key; a client may supply it, otherwise the backend
  generates a UUIDv4
- Inserts use `ON CONFLICT (message_id) DO NOTHING`, so a replay is reported as
  `duplicate: true` instead of being stored twice
- The balancer stamps `X-Message-Id` on `POST /message` so that *its own* retry
  onto a second backend cannot create a duplicate either

### Load balancer
- Composite load score per backend from container CPU, memory, in-flight
  requests and measured round-trip time
- Sticks to the current backend while it is under the threshold, switches to the
  least-loaded healthy backend when it crosses
- Active health probes plus passive failure detection; unhealthy backends are
  removed from rotation and re-admitted automatically
- Streaming asyncio proxy with keep-alive pools, WebSocket tunnelling, and the
  chat UI served from the same port

## Layout

```
common/sysmetrics.py          cgroup-accurate CPU/memory for a container
server/main.py                backend node: /message, /feed, /health, /ws
server/db_async.py            shared PostgreSQL layer, dedup, feed cache
server/crypto_utils.py        AES-GCM + Ed25519, keys derived from one secret
load_balancer/balancer.py     asyncio reverse proxy and routing loop
load_balancer/algorithms.py   scoring and the routing policies
load_generator/generator.py   variable users / lengths / intervals
experiments/                  measurement suite and plotting
deploy.py                     deployment to the four containers
```

## Running

Credentials are not stored in the source. Create `.lab.env` first (it is
git-ignored):

```bash
cp .lab.env.example .lab.env    # then fill in the lab SSH / database passwords
```

```bash
pip install -r requirements.txt

python deploy.py             # deploy everything
python deploy.py --status    # health of all four systems
python deploy.py --logs Sys3 # tail one node
python deploy.py --stop      # stop all services

pytest tests/ -q             # unit tests
```

### Load generator

Variable user count, variable message length and variable think-time between
messages:

```bash
python -m load_generator.generator \
    --url http://10.1.75.79:4237 \
    --users 100 --duration 60 \
    --min-len 16 --max-len 256 \
    --min-interval 0.0 --max-interval 0.08 \
    --feed-ratio 0.2 --retry-duplicates 0.05 \
    --out experiments/results/run.json
```

### Experiments and figures

```bash
python -m experiments.run_experiments all   # capacity, threshold, algorithms, failover, timeline
python -m experiments.make_plots            # writes report/figures/*.png
```

## Diagnostics

| Endpoint | Purpose |
|---|---|
| `GET /lb/status` | routing state, per-backend scores and counters |
| `GET /lb/metrics` | compact sample of all four systems, used for the plots |
| `POST /lb/config` | change threshold/algorithm at runtime |
| `GET /health` (per node) | liveness and the load report the balancer consumes |
| `GET /stats` (per node) | feed-cache and database diagnostics |
