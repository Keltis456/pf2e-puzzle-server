from __future__ import annotations

import asyncio
import json
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Cookie, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    layers: int = 5
    bottom_size: int = 1


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


def can_highlight_circle(room: Room, layer: int, index: int) -> tuple[bool, str]:
    if layer == 0:
        return True, "bottom layer - always allowed"
    
    layer_below_size = room.bottom_size + (layer - 1)
    connected_below = []
    
    if index > 0 and index - 1 < layer_below_size:
        connected_below.append((layer - 1, index - 1))
    if index < layer_below_size:
        connected_below.append((layer - 1, index))
    
    connected_highlighted = []
    for (l, i) in connected_below:
        circle_id = f"{l}-{i}"
        if circle_id in room.highlighted:
            connected_highlighted.append(circle_id)
    
    if connected_highlighted:
        return True, f"connected to highlighted: {', '.join(connected_highlighted)}"
    
    required = [f"{l}-{i}" for (l, i) in connected_below]
    return False, f"no connected circles below (need {' or '.join(required) if required else 'none exist'})"


def has_dependent_circles(room: Room, layer: int, index: int) -> tuple[bool, list[str]]:
    dependent = []
    for highlighted_id in room.highlighted:
        h_layer, h_index = map(int, highlighted_id.split('-'))
        if h_layer > layer:
            temp_highlighted = room.highlighted - {f"{layer}-{index}"}
            if not can_highlight_circle_with_set(room, h_layer, h_index, temp_highlighted):
                dependent.append(highlighted_id)
    return len(dependent) > 0, dependent


def can_highlight_circle_with_set(room: Room, layer: int, index: int, highlighted_set: Set[str]) -> bool:
    if layer == 0:
        return True
    
    layer_below_size = room.bottom_size + (layer - 1)
    connected_below = []
    
    if index > 0 and index - 1 < layer_below_size:
        connected_below.append((layer - 1, index - 1))
    if index < layer_below_size:
        connected_below.append((layer - 1, index))
    
    for (l, i) in connected_below:
        circle_id = f"{l}-{i}"
        if circle_id in highlighted_set:
            return True
    
    return False


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
    try:
        await ws.accept()
        logger.info(f"[{room_id}] WebSocket accepted")

        session_id = ws.cookies.get("session_id")
        session = get_session(session_id) if session_id else None
        
        if not session:
            logger.warning(f"[{room_id}] Not authenticated, closing")
            await safe_send(ws, {"type": "error", "message": "Not authenticated"})
            await ws.close()
            return

        logger.info(f"[{room_id}] Authenticated as {session.get('role')}: {session.get('name', 'DM')}")

        room = get_room(room_id)
        room.clients.add(ws)

        logger.info(f"[{room_id}] Sending initial state_sync")
        await safe_send(
            ws,
            {
                "type": "state_sync",
                "highlighted": sorted(room.highlighted),
                "layers": room.layers,
                "bottomSize": room.bottom_size,
            },
        )
        logger.info(f"[{room_id}] Initial state sent successfully")

    except Exception as e:
        logger.error(f"[{room_id}] ERROR during WebSocket setup: {e}")
        import traceback
        traceback.print_exc()
        return

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

            if msg_type == "circle_toggle":
                circle_id = msg.get("circleId")
                if not isinstance(circle_id, str):
                    await safe_send(ws, {"type": "error", "message": "circleId must be a string"})
                    continue

                try:
                    layer, index = map(int, circle_id.split('-'))
                except (ValueError, AttributeError):
                    await safe_send(ws, {"type": "error", "message": "Invalid circleId format"})
                    continue

                async with room.lock:
                    if circle_id in room.highlighted:
                        has_deps, dependent_circles = has_dependent_circles(room, layer, index)
                        if has_deps:
                            reason = f"circles above depend on it: {', '.join(dependent_circles)}"
                            logger.warning(f"[ Deselect rejected for {circle_id} - {reason}")
                            await safe_send(ws, {
                                "type": "toggle_rejected",
                                "circleId": circle_id,
                                "reason": f"Cannot deselect: {reason}"
                            })
                            continue
                        
                        room.highlighted.remove(circle_id)
                        is_highlighted = False
                        logger.info(f"[{room_id}] ✅ Circle deselected: {circle_id} (layer {layer}, index {index})")
                    else:
                        can_highlight, reason = can_highlight_circle(room, layer, index)
                        if not can_highlight:
                            logger.warning(f"[ Select rejected for {circle_id} - {reason}")
                            await safe_send(ws, {
                                "type": "toggle_rejected",
                                "circleId": circle_id,
                                "reason": f"Cannot select: {reason}"
                            })
                            continue
                        
                        room.highlighted.add(circle_id)
                        is_highlighted = True
                        logger.info(f"[{room_id}] ✅ Circle selected: {circle_id} (layer {layer}, index {index}) - {reason}")

                logger.info(f"[{room_id}] Current highlighted circles: {sorted(room.highlighted)}")
                await broadcast(
                    room,
                    {
                        "type": "circle_toggled",
                        "circleId": circle_id,
                        "isHighlighted": is_highlighted,
                    },
                )

            elif msg_type == "pyramid_config":
                if session.get("role") != "dm":
                    await safe_send(ws, {"type": "error", "message": "Only DM can configure pyramid"})
                    continue

                layers = msg.get("layers")
                bottom_size = msg.get("bottomSize")

                if not isinstance(layers, int) or layers < 1:
                    await safe_send(ws, {"type": "error", "message": "layers must be a positive integer"})
                    continue

                if not isinstance(bottom_size, int) or bottom_size < 1:
                    await safe_send(ws, {"type": "error", "message": "bottomSize must be a positive integer"})
                    continue

                async with room.lock:
                    room.layers = layers
                    room.bottom_size = bottom_size
                    room.highlighted.clear()
                    logger.info(f"[{room_id}] Pyramid config updated: layers={layers}, bottom_size={bottom_size}")
                    logger.info(f"[{room_id}] All highlighted circles cleared")

                await broadcast(
                    room,
                    {
                        "type": "config_sync",
                        "layers": layers,
                        "bottomSize": bottom_size,
                    },
                )

            elif msg_type == "state_request":
                await safe_send(
                    ws,
                    {
                        "type": "state_sync",
                        "highlighted": sorted(room.highlighted),
                        "layers": room.layers,
                        "bottomSize": room.bottom_size,
                    },
                )

            elif msg_type == "ping":
                await safe_send(ws, {"type": "pong"})

            else:
                await safe_send(ws, {"type": "error", "message": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        logger.info(f"[{room_id}] WebSocket disconnected normally")
    except Exception as e:
        logger.error(f"[ in WebSocket message loop: {e}")
        import traceback
        traceback.print_exc()
    finally:
        logger.info(f"[{room_id}] Removing client from room")
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