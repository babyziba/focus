# relay_server.py (fixed order + imports + single _auth_room)

import os, asyncio, json, time, secrets
from string import ascii_uppercase          # <-- ADD THIS
from dataclasses import dataclass, field
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware


PING_INTERVAL = 15
IDLE_DROP_SECS = 60
MAX_ROOM_USERS = 200
MAX_MSG_RATE_HZ = 10

ROOM_TOKENS = dict(pair.split(":") for pair in os.getenv("ROOMS", "").split(",") if pair)
DYNAMIC_TOKENS: dict[str, str] = {}   


def _new_code(n=5) -> str:
    return "".join(secrets.choice(ascii_uppercase) for _ in range(n))

# --- create app BEFORE using @app decorators ---
app = FastAPI(title="Group Pomodoro Rooms Relay", version="0.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # IMPORTANT when using "*"
    allow_methods=["*"],
    allow_headers=["*"],
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
    last_send_ts: float = 0.0
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
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self._lock = asyncio.Lock()
    async def get_or_create(self, room: str) -> Room:
        async with self._lock:
            return self.rooms.setdefault(room, Room(name=room))
    async def remove(self, room: str, user: str):
        async with self._lock:
            r = self.rooms.get(room)
            if not r: return
            r.participants.pop(user, None)

hub = Hub()

@app.get("/rooms")
async def list_rooms():
    return JSONResponse([r.summary() for r in hub.rooms.values()])

@app.get("/rooms/{room}")
async def get_room(room: str):
    r = hub.rooms.get(room)
    return JSONResponse(r.summary() if r else {"room": room, "participants": [], "leaderboard": []})

async def broadcast(room: Room):
    payload = json.dumps({"type": "room_state", **room.summary()})
    drops = []
    for u, p in room.participants.items():
        try:
            await p.ws.send_text(payload)
        except Exception:
            drops.append(u)
    for u in drops:
        try:
            await room.participants[u].ws.close()
        except Exception:
            pass
        room.participants.pop(u, None)

def _auth_room(room: str, token: str | None):   # <-- KEEP ONLY THIS VERSION
    if not ROOM_TOKENS and not DYNAMIC_TOKENS:
        return True
    expect = ROOM_TOKENS.get(room) or DYNAMIC_TOKENS.get(room)
    return expect == (token or "")

@app.post("/create_room")                         # <-- NOW VALID (app already defined)
async def create_room():
    code = _new_code()
    token = secrets.token_urlsafe(18)[:24]
    DYNAMIC_TOKENS[code] = token
    await hub.get_or_create(code)
    return JSONResponse({"code": code, "token": token})

def _auth_room(room: str, token: str | None):
    expect = ROOM_TOKENS.get(room) or DYNAMIC_TOKENS.get(room)
    if expect is None:
        return True          # rooms not in the token maps are open
    return expect == (token or "")

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket,
                      room: str = Query(...), user: str = Query(...),
                      token: str | None = Query(None)):
    if not _auth_room(room, token):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    r = await hub.get_or_create(room)
    if len(r.participants) >= MAX_ROOM_USERS:
        await websocket.close(code=4409)
        return
    uname, i = user, 2
    while uname in r.participants:
        uname = f"{user}#{i}"; i += 1
    p = Participant(user=uname, ws=websocket)
    r.participants[uname] = p
    await broadcast(r)

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
                text = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                now = time.time()
                for uname2, pp in list(r.participants.items()):
                    if now - pp.last_heartbeat > IDLE_DROP_SECS:
                        try: await pp.ws.close()
                        except Exception: pass
                        r.participants.pop(uname2, None)
                if int(now) % 2 == 0:
                    await broadcast(r)
                continue
            except (WebSocketDisconnect, RuntimeError):
                break

            now = time.time()
            for pp in r.participants.values():
                pp.step(now)

            data = json.loads(text); typ = data.get("type")
            if now - p.last_send_ts < 1.0 / MAX_MSG_RATE_HZ:
                continue
            p.last_send_ts = now

            if   typ == "hello":  p.state = data.get("state", p.state); p.phase = data.get("phase", p.phase)
            elif typ == "update": p.state = data.get("state", p.state); p.phase = data.get("phase", p.phase)
            elif typ == "pong":   p.last_heartbeat = now
            elif typ == "rename":
                new = data.get("user")
                if new and new not in r.participants:
                    r.participants[new] = p; r.participants.pop(uname, None); uname = new; p.user = new

            await broadcast(r)
    finally:
        tick_task.cancel()
        await hub.remove(room, uname)
        rr = hub.rooms.get(room)
        if rr:
            await broadcast(rr)
