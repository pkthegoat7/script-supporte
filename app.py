"""Painel web FastAPI pra controlar o bot na Railway."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot import TicketBot


# ============================ Config ============================
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/data/config.json"))
PORT = int(os.environ.get("PORT", "8000"))

if not PANEL_PASSWORD:
    raise SystemExit("Defina PANEL_PASSWORD nas variaveis de ambiente.")

CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============================ App ===============================
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 24 * 7)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ============================ State =============================
class State:
    def __init__(self):
        self.bot: TicketBot | None = None
        self.status_label: str = "parado"
        self.status_color: str = "muted"
        self.log_ring: deque[tuple[str, str]] = deque(maxlen=500)
        self.ws_clients: set[WebSocket] = set()

    async def push_log(self, level: str, msg: str):
        self.log_ring.append((level, msg))
        await self._broadcast({"type": "log", "level": level, "msg": msg})

    async def set_status(self, label: str, color: str):
        self.status_label = label
        self.status_color = color
        await self._broadcast({"type": "status", "label": label, "color": color})

    async def _broadcast(self, payload: dict):
        dead = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)


state = State()


# ============================ Config storage ====================
DEFAULT_CONFIG = {
    "token": "",
    "parent_channel_id": 0,
    "bot_id": 0,
    "button_label": "Assumir Ticket",
    "embed_wait_seconds": 30,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# ============================ Auth ==============================
def require_auth(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(status_code=302, headers={"Location": "/login"})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PANEL_PASSWORD):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Senha incorreta"}, status_code=401
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ============================ Dashboard =========================
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("auth"):
        return RedirectResponse("/login", status_code=303)
    cfg = load_config()
    cfg_safe = {**cfg, "token": "•" * 12 if cfg["token"] else ""}
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "config": cfg_safe,
            "running": state.bot.running if state.bot else False,
            "status_label": state.status_label,
            "status_color": state.status_color,
        },
    )


@app.post("/api/config")
async def api_save_config(request: Request, payload: dict):
    if not request.session.get("auth"):
        raise HTTPException(401)
    cfg = load_config()
    for k in ("parent_channel_id", "bot_id", "embed_wait_seconds"):
        if k in payload and payload[k] != "":
            try:
                cfg[k] = int(payload[k])
            except (ValueError, TypeError):
                raise HTTPException(400, f"{k} deve ser numero")
    if "button_label" in payload and payload["button_label"]:
        cfg["button_label"] = str(payload["button_label"]).strip()
    if "token" in payload and payload["token"] and "•" not in payload["token"]:
        cfg["token"] = str(payload["token"]).strip()
    save_config(cfg)
    await state.push_log("ok", "config salva")
    return {"ok": True}


@app.post("/api/start")
async def api_start(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(401)
    if state.bot and state.bot.running:
        return {"ok": True, "already": True}
    cfg = load_config()
    missing = [k for k in ("token", "parent_channel_id", "bot_id") if not cfg.get(k)]
    if missing:
        raise HTTPException(400, f"config faltando: {', '.join(missing)}")
    state.bot = TicketBot(cfg, on_log=state.push_log, on_status=state.set_status)
    await state.set_status("conectando...", "warn")
    state.bot.start()
    return {"ok": True}


@app.post("/api/stop")
async def api_stop(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(401)
    if state.bot:
        await state.bot.stop()
    return {"ok": True}


@app.get("/api/status")
async def api_status(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(401)
    return {
        "running": state.bot.running if state.bot else False,
        "status": state.status_label,
        "color": state.status_color,
    }


@app.get("/api/token")
async def api_token(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(401)
    cfg = load_config()
    return {"token": cfg.get("token", "")}


# ============================ WebSocket =========================
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    cookie = websocket.cookies.get("session")
    if not cookie:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    state.ws_clients.add(websocket)
    try:
        await websocket.send_json({"type": "status", "label": state.status_label, "color": state.status_color})
        for level, msg in list(state.log_ring):
            await websocket.send_json({"type": "log", "level": level, "msg": msg})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT)
