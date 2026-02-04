from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Cookie, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

DM_PASSWORD = os.environ.get("DM_PASSWORD", "dm123")

sessions: Dict[str, Dict] = {}

# --------- Data model ---------


class PlayerLoginRequest(BaseModel):
    name: str

class DMLoginRequest(BaseModel):
    password: str

@dataclass
class Room:
    highlighted: Set[str] = field(default_factory=set)
    clients: Set[WebSocket] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


rooms: Dict[str, Room] = {}


def get_room(room_id: str) -> Room:
    room = rooms.get(room_id)
    if room is None:
        room = Room()
        rooms[room_id] = room
    return room


def create_session(role: str, name: str = None) -> str:
    import uuid
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "role": role,
        "name": name
    }
    return session_id


def get_session(session_id: str) -> Dict | None:
    return sessions.get(session_id)


# --------- Messaging helpers ---------


async def safe_send(ws: WebSocket, message: dict) -> bool:
    """Send JSON to a client; return False if it failed (disconnected, etc.)."""
    try:
        await ws.send_text(json.dumps(message))
        return True
    except Exception:
        return False


async def broadcast(room: Room, message: dict) -> None:
    """Broadcast to all clients in a room and prune dead sockets."""
    dead: Set[WebSocket] = set()
    for client in list(room.clients):
        ok = await safe_send(client, message)
        if not ok:
            dead.add(client)
    for client in dead:
        room.clients.discard(client)


# --------- WebSocket endpoint ---------


@app.websocket("/ws/{room_id}")
async def ws_room(ws: WebSocket, room_id: str):
    await ws.accept()

    session_id = ws.cookies.get("session_id")
    session = get_session(session_id) if session_id else None
    
    if not session:
        await safe_send(ws, {"type": "error", "message": "Not authenticated"})
        await ws.close()
        return

    room = get_room(room_id)
    room.clients.add(ws)

    # Initial snapshot (join/reconnect)
    await safe_send(
        ws,
        {
            "type": "state_sync",
            "highlighted": sorted(room.highlighted),
        },
    )

    try:
        while True:
            raw = await ws.receive_text()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await safe_send(ws, {"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")
            if not isinstance(msg_type, str):
                await safe_send(ws, {"type": "error", "message": "Missing or invalid 'type'"})
                continue

            if msg_type == "cube_toggle":
                cube_id = msg.get("cubeId")
                if not isinstance(cube_id, str):
                    await safe_send(ws, {"type": "error", "message": "cubeId must be a string"})
                    continue

                async with room.lock:
                    if cube_id in room.highlighted:
                        room.highlighted.remove(cube_id)
                        is_highlighted = False
                    else:
                        room.highlighted.add(cube_id)
                        is_highlighted = True

                await broadcast(
                    room,
                    {
                        "type": "cube_toggled",
                        "cubeId": cube_id,
                        "isHighlighted": is_highlighted,
                    },
                )

            elif msg_type == "state_request":
                await safe_send(
                    ws,
                    {
                        "type": "state_sync",
                        "highlighted": sorted(room.highlighted),
                    },
                )

            elif msg_type == "ping":
                await safe_send(ws, {"type": "pong"})

            else:
                await safe_send(ws, {"type": "error", "message": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        pass
    finally:
        room.clients.discard(ws)


# --------- Optional HTTP endpoints / static hosting ---------


@app.post("/api/login/player")
async def login_player(request: PlayerLoginRequest, response: Response):
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    
    session_id = create_session(role="player", name=request.name.strip())
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400
    )
    
    return {"ok": True, "role": "player", "name": request.name.strip()}


@app.post("/api/login/dm")
async def login_dm(request: DMLoginRequest, response: Response):
    if request.password != DM_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    
    session_id = create_session(role="dm")
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400
    )
    
    return {"ok": True, "role": "dm"}


@app.get("/api/logout")
async def logout(response: Response, session_id: str = Cookie(None)):
    if session_id and session_id in sessions:
        del sessions[session_id]
    
    response.delete_cookie("session_id")
    return RedirectResponse(url="/login.html")


@app.get("/api/session")
async def get_session_info(session_id: str = Cookie(None)):
    session = get_session(session_id) if session_id else None
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms)}


# If you create a ./static folder (next to server.py) with index.html inside,
# this will serve it at http://localhost:8000/
static_dir = Path(__file__).with_name("static")
if static_dir.exists() and static_dir.is_dir():
    # NOTE: The websocket route is registered above, so /ws/... still works.
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
else:
    @app.get("/")
    def root():
        return PlainTextResponse(
            "Server is running. Create a 'static/index.html' next to server.py to serve a test page."
        )