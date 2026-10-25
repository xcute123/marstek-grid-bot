"""Telegram Mini App: HTTP-сервер (aiohttp) внутри процесса бота.

Авторизация — только подписанный Telegram initData (HMAC с токеном бота), отдельных паролей/ссылок с токенами нет.
Снаружи сервер открывает cloudflared (быстрый туннель), см. bot.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

log = logging.getLogger("marstek_web")
INDEX = Path(__file__).resolve().parent / "webapp" / "index.html"

# заполняется из bot.py: token, db, allowed(uid), build_state(d), touch_live(), bool_keys, choices, tz_now()
ctx = SimpleNamespace()


def verify_init_data(init_data: str, token: str, max_age: int = 2 * 86400) -> int | None:
    """Возвращает Telegram user id, если подпись initData верна и она не протухла."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None


@web.middleware
async def auth_mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        uid = verify_init_data(request.headers.get("X-Init-Data", ""), ctx.token)
        if uid is None:
            return web.json_response({"error": "auth"}, status=401)
        if not ctx.allowed(uid):
            return web.json_response({"error": "forbidden"}, status=403)
        request["uid"] = uid
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _device(request: web.Request, did_raw: Any):
    try:
        d = ctx.db.device(int(did_raw))
    except (TypeError, ValueError):
        return None
    if not d:
        return None
    uid = request["uid"]
    return d if (d["user_id"] == uid or ctx.db.is_shared(d["id"], uid)) else None


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX.read_text(encoding="utf-8"), content_type="text/html")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def api_devices(request: web.Request) -> web.Response:
    uid = request["uid"]
    out = []
    for d in ctx.db.devices(user_id=uid):
        out.append({"id": d["id"], "name": d["name"], "type": d["type"], "shared": False})
    for d in ctx.db.shared_devices(uid):
        out.append({"id": d["id"], "name": d["name"], "type": d["type"], "shared": True})
    return web.json_response({"devices": out})


async def api_state(request: web.Request) -> web.Response:
    d = _device(request, request.query.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    if request.query.get("live") == "1":
        ctx.touch_live()
    return web.json_response(_state(request, d))


def _state(request: web.Request, d: Any) -> dict:
    st = ctx.build_state(d)
    st["readonly"] = d["user_id"] != request["uid"]
    if st["readonly"]:
        st.pop("settings", None)
        u = ctx.db.user(d["user_id"])
        st["owner_name"] = (u["name"] or u["username"]) if u else None
    return st


def _bucket(rows: list[dict], n: int = 260) -> list[dict]:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = []
    for i in range(n):
        chunk = rows[int(i * step): int((i + 1) * step)] or [rows[-1]]
        o = {"ts": chunk[len(chunk) // 2]["ts"]}
        for k in ("soc", "bp", "grid", "eps", "wifi"):
            vals = [c[k] for c in chunk if c.get(k) is not None]
            o[k] = round(sum(vals) / len(vals), 1) if vals else None
        out.append(o)
    return out


async def api_history(request: web.Request) -> web.Response:
    d = _device(request, request.query.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    try:
        hours = max(1, min(168, int(request.query.get("hours", "6"))))
    except ValueError:
        hours = 6
    since = int(time.time()) - hours * 3600
    rows = [dict(r) for r in ctx.db.samples(d["id"], since)]
    return web.json_response({"hours": hours, "points": _bucket(rows)})


async def api_outages(request: web.Request) -> web.Response:
    d = _device(request, request.query.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    try:
        days = max(1, min(90, int(request.query.get("days", "7"))))
    except ValueError:
        days = 7
    return web.json_response(ctx.build_outages(d, days))


def _owner_only(request, d):
    return d["user_id"] == request["uid"]


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def api_settings(request: web.Request) -> web.Response:
    d = _device(request, request.query.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    return web.json_response(ctx.build_settings(d, request["uid"]))


async def api_setting(request: web.Request) -> web.Response:
    body = await _json(request)
    d = _device(request, body.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    if not _owner_only(request, d):
        return web.json_response({"error": "readonly"}, status=403)
    err = ctx.apply_setting(d, str(body.get("key", "")), body.get("value"))
    if err:
        return web.json_response({"error": "bad_setting", "message": err}, status=400)
    d = ctx.db.device(d["id"])
    return web.json_response({"settings": ctx.build_settings(d, request["uid"]), "state": _state(request, d)})


async def api_action(request: web.Request) -> web.Response:
    body = await _json(request)
    d = _device(request, body.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    action = str(body.get("action", ""))
    err = await ctx.action(d, request["uid"], action)
    if err:
        return web.json_response({"error": "action_failed", "message": err}, status=400)
    d2 = ctx.db.device(d["id"])
    if d2 is None:
        return web.json_response({"ok": True, "deleted": True})
    return web.json_response({"ok": True, "settings": ctx.build_settings(d2, request["uid"]), "state": _state(request, d2)})


async def api_share(request: web.Request) -> web.Response:
    body = await _json(request)
    d = _device(request, body.get("did"))
    if not d:
        return web.json_response({"error": "no_device"}, status=404)
    err = await ctx.share(d, request["uid"], str(body.get("op", "")), _int(body.get("viewer_id")))
    if err:
        return web.json_response({"error": "share_failed", "message": err}, status=400)
    return web.json_response({"ok": True, "settings": ctx.build_settings(ctx.db.device(d["id"]), request["uid"])})


async def api_admin(request: web.Request) -> web.Response:
    uid = request["uid"]
    if not ctx.is_admin(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    if request.method == "POST":
        body = await _json(request)
        err = await ctx.admin_op(uid, str(body.get("op", "")), _int(body.get("target")))
        if err:
            return web.json_response({"error": "admin_failed", "message": err}, status=400)
    return web.json_response(ctx.admin_list())


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/api/devices", api_devices),
        web.get("/api/state", api_state),
        web.get("/api/history", api_history),
        web.get("/api/outages", api_outages),
        web.get("/api/settings", api_settings),
        web.post("/api/setting", api_setting),
        web.post("/api/action", api_action),
        web.post("/api/share", api_share),
        web.get("/api/admin", api_admin),
        web.post("/api/admin", api_admin),
    ])
    return app


async def start(port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("Mini App server on 127.0.0.1:%s", port)
    return runner
