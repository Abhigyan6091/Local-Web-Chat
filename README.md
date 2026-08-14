# GroupChat Application

A real-time group chat application built with **Vite + Vanilla JS** (frontend), **FastAPI + WebSockets** (backend), and **SQLite** for persistent message & room storage.

---

## 🚀 Features

- **🌐 Global Chat**: Everyone joins the default `General Room` on connect.
- **🔒 Private Rooms**: Create private rooms with shareable 6-character room codes (e.g. `X8K9P2`).
- **🔑 Join by Code**: Enter a 6-character code to instantly enter any private room.
- **💾 SQLite Persistence**: All messages and rooms are stored in `server/chat.db` and persist across server restarts and browser reloads.
- **📱 100% Mobile Responsive**: Fixed viewport positioning, smooth mobile drawer sidebar, and touch-friendly controls.

---

## ⚡ Quick Start

Run a single command from the project root:

```bash
npm run dev
```
*(or double-click `start.bat`)*

| Service | URL |
|---------|-----|
| **Frontend UI** | **http://localhost:5173** (or `http://<YOUR_LAN_IP>:5173` on Wi-Fi) |
| **Backend WebSocket** | **ws://localhost:8000/ws** |
| **Health Check** | **http://localhost:8000/health** |

---

## 📡 WebSocket Protocol

Client sends JSON messages over `ws://<HOST>:8000/ws`:

| Action | Sent JSON Payload |
|--------|-------------------|
| **Join Server** | `{ "type": "join", "username": "name" }` |
| **Create Private Room** | `{ "type": "create_room", "name": "Room Name" }` |
| **Join Room by Code** | `{ "type": "join_room", "code": "X8K9P2" }` |
| **Switch Room** | `{ "type": "switch_room", "roomId": "room_123" }` |
| **Send Chat Message** | `{ "type": "message", "text": "Hello world!" }` |

---

## 📁 File Structure

| File | Purpose |
|------|---------|
| `index.html` | Chat UI, Room list section, Create/Join room modals |
| `style.css` | Styling — Mobile flex layout, Dark theme, Room badges, Modals |
| `main.js` | UI logic — Room switching, message history rendering, modals |
| `websocket.js` | Client WebSocket API — Room creation & connection logic |
| `server/main.py` | FastAPI WebSocket server — Multi-room broadcasting & handlers |
| `server/database.py` | SQLite DB manager — Persistent rooms & message history |
| `server/chat.db` | Local SQLite database file |
