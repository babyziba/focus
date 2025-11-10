# relay_server.py (v0.2)
# Run: uvicorn relay_server:app --host 0.0.0.0 --port 8787
import os, asyncio, json, time, secrets
from dataclasses import dataclass, field
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

PING_INTERVAL = 15        # seconds
IDLE_DROP_SECS = 60       # drop if no updates/heartbeat for N secs
MAX_ROOM_USERS = 200
MAX_MSG_RATE_HZ = 10      # per-connection

# Simple token map: ROOM -> TOKEN (set via env, e.g., ROOMS="CSUN:abc123,TEAM:xyz")
ROOM_TOKENS = dict(pair.split(":") for pair in os.getenv("ROOMS", "").split(",") if pair)

app = FastAPI(title="Group Pomodoro Rooms Relay", version="0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

@dataclass
class Participant:
    user: str
    ws: WebSocket
    state: str = "INIT"
    phase: str = "OFF"
    last_ts: float = field(default_factory=lambda: time.time())
    focus_sec: float = 0.0
    active_sec: float = 0.0
    last_send_ts: float = 0.0      # rate limit guard
    last_heartbeat: float = field(default_factory=lambda: time.time())

    def step(self, now: float):
        dt = max(0.0, now - self.last_ts)
        self.last_ts = now
        if self.state != "INIT":
            self.active_sec += dt
        if self.state == "FOCUSED":
            self.focus_sec += dt

    @property
    def focus_pct(self) -> float:
        return 0.0 if self.active_sec <= 0 else max(0.0, min(100.0, 100.0 * self.focus_sec / self.active_sec))

@dataclass
class Room:
    name: str
    participants: Dict[str, Participant] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: time.time())

    def summary(self):
        arr = [{
            "user": p.user, "state": p.state, "phase": p.phase,
            "focus_sec": round(p.focus_sec, 1), "active_sec": round(p.active_sec, 1),
            "focus_pct": round(p.focus_pct, 1), "last_seen": round(time.time() - p.last_ts, 1)
        } for p in self.participants.values()]
        arr.sort(key=lambda x: (-x["focus_pct"], -x["active_sec"], x["user"]))
        return {"room": self.name, "created_at": self.created_at,
                "participants": arr, "leaderboard": arr[:5]}

class Hub:
    def __init__(self): self.rooms: Dict[str, Room] = {}; self._lock = asyncio.Lock()
    async def get_or_create(self, room: str) -> Room:
        async with self._lock:
            return self.rooms.setdefault(room, Room(name=room))
    async def remove(self, room: str, user: str):
        async with self._lock:
            r = self.rooms.get(room); 
            if not r: return
            r.participants.pop(user, None)
            # prune idle rooms optionally

hub = Hub()

@app.get("/rooms")
async def list_rooms(): return JSONResponse([r.summary() for r in hub.rooms.values()])

@app.get("/rooms/{room}")
async def get_room(room: str):
    r = hub.rooms.get(room); 
    return JSONResponse(r.summary() if r else {"room": room, "participants": [], "leaderboard": []})

async def broadcast(room: Room):
    payload = json.dumps({"type": "room_state", **room.summary()})
    drops = []
    for u, p in room.participants.items():
        try: await p.ws.send_text(payload)
        except Exception: drops.append(u)
    for u in drops:
        try: await room.participants[u].ws.close()
        except Exception: pass
        room.participants.pop(u, None)

def _auth_room(room: str, token: str | None):
    if not ROOM_TOKENS: return True  # auth off by default
    return ROOM_TOKENS.get(room) == (token or "")

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket,
                      room: str = Query(...), user: str = Query(...),
                      token: str | None = Query(None)):
    if not _auth_room(room, token):
        await websocket.close(code=4403);  # Forbidden
        return

    await websocket.accept()
    r = await hub.get_or_create(room)
    if len(r.participants) >= MAX_ROOM_USERS:
        await websocket.close(code=4409);  # Too many
        return

    # ensure unique username
    uname, i = user, 2
    while uname in r.participants:
        uname = f"{user}#{i}"; i += 1
    p = Participant(user=uname, ws=websocket)
    r.participants[uname] = p
    await broadcast(r)

    # heartbeat ticker
    async def ticker():
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await websocket.send_text(json.dumps({"type": "ping", "t": time.time()}))
            except Exception:
                break

    tick_task = asyncio.create_task(ticker())
    try:
        while True:
            try:
                # --- changed: catch RuntimeError from dropped sockets ---
                text = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                # idle cleanup
                now = time.time()
                for uname2, pp in list(r.participants.items()):
                    if now - pp.last_heartbeat > IDLE_DROP_SECS:
                        try:
                            await pp.ws.close()
                        except Exception:
                            pass
                        r.participants.pop(uname2, None)
                # broadcast every 2s regardless
                if int(now) % 2 == 0:
                    await broadcast(r)
                continue
            except (WebSocketDisconnect, RuntimeError):
                # client disconnected or socket not connected (accept race)
                break

            now = time.time()
            # step all participants
            for pp in r.participants.values():
                pp.step(now)

            data = json.loads(text); typ = data.get("type")
            # tiny per-conn rate-limit (drop if spamming)
            if now - p.last_send_ts < 1.0 / MAX_MSG_RATE_HZ:
                continue
            p.last_send_ts = now

            if typ == "hello":
                p.state = data.get("state", p.state); p.phase = data.get("phase", p.phase)
            elif typ == "update":
                p.state = data.get("state", p.state); p.phase = data.get("phase", p.phase)
            elif typ == "pong":
                p.last_heartbeat = now
            elif typ == "rename":
                new = data.get("user")
                if new and new not in r.participants:
                    r.participants[new] = p
                    r.participants.pop(uname, None)
                    uname = new
                    p.user = new

            await broadcast(r)

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        tick_task.cancel()
        await hub.remove(room, uname)
        rr = hub.rooms.get(room)
        if rr:
            await broadcast(rr)
