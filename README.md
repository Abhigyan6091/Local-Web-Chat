# GroupChat Application

A real-time group chat application built with **Vite + Vanilla JavaScript** for the frontend, **FastAPI + WebSockets** for the backend, and **SQLite** for persistent message and room storage.

---

## Features

- **Global Chat:** Everyone joins the default `General Room` when they connect.
- **Private Rooms:** Create private rooms with shareable 6-character room codes, such as `X8K9P2`.
- **Join by Code:** Enter a 6-character room code to instantly join a private room.
- **SQLite Persistence:** Messages and rooms are stored in `server/chat.db` and persist across server restarts and browser reloads.
- **Mobile Responsive:** Responsive layout with a mobile-friendly sidebar, drawer navigation, and touch-friendly controls.

---

## Deployment

The application is deployed on an **IIT Bhilai SSH server**.

When connected to the **IIT Bhilai network**, the application can be accessed at:

**http://10.1.75.53:5201**

---

## Quick Start

### 1. Clone the Repository

Clone the repository and navigate to the project directory:

```bash
git clone https://github.com/maharshisoni24/web_chat.git
cd web_chat
````

### 2. Install Frontend Dependencies

Since `node_modules` is not included in the repository, install the required Node.js dependencies:

```bash
npm install
```

### 3. Install Backend Dependencies

Install the required Python dependencies:

```bash
pip install -r server/requirements.txt
```

### 4. Run the Application

Start both the frontend and backend with a single command:

```bash
npm run dev
```

The application will start the frontend and backend together.

Alternatively, on Windows, you can double-click `start.bat`.

| Service           | URL                            |
| ----------------- | ------------------------------ |
| Frontend UI       | `http://localhost:5000`        |
| Backend WebSocket | `ws://localhost:8000/ws`       |
| Health Check      | `http://localhost:8000/health` |

For the deployed application, access the frontend through:

```text
http://10.1.75.53:5201
```

when connected to the IIT Bhilai network.

---

## WebSocket Protocol

The client communicates with the backend through WebSockets.

### Join Server

```json
{
  "type": "join",
  "username": "name"
}
```

### Create Private Room

```json
{
  "type": "create_room",
  "name": "Room Name"
}
```

### Join Room by Code

```json
{
  "type": "join_room",
  "code": "X8K9P2"
}
```

### Switch Room

```json
{
  "type": "switch_room",
  "roomId": "room_123"
}
```

### Send Chat Message

```json
{
  "type": "message",
  "text": "Hello world!"
}
```

---

## File Structure

| File                 | Purpose                                                                     |
| -------------------- | --------------------------------------------------------------------------- |
| `index.html`         | Chat UI, room list, and create/join room modals                             |
| `style.css`          | Application styling, responsive layout, dark theme, room badges, and modals |
| `main.js`            | UI logic, room switching, message history rendering, and modal handling     |
| `websocket.js`       | WebSocket client and room communication logic                               |
| `server/main.py`     | FastAPI WebSocket server, multi-room management, and message handlers       |
| `server/database.py` | SQLite database manager for persistent rooms and message history            |
| `server/chat.db`     | SQLite database containing persistent application data                      |

```
```
