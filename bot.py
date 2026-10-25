"""
Marstek Cloud Grid Alert Telegram Bot.

Single-screen inline keyboard UI (Bot API 9.4 button `style`: primary / success / danger).
Polls Marstek Cloud (`eu.hamedata.com`) and notifies immediately when grid power appears or disappears.
"""
from __future__ import annotations

import asyncio
import html
import subprocess
from types import SimpleNamespace
import json
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from rich.logging import RichHandler
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup as M, MenuButtonWebApp, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import anomaly
import marstek
import mqtt_live
import webapp
from db import DB

BASE = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


load_dotenv(BASE / ".env")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN = int(os.environ.get("ADMIN_ID", "0") or "0")
POLL_SEC = max(10, int(os.environ.get("POLL_SEC", "180") or "180"))

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("marstek_bot")

db = DB(str(BASE / "data.db"), ADMIN)
CERT_DIR = str(BASE / "mqtt_certs")
LIVE: mqtt_live.MarstekLive | None = None
if mqtt_live.MarstekLive.available(CERT_DIR):
    LIVE = mqtt_live.MarstekLive(CERT_DIR, interval=POLL_SEC)
H = ParseMode.HTML
e = html.escape

REGIONS = {
    "eu": ("🇪🇺 Европа (eu.hamedata.com)", "https://eu.hamedata.com"),
    "global": ("🌍 Глобал (www.hamedata.com)", "https://www.hamedata.com"),
}

MODE_META = {
    "auto": ("🔀 Авто (связь + сеть)", "онлайн и телеметрия сети/заряда"),
    "online": ("📡 По связи (Online)", "устройство вышло на связь в облако (status=1)"),
    "grid": ("⚡ По мощности (Grid/Charge)", "|grid| > порога или идёт заряд от сети"),
}

FIELD_LABELS: dict[str, tuple[str, str]] = {
    "soc": ("🔋 Заряд (SoC)", "%"),
    "_work_status_text": ("⚙️ Режим батареи", ""),
    "grid": ("🔌 Счётчик сети (CT)", "Вт"),
    "charge": ("⚡ Зарядка (Charge)", "Вт"),
    "discharge": ("🪫 Разряд (Discharge)", "Вт"),
    "eps": ("🔌 Выход EPS (резерв)", "Вт"),
    "load": ("🏠 Нагрузка (Load)", "Вт"),
    "pv": ("☀️ Солнце (PV)", "Вт"),
    "day_charge_kwh": ("📈 Заряжено сегодня", "кВт·ч"),
    "day_discharge_kwh": ("📉 Отдано сегодня", "кВт·ч"),
    "total_charge_kwh": ("Σ Заряжено всего", "кВт·ч"),
    "total_discharge_kwh": ("Σ Отдано всего", "кВт·ч"),
    "work_mode_text": ("🎛 Режим работы", ""),
    "wifi_dbm": ("📶 Wi-Fi", "dBm"),
    "status": ("📡 Статус связи", ""),
    "profit": ("💶 Экономия (Profit)", "€"),
    "version": ("🧩 Прошивка", ""),
    "sn": ("🔢 Серийный номер", ""),
    "mac": ("🌐 MAC", ""),
    "type": ("📦 Модель", ""),
    "report_time": ("🕒 Отчёт устройства", ""),
}

CAPACITIES = (2.56, 5.12, 10.24, 15.36)


# ------------------------------------------------------------------ UI primitives
def b(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    """Inline button with optional Bot API 9.4 style ('primary' | 'success' | 'danger')."""
    return InlineKeyboardButton(
        text,
        callback_data=data,
        api_kwargs={"style": style} if style else None,
    )


HOME_BTN = b("‹ Меню", "home")
WEBAPP_URL: str = ""          # https-адрес Mini App (быстрый туннель cloudflared), заполняется при старте


def app_btn() -> InlineKeyboardButton | None:
    return InlineKeyboardButton("📱 Приложение", web_app=WebAppInfo(WEBAPP_URL)) if WEBAPP_URL else None


async def show(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, mid: int | None, text: str, kb: M | None = None) -> int:
    """Edit message in place; fall back to sending a new message."""
    if mid:
        try:
            await ctx.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=mid,
                parse_mode=H,
                reply_markup=kb,
                link_preview_options={"is_disabled": True},
            )
            return mid
        except BadRequest as ex:
            if "not modified" in str(ex).lower():
                return mid
    m = await ctx.bot.send_message(
        chat_id,
        text,
        parse_mode=H,
        reply_markup=kb,
        link_preview_options={"is_disabled": True},
    )
    return m.message_id


async def notify(ctx: ContextTypes.DEFAULT_TYPE, uid: int, text: str, kb: M | None = None) -> None:
    try:
        await ctx.bot.send_message(uid, text, parse_mode=H, reply_markup=kb)
    except Exception as ex:
        log.warning("send to %s failed: %s", uid, ex)


async def drop(msg: Any) -> None:
    if not msg:
        return
    try:
        await msg.delete()
    except Exception:
        pass


# ------------------------------------------------------------------ Helpers
def is_admin(uid: int) -> bool:
    if ADMIN and uid == ADMIN:
        return True
    u = db.user(uid)
    return bool(u) and u["status"] == "admin"


def allowed(uid: int) -> bool:
    if ADMIN and uid == ADMIN:
        return True
    u = db.user(uid)
    return bool(u) and u["status"] in ("admin", "allowed")


def can_view(d: Any, uid: int) -> bool:
    return bool(d) and (d["user_id"] == uid or db.is_shared(d["id"], uid))


async def notify_dev(ctx: ContextTypes.DEFAULT_TYPE, d: Any, text: str, kb: M | None = None) -> None:
    """Уведомление владельцу и всем, с кем устройство расшарено (с включёнными уведомлениями)."""
    await notify(ctx, d["user_id"], text, kb)
    for vid in db.alert_viewers(d["id"]):
        if vid != d["user_id"] and allowed(vid):
            await notify(ctx, vid, text, kb)


def fmt_duration(seconds: int | float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    mins = max(1, int(round(seconds / 60)))
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m or not parts:
        parts.append(f"{m} мин")
    return " ".join(parts)


KYIV = ZoneInfo("Europe/Kyiv")


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, KYIV)
    now = datetime.now(KYIV)
    if dt.date() == now.date():
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%d.%m %H:%M")


def parse_cache(d: Any) -> dict[str, Any]:
    try:
        return json.loads(d["cache"]) if d["cache"] else {}
    except Exception:
        return {}


def hidden_set(d: Any) -> set[str]:
    try:
        return set(json.loads(d["hidden_fields"])) if d["hidden_fields"] else set()
    except Exception:
        return set()


def merge_live(raw: dict[str, Any]) -> dict[str, Any]:
    """Если по MQTT есть свежий кадр (cd=1) — подставляем реальные ватты вместо облачных нулей."""
    if not LIVE or not raw.get("devid"):
        return raw
    frame = LIVE.fresh(str(raw["devid"]))
    if not frame:
        return raw
    lv = mqtt_live.decode(frame)
    out = dict(raw)
    out["_live"] = True
    last = LIVE.latest(str(raw["devid"]))
    if last:
        out["_cloud_report_time"] = raw.get("report_time")
        out["report_time"] = int(last[0])  # время последнего MQTT-кадра, а не застывшая метка облака
    out["_work_status"] = lv["work_status"]
    out["_work_status_text"] = lv["work_status_text"]
    if lv["soc"] is not None:
        out["soc"] = lv["soc"]
    out["charge"] = lv["charge"]
    out["discharge"] = lv["discharge"]
    if lv["grid_power"] is not None:
        out["grid"] = lv["grid_power"]
    if lv["eps_power"] is not None:
        out["eps"] = lv["eps_power"]
    for k in ("day_charge_kwh", "day_discharge_kwh", "total_charge_kwh", "total_discharge_kwh",
              "wifi_dbm", "work_mode_text", "battery_power", "max_discharge_w"):
        if lv.get(k) is not None:
            out[k] = lv[k]
    return out


def device_state(d: Any) -> tuple[bool | None, str, dict[str, Any]]:
    base = parse_cache(d)
    raw = merge_live(base)
    if not raw.get("_live") and LIVE and LIVE._connected:
        # MQTT раньше отвечал, а теперь батарея молчит при живом соединении бота — это сильнее,
        # чем облачный status (он держит «1» ещё долго после пропажи связи)
        last = LIVE.latest(str(base.get("devid") or ""))
        if last:
            silent = int(time.time() - last[0])
            if silent > LIVE.interval * 4 + 30:
                return None, f"связь с батареей потеряна (молчит {fmt_duration(silent)})", raw
    if raw.get("_live"):
        # свежий MQTT-кадр сам по себе доказывает, что батарея на связи (облачный status может отставать)
        if raw.get("_work_status") == "4":
            return False, "батарея ушла в резерв (EPS) — сети нет", raw
        parts = [f"режим: {raw.get('_work_status_text')}"]
        g = marstek.to_float(raw.get("grid"))
        if g is not None:
            parts.append(f"сеть {g:+.0f} Вт")
        return True, "живые данные · " + ", ".join(parts), raw
    on, reason = marstek.detect_grid_state(
        raw,
        mode=d["mode"] or "auto",
        grid_threshold_w=float(d["grid_threshold"] or 5.0),
    )
    return on, reason, raw


LEVELS = (30, 15, 10)           # пороги SoC (%) для предупреждений при отсутствии сети
DEFAULT_MAX_W = 2500.0          # если батарея не сообщила свой предел отдачи
OVER_ON, OVER_OFF = 0.90, 0.75  # перегрузка: алерт при 90% предела, сброс при <75%


def fmt_dt(ts: int | float | None) -> str:
    return datetime.fromtimestamp(ts, KYIV).strftime("%d.%m %H:%M") if ts else "—"


def runtime_forecast(d: Any, raw: dict[str, Any]) -> tuple[float, float, int] | None:
    """(часов, мощность Вт, unix-время «до») — на сколько хватит заряда при текущей нагрузке."""
    if power_missing(raw):
        return None
    soc = marstek.to_float(raw.get("soc"))
    p = marstek.to_float(raw.get("discharge")) or marstek.to_float(raw.get("load"))
    if soc is None or not p or p <= 10:
        return None
    cap = float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH)
    hours = (soc / 100.0 * cap * 1000.0) / p
    return hours, p, int(time.time() + hours * 3600)


def forecast_text(d: Any, raw: dict[str, Any]) -> str:
    if not d["set_fc"]:
        return ""
    f = runtime_forecast(d, raw)
    if not f:
        return ""
    hours, p, until = f
    until_s = datetime.fromtimestamp(until, KYIV).strftime("%H:%M")
    return f"~<b>{e(fmt_duration(hours * 3600))}</b> при {p:.0f} Вт (до ≈{until_s})"


def link_lost_sec(d: Any) -> int:
    """Сколько секунд молчит MQTT у батареи при живом соединении бота (0 = всё нормально)."""
    if not LIVE or not LIVE._connected:
        return 0
    last = LIVE.latest(str(d["devid"]))
    if not last:
        return 0
    silent = int(time.time() - last[0])
    limit = max(int(d["set_link_min"] or 4) * 60, LIVE.interval * 2 + 10)
    return silent if silent > limit else 0


def cfg_levels(d: Any) -> list[int]:
    out = []
    for x in str(d["set_levels"] or "").split(","):
        x = x.strip()
        if x.isdigit() and 1 <= int(x) <= 99:
            out.append(int(x))
    return sorted(set(out), reverse=True)


def over_limit(d: Any, raw: dict[str, Any]) -> float:
    """Предел отдачи, Вт: ручной из настроек → от батареи (mdp_w) → 2500."""
    manual = float(d["set_over_w"] or 0)
    if manual > 0:
        return manual
    lim = marstek.to_float(raw.get("max_discharge_w"))
    return lim if lim and 500 <= lim <= 6000 else DEFAULT_MAX_W


def state_age(d: Any, on: bool | None, now: int) -> str:
    """Сколько длится текущее состояние. Если живое состояние уже отличается от записанного в БД
    (переход ещё не зафиксирован опросом) — таймер старого состояния показывать нельзя."""
    if not d["last_change_ts"] or on is None:
        return ""
    if d["grid_was_on"] is not None and bool(d["grid_was_on"]) != bool(on):
        return "только что"
    return fmt_duration(now - d["last_change_ts"])


def state_icon(on: bool | None) -> str:
    return "⏳" if on is None else ("🟢" if on else "🔴")


def format_field_value(key: str, val: Any, d: Any = None) -> str:
    if val is None or val == "":
        return "—"
    if key == "status":
        try:
            return "🟢 Online (1)" if int(float(str(val))) == 1 else "🔴 Offline (0)"
        except ValueError:
            return str(val)
    if key == "report_time":
        ts = marstek.parse_report_time(val)
        return fmt_ts(ts) if ts else str(val)
    if key == "soc" and d is not None:
        soc_f = marstek.to_float(val)
        if soc_f is not None:
            cap = float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH)
            kwh = round((soc_f / 100.0) * cap, 2)
            return f"{soc_f:.0f}% ({kwh} кВт·ч из {cap} кВт·ч)"
    label, unit = FIELD_LABELS.get(key, (key, ""))
    if isinstance(val, float):
        val = f"{val:.0f}" if unit in ("Вт", "dBm") else (f"{val:.2f}" if unit == "кВт·ч" else f"{val:g}")
    return f"{val} {unit}".strip()


POWER_KEYS = ("grid", "charge", "discharge", "load", "pv")


def power_missing(raw: dict[str, Any]) -> bool:
    """Облако (getDeviceList) для части моделей (напр. Venus E 3.0 / VNSE3) отдаёт мощности всегда 0 —
    реальные значения идут только по MQTT. Все пять полей ровно 0 = данных нет, а не «0 Вт»."""
    if raw.get("_live"):
        return False
    vals = [marstek.to_float(raw.get(k)) for k in POWER_KEYS]
    return all(v in (None, 0.0) for v in vals)


def data_age_line(raw: dict[str, Any]) -> str:
    ts = marstek.parse_report_time(raw.get("report_time"))
    if not ts:
        return ""
    age = int(time.time()) - ts
    src = "живой MQTT-кадр" if raw.get("_live") else "данные облака"
    return f"{src} от {fmt_ts(ts)} ({fmt_duration(age) or 'только что'} назад)"


def summary_metrics_line(d: Any, raw: dict[str, Any]) -> str:
    if not raw:
        return "нет данных"
    parts = []
    soc = marstek.to_float(raw.get("soc"))
    if soc is not None:
        cap = float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH)
        kwh = round((soc / 100.0) * cap, 2)
        parts.append(f"🔋 {soc:.0f}% ({kwh} кВт·ч)")
    if power_missing(raw):
        parts.append("мощности облако не передаёт")
        return " · ".join(parts)
    grid = marstek.to_float(raw.get("grid"))
    if grid is not None:
        parts.append(f"🔌 сеть {grid:+.0f} Вт")
    chg = marstek.to_float(raw.get("charge"))
    if chg:
        parts.append(f"⚡ заряд {chg:.0f} Вт")
    dis = marstek.to_float(raw.get("discharge"))
    if dis:
        parts.append(f"🪫 разряд {dis:.0f} Вт")
    load = marstek.to_float(raw.get("load"))
    if load:
        parts.append(f"🏠 нагр. {load:.0f} Вт")
    pv = marstek.to_float(raw.get("pv"))
    if pv:
        parts.append(f"☀️ PV {pv:.0f} Вт")
    return " · ".join(parts) if parts else "телеметрия получена"


# ------------------------------------------------------------------ Cloud Polling Logic
async def refresh_account(acc: Any) -> list[Any]:
    """Poll Marstek Cloud for one account, update token & device caches in DB."""
    client = marstek.MarstekClient(
        email=acc["email"],
        password_or_md5=acc["pwd_md5"],
        token=acc["token"],
        base_url=acc["base_url"],
    )
    dev_list = await asyncio.to_thread(client.get_devices)
    now = int(time.time())
    db.set_account(acc["id"], token=client.token, last_ok=now, last_err=None)
    if LIVE:
        for dv in dev_list:
            LIVE.register(str(dv.get("devid") or ""), str(dv.get("type") or ""),
                          str(dv.get("mac") or ""), str(dv.get("salt") or ""))
    return db.sync_account_devices(acc["id"], acc["user_id"], dev_list)


def build_transition_alert(d: Any, grid_on: bool, reason: str, raw: dict[str, Any], prev_change_ts: int | None) -> str:
    now = int(time.time())
    dur = fmt_duration(now - prev_change_ts) if prev_change_ts else ""
    title = "🟢 ⚡ <b>СЕТЬ ПОЯВИЛАСЬ!</b>" if grid_on else "🔴 ⚡ <b>СЕТЬ ПРОПАЛА!</b>"
    lines = [
        title,
        f"🔌 <b>{e(d['name'])}</b> ({e(d['type'])})",
        f"Статус: <b>{'Сеть в норме' if grid_on else 'Отключение сети'}</b> — {e(reason)}",
    ]
    if dur:
        if grid_on:
            lines.append(f"⏱ Света не было: <b>{e(dur)}</b>")
        else:
            lines.append(f"⏱ Свет был: <b>{e(dur)}</b>")

    mline = summary_metrics_line(d, raw)
    if mline:
        lines.append(f"📊 {e(mline)}")

    if not grid_on:
        ft = forecast_text(d, raw)
        if ft:
            lines.append(f"⏳ Прогноз от батареи: {ft}")

    return "\n".join(lines)


# ------------------------------------------------------------------ Screens
def home_screen(uid: int) -> tuple[str, M]:
    devs = list(db.devices(user_id=uid)) + list(db.shared_devices(uid))
    accs = db.accounts(user_id=uid)
    if not accs and not devs:
        text = (
            "<b>⚡ Marstek Cloud — Монитор Сети</b>\n\n"
            "Бот отслеживает ваши батареи и счётчики Marstek (<code>eu.hamedata.com</code>) "
            "и мгновенно присылает уведомление при <b>появлении</b> или <b>пропадании сети</b>.\n\n"
            "Добавьте аккаунт Marstek — пароль хешируется в MD5, а сообщение с ним сразу удаляется из чата."
        )
        kb = [[b("➕ Подключить Marstek", "add", "primary")]]
    else:
        lines = ["<b>⚡ Marstek Cloud — Монитор Сети</b>\n"]
        kb = []
        now = int(time.time())
        for d in devs:
            on, reason, raw = device_state(d)
            ic = state_icon(on)
            dur = state_age(d, on, now)
            state_lbl = "Сеть есть" if on else ("Нет сети" if on is False else "Ожидание")
            if dur:
                state_lbl += f" ({dur})"
            mute_ic = (" 🔕" if d["muted"] else "") + (" 👁" if d["user_id"] != uid else "")
            lines.append(
                f"{ic} <b>{e(d['name'])}</b> <i>({e(d['type'])})</i>{mute_ic}\n"
                f"      {e(state_lbl)} · {e(reason)}\n"
                f"      {e(summary_metrics_line(d, raw))}"
            )
            btn_style = None if on is None else ("success" if on else "danger")
            soc_val = raw.get("soc")
            soc_tag = f" · {soc_val}%" if soc_val is not None else ""
            kb.append([b(f"{ic} {'👁 ' if d['user_id'] != uid else ''}{d['name']}{soc_tag}"[:60], f"d:{d['id']}", btn_style)])

        if not devs:
            lines.append("На подключённом аккаунте пока не найдено устройств.")

        stamps = [fmt_ts(d["checked"]) for d in devs if d["checked"]]
        lines.append(
            f"\n<i>обновлено: {stamps[0] if stamps else '—'} · интервал опроса: {POLL_SEC} сек</i>"
        )
        text = "\n".join(lines)
        kb.append([b("🔄 Обновить", "poll_now", "primary"), b(f"⚙️ Аккаунты ({len(accs)})", "accs")])
        if not accs:
            lines.append("\n<i>Свой аккаунт Marstek можно подключить в «⚙️ Аккаунты».</i>")

    if app_btn():
        kb.insert(0, [app_btn()])
    if is_admin(uid):
        pend = len(db.users("pending"))
        kb.append([b(f"🛡 Админ{' · ' + str(pend) if pend else ''}", "adm", "danger" if pend else None)])
    return text, M(kb)


def accounts_screen(uid: int) -> tuple[str, M]:
    accs = db.accounts(user_id=uid)
    lines = ["<b>⚙️ Аккаунты Marstek Cloud</b>\n"]
    kb = []
    if not accs:
        lines.append("Нет подключённых аккаунтов.")
    for a in accs:
        devs = db.devices(user_id=uid, account_id=a["id"])
        status_ic = "🟢" if not a["last_err"] else "⚠️"
        lines.append(
            f"{status_ic} <b>{e(a['email'])}</b>\n"
            f"      сервер: <code>{e(a['base_url'])}</code> · устройств: {len(devs)}\n"
            f"      опрос: {fmt_ts(a['last_ok'])}"
            + (f"\n      ⚠️ <code>{e(str(a['last_err'])[:120])}</code>" if a["last_err"] else "")
        )
        kb.append([b(f"🔄 {a['email']}"[:42], f"accr:{a['id']}"), b("🗑", f"accdl:{a['id']}", "danger")])
    kb.append([b("➕ Добавить аккаунт", "add", "primary")])
    kb.append([HOME_BTN])
    return "\n".join(lines), M(kb)


def device_screen(d: Any, uid: int | None = None) -> tuple[str, M]:
    viewer = uid is not None and uid != d["user_id"]
    on, reason, raw = device_state(d)
    ic = state_icon(on)
    now = int(time.time())
    dur = state_age(d, on, now)
    state_title = "Сеть есть" if on else ("Нет сети" if on is False else "Ожидание данных")
    if dur:
        state_title += f" ({dur})"

    mode = d["mode"] or "auto"
    mode_name, _ = MODE_META.get(mode, (mode, ""))

    owner = db.user(d["user_id"]) if viewer else None
    lines = [
        f"{ic} <b>{e(d['name'])}</b> · <code>{e(d['type'])}</code>",
        *([f"👁 <i>Только просмотр · владелец: {e((owner['name'] or owner['username'] or str(owner['id'])) if owner else '—')}</i>"] if viewer else []),
        f"<i>ID: <code>{e(d['devid'])}</code>" + (f" · SN: <code>{e(d['sn'])}</code>" if d["sn"] else "") + "</i>",
        "",
        f"Статус: <b>{e(state_title)}</b>\nПричина: {e(reason)}",
        "",
    ]

    if on is False:
        ft = forecast_text(d, raw)
        if ft:
            lines.append(f"⏳ Хватит заряда: {ft}")
            lines.append("")

    if not raw:
        lines.append("Телеметрия ещё не загружена. Нажмите «🔄 Обновить».")
    else:
        hidden = hidden_set(d)
        no_power = power_missing(raw)
        is_live = bool(raw.get("_live"))
        # Только осмысленные поля; служебные (id, salt, dlock, is_illegal, ...) не показываем
        ordered_keys = [k for k in FIELD_LABELS if k in raw]
        if is_live:
            # load/pv из облака — заведомые нули, у Venus E 3.0 их нет и в MQTT-кадре
            ordered_keys = [k for k in ordered_keys if k not in ("load", "pv")]
        shown = 0
        lines.append("<b>📊 Телеметрия" + (" (живые, MQTT)" if is_live else "") + ":</b>")
        for k in ordered_keys:
            if k in hidden:
                continue
            if no_power and k in POWER_KEYS:
                continue
            shown += 1
            label, _ = FIELD_LABELS.get(k, (f"• {k}", ""))
            val_str = format_field_value(k, raw.get(k), d)
            lines.append(f"  {e(label)}: <b>{e(val_str)}</b>")
        if no_power:
            lines.append(
                "  ⚠️ <i>Мощности облако для этой модели не отдаёт, а живого MQTT-кадра пока нет — "
                "подождите до минуты или нажмите «🔄 Обновить».</i>"
            )
        if shown == 0 and not no_power:
            lines.append("  <i>Все поля скрыты в «⚙️ Поля».</i>")
        age = data_age_line(raw)
        if age:
            lines.append(f"  <i>{e(age)}</i>")
        lines.append("")

    if not viewer:
        lines.append(f"Режим детекции сети: <b>{e(mode_name)}</b>")
        lines.append("🔕 Оповещения выключены" if d["muted"] else "🔔 Оповещения о сети включены")
        n_sh = len(db.shares(d["id"]))
        if n_sh:
            lines.append(f"👥 Доступ открыт: {n_sh}")
    if d["checked"]:
        lines.append(f"<i>обновлено: {fmt_ts(d['checked'])}</i>")

    kb = [
        [b("📡 LIVE в чате", f"dlv:{d['id']}", "success")] + ([app_btn()] if app_btn() else []),
        [b("🔄 Обновить", f"dr:{d['id']}", "primary"), b("🧪 Тест пуша", f"dt:{d['id']}")],
        [
            b(("✅ " if mode == "auto" else "") + "🔀 Авто", f"dm:{d['id']}:auto", "success" if mode == "auto" else None),
            b(("✅ " if mode == "online" else "") + "📡 Online", f"dm:{d['id']}:online", "success" if mode == "online" else None),
            b(("✅ " if mode == "grid" else "") + "⚡ Grid Вт", f"dm:{d['id']}:grid", "success" if mode == "grid" else None),
        ],
        [
            b(f"{c} кВт·ч", f"dc:{d['id']}:{c}", "success" if abs(float(d["capacity_kwh"] or 5.12) - c) < 0.05 else None)
            for c in CAPACITIES
        ],
        [
            b("🔔 Выключить пуши", f"dmu:{d['id']}")
            if not d["muted"]
            else b("🔕 Включить пуши", f"dmu:{d['id']}", "success"),
            b("⚙️ Поля", f"df:{d['id']}"),
        ],
        [b("📊 Статистика отключений", f"dst:{d['id']}:7"), b("🔔 Настройки алертов", f"dse:{d['id']}")],
        [b("👥 Поделиться", f"dsh:{d['id']}"), b("🗑 Удалить", f"ddl:{d['id']}", "danger")],
        [HOME_BTN],
    ]
    if viewer:
        mine = next((r for r in db.shares(d["id"]) if r["viewer_id"] == uid), None)
        kb = [
            kb[0],
            [b("🔄 Обновить", f"dr:{d['id']}", "primary"), b("📊 Статистика отключений", f"dst:{d['id']}:7")],
            [b("🔔 Мои уведомления: вкл" if mine and mine["alerts"] else "🔕 Мои уведомления: выкл", f"dvn:{d['id']}")],
            [HOME_BTN],
        ]
    return "\n".join(lines), M(kb)


def stats_screen(d: Any, days: int) -> tuple[str, M]:
    now = int(time.time())
    since = now - days * 86400
    watch_from = max(since, int(d["created"] or since))
    window = max(1, now - watch_from)
    rows = db.outages(d["id"], since)
    durs = [(r, max(0, (r["end_ts"] or now) - max(r["start_ts"], since))) for r in rows]
    total = sum(x for _, x in durs)
    label = {1: "24 часа", 7: "7 дней", 30: "30 дней"}.get(days, f"{days} дн.")
    lines = [f"<b>📊 Отключения — {e(d['name'])}</b>", f"<i>за {label}</i>", ""]
    if not rows:
        lines.append("✅ Отключений не было.")
    else:
        lines.append(f"🔴 Отключений: <b>{len(rows)}</b>")
        lines.append(f"⏱ Всего без света: <b>{e(fmt_duration(total))}</b>")
        lines.append(f"📏 Самое долгое: <b>{e(fmt_duration(max(x for _, x in durs)))}</b>")
        lines.append(f"≈ В среднем: <b>{e(fmt_duration(total / len(rows)))}</b>")
        lines.append(f"💡 Свет был: <b>{max(0.0, 100 - total * 100 / window):.1f}%</b> времени")
        lines.append("")
        lines.append("<b>Последние:</b>")
        for r, dur in durs[:8]:
            soc = ""
            if r["start_soc"] is not None:
                soc = f" · 🔋{r['start_soc']:.0f}%"
                if r["end_soc"] is not None:
                    soc += f"→{r['end_soc']:.0f}%"
            if r["end_ts"]:
                lines.append(f"• {fmt_dt(r['start_ts'])} → {fmt_dt(r['end_ts'])} · {e(fmt_duration(dur))}{soc}")
            else:
                lines.append(f"• {fmt_dt(r['start_ts'])} → <b>идёт сейчас</b> · {e(fmt_duration(dur))}{soc}")
    if watch_from > since:
        lines.append(f"\n<i>наблюдение ведётся с {fmt_dt(watch_from)}</i>")

    def pb(n: int, t: str) -> InlineKeyboardButton:
        return b(("✅ " if n == days else "") + t, f"dst:{d['id']}:{n}", "success" if n == days else None)

    kb = [[pb(1, "24 ч"), pb(7, "7 дн"), pb(30, "30 дн")],
          [b("‹ Устройство", f"d:{d['id']}"), HOME_BTN]]
    return "\n".join(lines), M(kb)


SET_DEFAULTS = {"set_low": 1, "set_levels": "30,15,10", "set_full": 1, "set_over": 1,
                "set_over_pct": 90, "set_over_w": 0, "set_link": 1, "set_link_min": 4, "set_fc": 1, "set_anom": 1}
SET_BOOL = ("set_low", "set_full", "set_over", "set_link", "set_fc", "set_anom")
SET_CHOICES = {"set_over_pct": (80, 90, 95), "set_link_min": (2, 4, 10, 30)}
SET_INPUT = {
    "set_levels": "Отправьте пороги заряда в % через запятую, например: <code>50,30,15,10</code>",
    "set_over_w": "Отправьте предел мощности в Вт (например <code>2200</code>) или <code>0</code> — брать у батареи.",
}


def settings_screen(d: Any) -> tuple[str, M]:
    onoff = lambda v: "✅ вкл" if v else "⬜ выкл"
    lv = cfg_levels(d)
    lim = float(d["set_over_w"] or 0)
    lines = [
        f"<b>🔔 Настройки алертов — {e(d['name'])}</b>\n",
        f"🪫 Низкий заряд: <b>{onoff(d['set_low'])}</b> · пороги: <b>{', '.join(f'{x}%' for x in lv) or '—'}</b>",
        f"🔋 «Заряжена до 100%»: <b>{onoff(d['set_full'])}</b>",
        f"⚠️ Перегрузка: <b>{onoff(d['set_over'])}</b> · от <b>{int(d['set_over_pct'])}%</b> предела · "
        f"предел: <b>{f'{lim:.0f} Вт' if lim > 0 else f'авто ({DEFAULT_MAX_W:.0f} Вт, если батарея не сообщит)'}</b>",
        f"📵 Потеря связи: <b>{onoff(d['set_link'])}</b> · через <b>{int(d['set_link_min'])} мин</b> молчания",
        f"⏳ Прогноз времени работы: <b>{onoff(d['set_fc'])}</b>",
        f"🔎 Детектор аномалий: <b>{onoff(d['set_anom'])}</b>",
    ]
    i = d["id"]
    def tg(key, label):
        return b(("✅ " if d[key] else "⬜ ") + label, f"dsx:{i}:{key}", "success" if d[key] else None)
    def ch(key, vals, fmt):
        cur = int(d[key])
        return [b(("✅ " if cur == v else "") + fmt(v), f"dsv:{i}:{key}:{v}", "success" if cur == v else None) for v in vals]
    kb = [
        [tg("set_low", "Низкий заряд"), b("✏️ Пороги", f"dsi:{i}:set_levels")],
        [tg("set_full", "Заряжена до 100%")],
        [tg("set_over", "Перегрузка"), b("✏️ Предел Вт", f"dsi:{i}:set_over_w")],
        ch("set_over_pct", SET_CHOICES["set_over_pct"], lambda v: f"от {v}%"),
        [tg("set_link", "Потеря связи")],
        ch("set_link_min", SET_CHOICES["set_link_min"], lambda v: f"{v} мин"),
        [tg("set_fc", "Прогноз"), tg("set_anom", "Аномалии")],
        [b("↩️ По умолчанию", f"dsr:{i}"), b("‹ Устройство", f"d:{i}"), HOME_BTN],
    ]
    return "\n".join(lines), M(kb)


def share_screen(d: Any) -> tuple[str, M]:
    i = d["id"]
    cur = db.shares(i)
    have = {r["viewer_id"] for r in cur}
    lines = [f"<b>👥 Доступ к устройству — {e(d['name'])}</b>", "",
             "Люди из списка смогут <b>смотреть</b> устройство (в боте и в приложении) и получать уведомления "
             "об отключении света. Менять настройки, удалять и отправлять тест они не смогут.", ""]
    kb = []
    if cur:
        lines.append("<b>Сейчас смотрят:</b>")
        for r in cur:
            nm = r["name"] or r["username"] or str(r["viewer_id"])
            lines.append(f"• {e(nm)} · <code>{r['viewer_id']}</code> · " + ("🔔 с уведомлениями" if r["alerts"] else "🔕 без уведомлений"))
            kb.append([b(f"{'🔔' if r['alerts'] else '🔕'} {nm}"[:30], f"dsn:{i}:{r['viewer_id']}"),
                       b("❌ Закрыть доступ", f"dsd:{i}:{r['viewer_id']}", "danger")])
        lines.append("")
    cands = [u for u in list(db.users("allowed")) + list(db.users("admin"))
             if u["id"] != d["user_id"] and u["id"] not in have]
    if cands:
        lines.append("<b>Дать доступ:</b>")
        for u in cands[:20]:
            kb.append([b(f"➕ {u['name'] or u['username'] or u['id']}"[:40], f"dsa:{i}:{u['id']}", "success")])
    else:
        lines.append("<i>Других пользователей бота нет. Человек должен написать боту /start, "
                     "а администратор — открыть ему доступ в «🛡 Админ». Потом он появится здесь.</i>")
    kb.append([b("‹ Устройство", f"d:{i}"), HOME_BTN])
    return "\n".join(lines), M(kb)


def fields_screen(d: Any) -> tuple[str, M]:
    raw = parse_cache(d)
    hidden = hidden_set(d)
    lines = [
        f"<b>⚙️ Видимость полей — {e(d['name'])}</b>\n",
        "Нажмите на поле, чтобы скрыть или показать его на экране устройства:",
    ]
    kb = []
    ordered_keys = [k for k in FIELD_LABELS if k in raw]
    for k in ordered_keys:
        on = k not in hidden
        label, _ = FIELD_LABELS.get(k, (k, ""))
        kb.append([b(("✅ " if on else "⬜ ") + label[:42], f"dft:{d['id']}:{k}", "success" if on else None)])
    kb.append([b("Показать все", f"dfa:{d['id']}:on"), b("Скрыть все", f"dfa:{d['id']}:off")])
    kb.append([b("‹ Устройство", f"d:{d['id']}"), HOME_BTN])
    return "\n".join(lines), M(kb)


def admin_screen() -> tuple[str, M]:
    pend = db.users("pending")
    allow = db.users("allowed")
    admins = db.users("admin")
    lines = ["<b>🛡 Админ-панель</b>", ""]
    kb = []
    if pend:
        lines.append("<b>Ожидают доступа:</b>")
        for u in pend:
            lines.append(f"• {e(u['name'] or '')} @{e(u['username'] or '-')} · <code>{u['id']}</code>")
            kb.append(
                [
                    b(f"✅ {u['name'] or u['id']}"[:36], f"adm:ok:{u['id']}", "success"),
                    b("⛔", f"adm:no:{u['id']}", "danger"),
                ]
            )
        lines.append("")
    lines.append(f"<b>Админы ({len(admins)}) и пользователи ({len(allow)}):</b>")
    for u in list(admins) + list(allow):
        role_tag = "👑" if u["status"] == "admin" else "👤"
        lines.append(
            f"{role_tag} {e(u['name'] or '')} @{e(u['username'] or '-')} · <code>{u['id']}</code> "
            f"· устр: {len(db.devices(user_id=u['id']))}"
        )
        if u["status"] != "admin":
            kb.append([b(f"🗑 Удалить {u['name'] or u['id']}"[:40], f"adm:rm:{u['id']}")])
    kb.append([b("➕ Добавить по ID", "adm:add", "primary"), b("🔄", "adm")])
    kb.append([HOME_BTN])
    return "\n".join(lines), M(kb)


# ------------------------------------------------------------------ Access Gate & Commands
async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    u = update.effective_user
    if not u:
        return False
    row = db.upsert_user(u.id, u.username, u.full_name, "admin" if (ADMIN and u.id == ADMIN) else None)
    if row["status"] in ("admin", "allowed"):
        return True
    if row["status"] == "denied":
        return False
    for adm in db.admins():
        await notify(
            ctx,
            adm["id"],
            f"🆕 <b>Запрос доступа к боту</b>\n{e(u.full_name)} @{e(u.username or '-')} · <code>{u.id}</code>",
            M([[b("✅ Разрешить", f"adm:ok:{u.id}", "success"), b("⛔ Отклонить", f"adm:no:{u.id}", "danger")]]),
        )
    if update.effective_message:
        await update.effective_message.reply_text(
            "🔒 <b>Доступ по приглашению</b>\nЗапрос отправлен администратору.",
            parse_mode=H,
        )
    return False


async def open_home(ctx: ContextTypes.DEFAULT_TYPE, uid: int, mid: int | None = None) -> int:
    ctx.user_data.pop("flow", None)
    text, kb = home_screen(uid)
    return await show(ctx, uid, mid, text, kb)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, ctx):
        return
    await drop(update.message)
    await open_home(ctx, update.effective_user.id)


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, ctx):
        return
    await drop(update.message)
    await start_add_flow(ctx, update.effective_user.id, None)


# ------------------------------------------------------------------ Add Account Flow
async def start_add_flow(ctx: ContextTypes.DEFAULT_TYPE, uid: int, mid: int | None) -> None:
    fl = {"step": "email", "base_url": marstek.DEFAULT_BASE_URL, "region_key": "eu", "mid": mid}
    ctx.user_data["flow"] = fl
    fl["mid"] = await render_add_email_step(ctx, uid, fl)


async def render_add_email_step(ctx: ContextTypes.DEFAULT_TYPE, uid: int, fl: dict[str, Any]) -> int:
    rk = fl.get("region_key", "eu")
    reg_btns = [
        b(
            ("✅ " if rk == k else "") + title,
            f"reg:{k}",
            "success" if rk == k else None,
        )
        for k, (title, _) in REGIONS.items()
    ]
    text = (
        "<b>➕ Подключение аккаунта Marstek Cloud</b>\n\n"
        f"Сервер: <code>{e(fl['base_url'])}</code>\n\n"
        "Отправьте <b>Email</b> от вашего аккаунта приложения Marstek:"
    )
    kb = M([[btn] for btn in reg_btns] + [[HOME_BTN]])
    return await show(ctx, uid, fl.get("mid"), text, kb)


async def finish_login_flow(ctx: ContextTypes.DEFAULT_TYPE, uid: int, fl: dict[str, Any], pwd: str) -> None:
    mid = fl.get("mid")
    email = fl["email"]
    base_url = fl["base_url"]
    await show(ctx, uid, mid, f"⏳ Вхожу в Marstek Cloud (<code>{e(email)}</code>)…")

    client = marstek.MarstekClient(email=email, password_or_md5=pwd, base_url=base_url)
    try:
        await asyncio.to_thread(client.login)
        dev_list = await asyncio.to_thread(client.get_devices)
    except Exception as ex:
        await show(
            ctx,
            uid,
            mid,
            f"⚠️ <b>Ошибка входа:</b>\n<code>{e(str(ex))[:300]}</code>\n\nПроверьте email/пароль и попробуйте снова.",
            M([[b("🔁 Попробовать снова", "add", "primary"), HOME_BTN]]),
        )
        return

    aid = db.upsert_account(uid, email, client.pwd_md5, client.token, base_url=base_url)
    rows = db.sync_account_devices(aid, uid, dev_list)
    now = int(time.time())
    for r in rows:
        on, _, _ = device_state(r)
        if on is not None and r["grid_was_on"] is None:
            db.set_device(r["id"], grid_was_on=int(on), last_change_ts=now)

    ctx.user_data.pop("flow", None)
    if len(rows) == 1:
        t, kb = device_screen(db.device(rows[0]["id"]))
        await show(ctx, uid, mid, "✅ <b>Аккаунт подключён!</b>\n\n" + t, kb)
    else:
        t, kb = home_screen(uid)
        await show(ctx, uid, mid, f"✅ <b>Аккаунт подключён! Найдено устройств: {len(rows)}</b>\n\n" + t, kb)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, ctx):
        return
    msg = update.message
    uid = update.effective_user.id
    q = (msg.text or "").strip() if msg else ""
    # Always delete user's text message immediately (especially password!)
    await drop(msg)

    fl = ctx.user_data.get("flow")
    if not fl:
        await open_home(ctx, uid)
        return

    step = fl.get("step")
    if step == "admin_id" and is_admin(uid):
        digits = re.sub(r"\D", "", q)
        if digits:
            target_id = int(digits)
            db.upsert_user(target_id, status="allowed")
            await notify(ctx, target_id, "✅ <b>Доступ открыт!</b>", M([[b("Открыть меню", "home", "primary")]]))
        ctx.user_data.pop("flow", None)
        t, kb = admin_screen()
        await show(ctx, uid, fl.get("mid"), t, kb)
        return

    if step == "setting":
        did, key = fl.get("did"), fl.get("key")
        d = db.device(did) if did else None
        if not d or d["user_id"] != uid:
            ctx.user_data.pop("flow", None)
            await open_home(ctx, uid)
            return
        err = apply_setting(d, key, q) if key in SET_INPUT else "?"
        if err:
            await show(ctx, uid, fl.get("mid"), "⚠️ " + err, M([[b("Отмена", f"dse:{did}")]]))
            return
        ctx.user_data.pop("flow", None)
        t, kb = settings_screen(db.device(did))
        await show(ctx, uid, fl.get("mid"), t, kb)
        return

    if step == "email":
        if "@" not in q or len(q) < 4:
            await show(
                ctx,
                uid,
                fl.get("mid"),
                "⚠️ Пожалуйста, введите корректный <b>Email</b> аккаунта Marstek:",
                M([[HOME_BTN]]),
            )
            return
        fl["email"] = q
        fl["step"] = "password"
        await show(
            ctx,
            uid,
            fl.get("mid"),
            f"Email: <code>{e(q)}</code>\nСервер: <code>{e(fl['base_url'])}</code>\n\n"
            "Теперь отправьте <b>пароль</b> (или его 32-символьный MD5-хеш).\n"
            "<i>Сообщение с паролем будет мгновенно удалено, а в базе сохранится только MD5-хеш.</i>",
            M([[b("‹ Изменить Email", "add"), HOME_BTN]]),
        )
        return

    if step == "password":
        await finish_login_flow(ctx, uid, fl, q)


# ------------------------------------------------------------------ Callback Router
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    parts = (q.data or "").split(":")
    kind = parts[0]

    if not allowed(uid):
        await q.answer("Нет доступа", show_alert=True)
        return

    mid = q.message.message_id if q.message else None

    if kind not in ("dlv", "dl+"):
        stop_live(ctx, uid, mid)

    if kind in ("dlv", "dl+", "dlx"):
        did = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        d = db.device(did)
        if not can_view(d, uid):
            await q.answer("Устройство не найдено", show_alert=True)
            await open_home(ctx, uid, mid)
            return
        if kind == "dlv":
            await q.answer("📡 Live включён")
            await start_live(ctx, uid, mid, d)
        elif kind == "dl+":
            sess = live_sessions(ctx).get((uid, mid))
            if sess:
                sess["until"] = time.time() + LIVE_MINUTES * 60
            await q.answer(f"Продлено ещё на {LIVE_MINUTES} мин")
        else:
            await q.answer("Live остановлен")
            t, kb = device_screen(d, uid)
            await show(ctx, uid, mid, t, kb)
        return

    if kind == "home":
        await q.answer()
        await open_home(ctx, uid, mid)
        return

    if kind == "add":
        await q.answer()
        await start_add_flow(ctx, uid, mid)
        return

    if kind == "reg":
        await q.answer()
        fl = ctx.user_data.get("flow")
        if not fl or fl.get("step") != "email":
            await start_add_flow(ctx, uid, mid)
            return
        rk = parts[1]
        if rk in REGIONS:
            fl["region_key"] = rk
            fl["base_url"] = REGIONS[rk][1]
        fl["mid"] = mid
        await render_add_email_step(ctx, uid, fl)
        return

    if kind == "poll_now":
        accs = db.accounts(user_id=uid)
        if not accs:
            await q.answer("Сначала добавьте аккаунт", show_alert=True)
            return
        await q.answer("Обновляю данные из облака…")
        for a in accs:
            try:
                await refresh_account(a)
            except Exception as ex:
                db.set_account(a["id"], last_err=str(ex))
        await open_home(ctx, uid, mid)
        return

    if kind in ("accs", "accr", "accdl"):
        if kind == "accr" and len(parts) > 1:
            acc = db.account(int(parts[1]))
            if acc and acc["user_id"] == uid:
                try:
                    await refresh_account(acc)
                    await q.answer("Аккаунт обновлён")
                except Exception as ex:
                    db.set_account(acc["id"], last_err=str(ex))
                    await q.answer(f"Ошибка: {str(ex)[:150]}", show_alert=True)
        elif kind == "accdl" and len(parts) > 1:
            acc = db.account(int(parts[1]))
            if acc and acc["user_id"] == uid:
                db.del_account(acc["id"])
                await q.answer("Аккаунт удалён")
        else:
            await q.answer()
        t, kb = accounts_screen(uid)
        await show(ctx, uid, mid, t, kb)
        return

    if kind == "dvn":
        did = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        d = db.device(did)
        if not d or d["user_id"] == uid or not db.is_shared(did, uid):
            await q.answer("Нет доступа", show_alert=True)
            await open_home(ctx, uid, mid)
            return
        db.share_toggle_alerts(did, uid)
        await q.answer("Готово")
        t, kb = device_screen(d, uid)
        await show(ctx, uid, mid, t, kb)
        return

    if kind in ("dsh", "dsa", "dsd", "dsn"):
        did = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        d = db.device(did)
        if not d or d["user_id"] != uid:
            await q.answer("Только владелец может делиться устройством", show_alert=True)
            await open_home(ctx, uid, mid)
            return
        vid = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
        if kind == "dsa" and vid and vid != uid and allowed(vid):
            db.share_add(did, vid)
            await q.answer("Доступ открыт")
            await notify(ctx, vid, f"👁 <b>{e(q.from_user.full_name)}</b> открыл(а) вам просмотр устройства "
                                   f"<b>{e(d['name'])}</b>.",
                         M([[b("🔌 Открыть", f"d:{did}", "primary"), HOME_BTN]]))
        elif kind == "dsd" and vid:
            db.share_del(did, vid)
            await q.answer("Доступ закрыт")
        elif kind == "dsn" and vid:
            db.share_toggle_alerts(did, vid)
            await q.answer()
        else:
            await q.answer()
        t, kb = share_screen(d)
        await show(ctx, uid, mid, t, kb)
        return

    if kind in ("dse", "dsx", "dsv", "dsi", "dsr"):
        did = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        d = db.device(did)
        if not d or d["user_id"] != uid:
            await q.answer("Устройство не найдено", show_alert=True)
            await open_home(ctx, uid, mid)
            return
        key = parts[2] if len(parts) > 2 else ""
        if kind == "dsx" and key in SET_BOOL:
            db.set_device(did, **{key: 0 if d[key] else 1})
        elif kind == "dsv" and key in SET_CHOICES and len(parts) > 3 and parts[3].isdigit()                 and int(parts[3]) in SET_CHOICES[key]:
            db.set_device(did, **{key: int(parts[3])})
        elif kind == "dsr":
            db.set_device(did, soc_level=None, **SET_DEFAULTS)
            await q.answer("Настройки сброшены")
        elif kind == "dsi" and key in SET_INPUT:
            ctx.user_data["flow"] = {"step": "setting", "mid": mid, "did": did, "key": key}
            await q.answer()
            await show(ctx, uid, mid, "✏️ " + SET_INPUT[key], M([[b("Отмена", f"dse:{did}")]]))
            return
        if kind not in ("dsr",):
            await q.answer()
        ctx.user_data.pop("flow", None)
        t, kb = settings_screen(db.device(did))
        await show(ctx, uid, mid, t, kb)
        return

    if kind in ("d", "dr", "dt", "dm", "dc", "dmu", "df", "dft", "dfa", "ddl", "ddly", "dst"):
        did = int(parts[1]) if len(parts) > 1 else 0
        d = db.device(did)
        if not d or not (d["user_id"] == uid or (kind in ("d", "dr", "dst") and db.is_shared(did, uid))):
            await q.answer("Устройство не найдено", show_alert=True)
            await open_home(ctx, uid, mid)
            return

        if kind == "dr":
            acc = db.account(d["account_id"])
            if acc:
                try:
                    await refresh_account(acc)
                    d = db.device(did)
                    await q.answer("Данные обновлены")
                except Exception as ex:
                    db.set_account(acc["id"], last_err=str(ex))
                    await q.answer(f"Ошибка облака: {str(ex)[:140]}", show_alert=True)
                    return
        elif kind == "dt":
            on, reason, raw = device_state(d)
            alert_text = build_transition_alert(d, True if on is None else bool(on), reason, raw, d["last_change_ts"])
            await notify(
                ctx,
                uid,
                "🧪 <i>Тестовое уведомление:</i>\n\n" + alert_text,
                M([[b("🔌 Открыть устройство", f"d:{d['id']}", "primary")]]),
            )
            await q.answer("Тестовое уведомление отправлено!")
            return
        elif kind == "dm":
            new_mode = parts[2]
            if new_mode in MODE_META:
                db.set_device(did, mode=new_mode)
                d = db.device(did)
                on, _, _ = device_state(d)
                if on is not None:
                    db.set_device(did, grid_was_on=int(on))
                d = db.device(did)
            await q.answer(f"Режим: {MODE_META.get(new_mode, (new_mode,))[0]}")
        elif kind == "dc":
            cap = float(parts[2])
            db.set_device(did, capacity_kwh=cap)
            d = db.device(did)
            await q.answer(f"Ёмкость: {cap} кВт·ч")
        elif kind == "dmu":
            db.set_device(did, muted=0 if d["muted"] else 1)
            d = db.device(did)
            await q.answer("Уведомления выключены" if d["muted"] else "Уведомления включены")
        elif kind == "dst":
            await q.answer()
            t, kb = stats_screen(d, int(parts[2]) if len(parts) > 2 else 7)
            await show(ctx, uid, mid, t, kb)
            return
        elif kind == "df":
            await q.answer()
            t, kb = fields_screen(d)
            await show(ctx, uid, mid, t, kb)
            return
        elif kind == "dft":
            fkey = ":".join(parts[2:])
            hset = hidden_set(d)
            if fkey in hset:
                hset.discard(fkey)
            else:
                hset.add(fkey)
            db.set_device(did, hidden_fields=json.dumps(sorted(hset)))
            await q.answer()
            t, kb = fields_screen(db.device(did))
            await show(ctx, uid, mid, t, kb)
            return
        elif kind == "dfa":
            turn_on = parts[2] == "on"
            raw = parse_cache(d)
            hset = set() if turn_on else set(raw.keys())
            db.set_device(did, hidden_fields=json.dumps(sorted(hset)))
            await q.answer()
            t, kb = fields_screen(db.device(did))
            await show(ctx, uid, mid, t, kb)
            return
        elif kind == "ddl":
            await q.answer()
            await show(
                ctx,
                uid,
                mid,
                f"🗑 Удалить устройство <b>{e(d['name'])}</b>?",
                M([[b("Да, удалить", f"ddly:{d['id']}", "danger"), b("Отмена", f"d:{d['id']}")]]),
            )
            return
        elif kind == "ddly":
            db.del_device(did)
            await q.answer("Удалено")
            await open_home(ctx, uid, mid)
            return
        else:
            await q.answer()

        t, kb = device_screen(db.device(did), uid)
        await show(ctx, uid, mid, t, kb)
        return

    if kind == "adm":
        if not is_admin(uid):
            await q.answer("Только для админа", show_alert=True)
            return
        act = parts[1] if len(parts) > 1 else "list"
        if act in ("ok", "no", "rm") and len(parts) > 2:
            target = int(parts[2])
            if act == "ok":
                db.upsert_user(target, status="allowed")
                await notify(ctx, target, "✅ <b>Доступ открыт!</b>", M([[b("Открыть меню", "home", "primary")]]))
            elif act == "no":
                db.upsert_user(target, status="denied")
            elif act == "rm":
                db.delete_user(target)
        elif act == "add":
            ctx.user_data["flow"] = {"step": "admin_id", "mid": mid}
            await q.answer()
            await show(
                ctx,
                uid,
                mid,
                "<b>➕ Добавить пользователя</b>\n\nОтправьте Telegram ID числом:",
                M([[b("‹ Назад", "adm")]]),
            )
            return
        await q.answer()
        t, kb = admin_screen()
        await show(ctx, uid, mid, t, kb)


# ------------------------------------------------------------------ Background Poll Job
async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    accs = db.accounts()
    if not accs:
        return

    for acc in accs:
        if not allowed(acc["user_id"]):
            continue
        try:
            await refresh_account(acc)
        except Exception as ex:
            log.warning("poll account %s (%s) failed: %s", acc["id"], acc["email"], ex)
            db.set_account(acc["id"], last_err=str(ex))
            continue

        now = int(time.time())
        for d in db.devices(account_id=acc["id"]):
            try:
                await process_device(ctx, d, now)
            except Exception as ex:
                log.warning("device %s processing failed: %s", d["devid"], ex)


async def process_device(ctx: ContextTypes.DEFAULT_TYPE, d: Any, now: int) -> None:
    grid_on, reason, raw = device_state(d)
    soc = marstek.to_float(raw.get("soc"))
    muted = bool(d["muted"])
    open_kb = M([[b("🔌 Открыть устройство", f"d:{d['id']}", "primary"), HOME_BTN]])

    # ---- связь с батареей (MQTT молчит, хотя бот подключён)
    lost = link_lost_sec(d)
    if lost and d["set_link"] and not d["alert_link"]:
        db.set_device(d["id"], alert_link=1)
        if not muted:
            await notify_dev(ctx, d,
                f"📵 <b>Связь с батареей потеряна</b>\n🔌 <b>{e(d['name'])}</b>\n"
                f"От неё нет данных уже <b>{e(fmt_duration(lost))}</b>.\n"
                "Проверьте питание батареи и Wi-Fi роутер. Состояние сети сейчас определить нельзя.",
                open_kb,
            )
    elif not lost and d["alert_link"] and raw.get("_live"):
        db.set_device(d["id"], alert_link=0)
        if not muted:
            await notify_dev(ctx, d,
                         f"📶 <b>Связь с батареей восстановлена</b>\n🔌 <b>{e(d['name'])}</b>", open_kb)

    if grid_on is None:
        return
    was = d["grid_was_on"]
    if was is None:
        db.set_device(d["id"], grid_was_on=int(grid_on), last_change_ts=now)
        if not grid_on:
            db.start_outage(d["id"], now, soc)
        return

    if bool(was) != grid_on:
        prev_ts = d["last_change_ts"]
        db.set_device(d["id"], grid_was_on=int(grid_on), last_change_ts=now)
        # журнал отключений + сброс флагов алертов
        if grid_on:
            db.end_outage(d["id"], now, soc, started=prev_ts)
            db.set_device(d["id"], soc_level=None, alert_over=0,
                          full_pending=1 if (soc is not None and soc < 99.5) else 0)
        else:
            db.start_outage(d["id"], now, soc)
            db.set_device(d["id"], soc_level=None, alert_over=0, full_pending=0)
        log.info("Grid transition on device %s (%s): %s -> %s (%s)",
                 d["devid"], d["name"], bool(was), grid_on, reason)
        if not muted:
            alert_msg = build_transition_alert(db.device(d["id"]), grid_on, reason, raw, prev_ts)
            await notify_dev(ctx, d, alert_msg, open_kb)
        return

    await check_alerts(ctx, db.device(d["id"]), grid_on, raw, soc)


async def check_alerts(ctx: ContextTypes.DEFAULT_TYPE, d: Any, grid_on: bool, raw: dict[str, Any],
                       soc: float | None) -> None:
    """Предупреждения без смены состояния сети: низкий заряд, перегрузка, батарея заряжена."""
    muted = bool(d["muted"])
    kb = M([[b("🔌 Открыть устройство", f"d:{d['id']}", "primary"), HOME_BTN]])

    if not grid_on:
        # --- низкий заряд (30 / 15 / 10 %): по одному сообщению на каждый порог
        if soc is not None and d["set_low"]:
            hit = [lv for lv in cfg_levels(d) if soc <= lv]
            if hit:
                lv = min(hit)
                prev = d["soc_level"]
                if prev is None or lv < prev:
                    db.set_device(d["id"], soc_level=lv)
                    if not muted:
                        icon = "🟡" if lv >= 25 else ("🟠" if lv >= 12 else "🔴")
                        cap = float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH)
                        txt = [f"{icon} 🪫 <b>Батарея разряжается: {soc:.0f}%</b>",
                               f"🔌 <b>{e(d['name'])}</b> · осталось {soc / 100 * cap:.2f} кВт·ч",
                               "Сети всё ещё нет."]
                        ft = forecast_text(d, raw)
                        if ft:
                            txt.append(f"⏳ Хватит ещё: {ft}")
                        await notify_dev(ctx, d, "\n".join(txt), kb)

        # --- перегрузка: отдача близка к пределу инвертора
        if d["set_over"] and raw.get("_live") and not power_missing(raw):
            limit = over_limit(d, raw)
            on_pct = int(d["set_over_pct"] or 90) / 100.0
            off_pct = max(0.3, on_pct - 0.15)
            p = marstek.to_float(raw.get("discharge")) or 0.0
            if p >= limit * on_pct and not d["alert_over"]:
                db.set_device(d["id"], alert_over=1)
                if not muted:
                    await notify_dev(ctx, d,
                        f"⚠️ <b>ПЕРЕГРУЗКА БАТАРЕИ</b>\n🔌 <b>{e(d['name'])}</b>\n"
                        f"Нагрузка <b>{p:.0f} Вт</b> при пределе {limit:.0f} Вт ({p / limit * 100:.0f}%).\n"
                        "Отключите мощные приборы, иначе инвертор сам отключит выход.",
                        kb,
                    )
            elif p < limit * off_pct and d["alert_over"]:
                db.set_device(d["id"], alert_over=0)
                if not muted:
                    await notify_dev(ctx, d,
                                 f"✅ Нагрузка в норме: {p:.0f} Вт (предел {limit:.0f} Вт)\n"
                                 f"🔌 <b>{e(d['name'])}</b>", kb)
    else:
        # --- после возвращения сети: батарея дозарядилась
        if d["set_full"] and d["full_pending"] and soc is not None and soc >= 99.5:
            db.set_device(d["id"], full_pending=0)
            if not muted:
                await notify_dev(ctx, d,
                             f"🔋 <b>Батарея заряжена до 100%</b>\n🔌 <b>{e(d['name'])}</b> — "
                             "готова к следующему отключению.", kb)


# ------------------------------------------------------------------ LIVE-режим (сообщение обновляется на месте)
LIVE_MINUTES = 10        # на сколько минут включается live (кнопкой можно продлить)
LIVE_TICK = 3            # период обновления сообщения, с
ANOM_COOLDOWN = 6 * 3600  # не повторять одну и ту же аномалию чаще, чем раз в 6 часов
SPARK = "▁▂▃▄▅▆▇█"


def live_sessions(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.application.bot_data.setdefault("live", {})


def stop_live(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, mid: int | None) -> None:
    sess = live_sessions(ctx).pop((chat_id, mid), None)
    for job in ctx.application.job_queue.get_jobs_by_name(f"live:{chat_id}:{mid}"):
        job.schedule_removal()
    if sess is not None:
        log.info("live stopped chat=%s mid=%s", chat_id, mid)


def spark(vals: list[float]) -> str:
    if not vals:
        return ""
    top = max(100.0, max(abs(v) for v in vals))
    return "".join(SPARK[min(7, int(abs(v) / top * 7.999))] for v in vals)


def bar(pct: float, n: int = 10) -> str:
    k = max(0, min(n, round(pct / 100.0 * n)))
    return "█" * k + "░" * (n - k)


def live_text(d: Any, sess: dict) -> str:
    on, reason, raw = device_state(d)
    now = time.time()
    left = max(0, int((sess["until"] - now) / 60) + 1)
    head = f"{state_icon(on)} <b>{e(d['name'])}</b> · 📡 LIVE"
    if not raw.get("_live"):
        return head + "\n\n⏳ Жду живой кадр от батареи по MQTT…\n" + f"<i>осталось {left} мин</i>"
    last = LIVE.latest(str(d["devid"])) if LIVE else None
    age = int(now - last[0]) if last else 0
    lines = [head]
    if on is False:
        lines.append("🔴 <b>Сети нет — работа от батареи</b>")
    soc = marstek.to_float(raw.get("soc"))
    if soc is not None:
        cap = float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH)
        lines.append(f"🔋 <b>{soc:.0f}%</b> {bar(soc)} · {soc / 100 * cap:.2f} кВт·ч")
    lines.append(f"⚙️ {e(str(raw.get('_work_status_text') or '—'))}")
    lines.append("")
    chg = marstek.to_float(raw.get("charge")) or 0.0
    dis = marstek.to_float(raw.get("discharge")) or 0.0
    if chg >= 30:
        lines.append(f"⚡ Заряжается: <b>{chg:.0f} Вт</b>")
    elif dis >= 30:
        lines.append(f"🪫 Отдаёт: <b>{dis:.0f} Вт</b>")
    else:
        lines.append(f"⏸ Батарея в простое ({max(chg, dis):.0f} Вт)")
    g = marstek.to_float(raw.get("grid"))
    if g is not None:
        if abs(g) < 5:
            lines.append("🔌 Сеть: <b>0 Вт</b>")
        elif g < 0:
            lines.append(f"🔌 Из сети берём: <b>{-g:.0f} Вт</b> ⬇️")
        else:
            lines.append(f"🔌 В сеть отдаём: <b>{g:.0f} Вт</b> ⬆️")
    eps = marstek.to_float(raw.get("eps"))
    if eps is not None:
        lines.append(f"🏠 Выход EPS (резерв): <b>{eps:.0f} Вт</b>")
    hist = sess["hist"]
    if len(hist) >= 3:
        lines.append(f"<code>{spark(hist)}</code> <i>мощность батареи</i>")
    if on is False:
        ft = forecast_text(d, raw)
        if ft:
            lines.append(f"⏳ Хватит ещё: {ft}")
    dc, dd = raw.get("day_charge_kwh"), raw.get("day_discharge_kwh")
    if dc is not None or dd is not None:
        lines.append(f"📈 Сегодня: +{(dc or 0):.2f} / −{(dd or 0):.2f} кВт·ч")
    if raw.get("wifi_dbm") is not None:
        lines.append(f"📶 Wi-Fi {raw['wifi_dbm']:.0f} dBm")
    stamp = datetime.fromtimestamp(last[0], KYIV).strftime("%H:%M:%S") if last else "—"
    lines.append("")
    lines.append(f"<i>🕒 {stamp}" + (f" · ⚠️ данным {age} с" if age > 15 else "") + f" · осталось {left} мин</i>")
    return "\n".join(lines)


def live_kb(d: Any) -> M:
    i = d["id"]
    return M([[b("⏹ Стоп", f"dlx:{i}", "danger"), b(f"⏱ +{LIVE_MINUTES} мин", f"dl+:{i}")],
              [b("‹ Устройство", f"d:{i}"), HOME_BTN]])


async def start_live(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, mid: int | None, d: Any) -> None:
    if not mid:
        return
    stop_live(ctx, chat_id, mid)
    sess = {"did": d["id"], "chat": chat_id, "mid": mid, "until": time.time() + LIVE_MINUTES * 60,
            "hist": [], "last": None, "fails": 0, "skip_until": 0.0}
    live_sessions(ctx)[(chat_id, mid)] = sess
    ctx.application.job_queue.run_repeating(live_tick, interval=LIVE_TICK, first=0, data=sess,
                                            name=f"live:{chat_id}:{mid}")
    if LIVE:
        LIVE.fast_until = time.time() + 20
        LIVE.poll_now()


async def live_tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sess = ctx.job.data
    chat_id, mid = sess["chat"], sess["mid"]
    now = time.time()
    d = db.device(sess["did"])
    if not d or live_sessions(ctx).get((chat_id, mid)) is not sess:
        stop_live(ctx, chat_id, mid)
        return
    if now >= sess["until"]:
        stop_live(ctx, chat_id, mid)
        t, kb = device_screen(d, chat_id)
        await show(ctx, chat_id, mid, "⏹ <i>Live остановлен по таймеру.</i>\n\n" + t, kb)
        return
    if now < sess["skip_until"]:
        return
    if LIVE:
        LIVE.fast_until = now + 20
    _, _, raw = device_state(d)
    if raw.get("_live"):
        bp = marstek.to_float(raw.get("battery_power"))
        if bp is not None:
            sess["hist"].append(bp)
            del sess["hist"][:-30]
    text = live_text(d, sess)
    if text == sess["last"]:
        return
    try:
        await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=mid, parse_mode=H,
                                        reply_markup=live_kb(d), link_preview_options={"is_disabled": True})
        sess["last"] = text
        sess["fails"] = 0
    except BadRequest as ex:
        if "not modified" in str(ex).lower():
            sess["last"] = text
        else:  # сообщение удалено / нельзя редактировать
            log.info("live: edit failed (%s) — stop", ex)
            stop_live(ctx, chat_id, mid)
    except RetryAfter as ex:
        sess["skip_until"] = now + float(ex.retry_after) + 1
    except Exception as ex:
        sess["fails"] += 1
        log.warning("live edit failed: %s", ex)
        if sess["fails"] >= 5:
            stop_live(ctx, chat_id, mid)


# ------------------------------------------------------------------ история телеметрии + детектор аномалий
async def check_anomalies(ctx: ContextTypes.DEFAULT_TYPE, d: Any, now: int) -> None:
    samples = [dict(r) for r in db.samples(d["id"], now - 2700)]
    on, _, _ = device_state(d)
    cap = float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH)
    for a in anomaly.detect(samples, cap, on):
        key = f"anom:{d['id']}:{a['code']}"
        if now - float(db.kv_get(key, "0") or 0) < ANOM_COOLDOWN:
            continue
        db.kv_set(key, str(now))
        log.info("anomaly %s on device %s", a["code"], d["devid"])
        if not d["muted"]:
            await notify_dev(ctx, d, f"<b>{e(a['title'])}</b>\n🔌 {e(d['name'])}\n{e(a['text'])}",
                         M([[b("🔌 Открыть устройство", f"d:{d['id']}", "primary"), HOME_BTN]]))


async def sample_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в минуту кладёт последний живой MQTT-кадр в историю и прогоняет детектор аномалий."""
    if not LIVE:
        return
    now = int(time.time())
    for d in db.devices():
        try:
            last = LIVE.latest(str(d["devid"]))
            if not last or time.time() - last[0] > 120:
                continue
            f = last[1]
            lv = mqtt_live.decode(f)
            db.add_sample(d["id"], int(last[0]), lv["soc"], lv["battery_power"], lv["grid_power"],
                          lv["eps_power"], lv["wifi_dbm"], lv["work_status"], f.get("cel_s"))
            if d["set_anom"]:
                await check_anomalies(ctx, d, now)
        except Exception as ex:
            log.warning("sample_job device %s: %s", d["devid"], ex)
    if now % 3600 < 60:
        db.prune_samples(now - 14 * 86400)


# ------------------------------------------------------------------ Telegram Mini App (webapp.py + cloudflared)
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8765") or "8765")
WEBAPP_ON = os.environ.get("WEBAPP", "1").strip() not in ("0", "false", "no")
_tunnel: dict[str, Any] = {"proc": None, "runner": None, "task": None}


def web_touch_live() -> None:
    if LIVE:
        LIVE.fast_until = time.time() + 20


# ------------------------------------------------------------------ единый реестр настроек (бот и Mini App)
def _ch(vals, fmt):
    return [{"v": v, "t": fmt(v)} for v in vals]


SCHEMA = [
    {"group": "Уведомления", "key": "muted", "type": "bool", "label": "🔕 Выключить все пуши по устройству"},
    {"group": "Уведомления", "key": "set_low", "type": "bool", "label": "🪫 Предупреждать о низком заряде"},
    {"group": "Уведомления", "key": "set_levels", "type": "text", "label": "Пороги заряда, %",
     "hint": "через запятую, например 50,30,15,10"},
    {"group": "Уведомления", "key": "set_full", "type": "bool", "label": "🔋 «Батарея заряжена до 100%»"},
    {"group": "Уведомления", "key": "set_over", "type": "bool", "label": "⚠️ Перегрузка"},
    {"group": "Уведомления", "key": "set_over_pct", "type": "choice", "label": "Порог перегрузки",
     "choices": _ch(SET_CHOICES["set_over_pct"], lambda v: f"{v}%")},
    {"group": "Уведомления", "key": "set_over_w", "type": "number", "label": "Предел мощности, Вт",
     "hint": "0 — авто (берётся у батареи), иначе 300…10000"},
    {"group": "Уведомления", "key": "set_link", "type": "bool", "label": "📵 Потеря связи с батареей"},
    {"group": "Уведомления", "key": "set_link_min", "type": "choice", "label": "Связь потеряна через",
     "choices": _ch(SET_CHOICES["set_link_min"], lambda v: f"{v} мин")},
    {"group": "Уведомления", "key": "set_fc", "type": "bool", "label": "⏳ Прогноз времени работы"},
    {"group": "Уведомления", "key": "set_anom", "type": "bool", "label": "🔎 Детектор аномалий"},
    {"group": "Устройство", "key": "mode", "type": "choice", "label": "Режим детекции сети",
     "choices": [{"v": "auto", "t": "🔀 Авто"}, {"v": "online", "t": "📡 Online"}, {"v": "grid", "t": "⚡ Grid Вт"}]},
    {"group": "Устройство", "key": "capacity_kwh", "type": "choice", "label": "Ёмкость батареи",
     "choices": _ch(CAPACITIES, lambda v: f"{v} кВт·ч")},
]
SCHEMA_IDX = {x["key"]: x for x in SCHEMA}


def apply_setting(d: Any, key: str, value: Any) -> str | None:
    """Единая точка изменения настроек устройства (бот и приложение). None = ок, иначе текст ошибки."""
    did = d["id"]
    if key.startswith("field:"):
        fk = key[6:]
        if fk not in FIELD_LABELS or fk not in parse_cache(d):
            return "Нет такого поля"
        hs = hidden_set(d)
        if value:
            hs.discard(fk)
        else:
            hs.add(fk)
        db.set_device(did, hidden_fields=json.dumps(sorted(hs)))
        return None
    if key == "fields_all":
        db.set_device(did, hidden_fields=json.dumps(sorted(set() if value else set(parse_cache(d).keys()))))
        return None
    spec = SCHEMA_IDX.get(key)
    if not spec:
        return "Неизвестная настройка"
    t = spec["type"]
    if t == "bool":
        if value not in (0, 1, True, False):
            return "Нужно да/нет"
        db.set_device(did, **{key: 1 if value else 0})
        return None
    if t == "choice":
        hit = None
        for c in spec["choices"]:
            cv = c["v"]
            if isinstance(cv, str):
                if value == cv:
                    hit = cv
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and abs(float(value) - float(cv)) < 1e-6:
                hit = cv
        if hit is None:
            return "Недопустимое значение"
        db.set_device(did, **{key: hit})
        if key == "mode":
            on, _, _ = device_state(db.device(did))
            if on is not None:
                db.set_device(did, grid_was_on=int(on))
        return None
    if t == "text" and key == "set_levels":
        vals = sorted({int(x) for x in re.findall(r"\d+", str(value)) if 1 <= int(x) <= 99}, reverse=True)
        if not vals:
            return "Нужны числа от 1 до 99, например <code>50,30,15,10</code>"
        db.set_device(did, set_levels=",".join(map(str, vals)), soc_level=None)
        return None
    if t == "number" and key == "set_over_w":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            val = float(value)
        else:
            m = re.search(r"\d+(?:[.,]\d+)?", str(value))
            val = float(m.group(0).replace(",", ".")) if m else -1
        if val < 0 or (0 < val < 300) or val > 10000:
            return "Нужно число от 300 до 10000 Вт или <code>0</code> (авто)"
        db.set_device(did, set_over_w=val)
        return None
    return "Неизвестная настройка"


def reset_settings(d: Any) -> None:
    db.set_device(d["id"], soc_level=None, **SET_DEFAULTS)


def web_settings(d: Any, uid: int) -> dict[str, Any]:
    owner = d["user_id"] == uid
    out: dict[str, Any] = {"id": d["id"], "owner": owner, "is_admin": is_admin(uid)}
    if not owner:
        mine = next((r for r in db.shares(d["id"]) if r["viewer_id"] == uid), None)
        out["my_alerts"] = bool(mine and mine["alerts"])
        return out
    vals = {}
    for sp in SCHEMA:
        v = d[sp["key"]] if sp["key"] in d.keys() else None
        if sp["type"] == "bool":
            v = bool(v)
        elif sp["key"] == "set_levels":
            v = ",".join(map(str, cfg_levels(d)))
        elif sp["key"] == "capacity_kwh":
            v = float(v or 5.12)
        vals[sp["key"]] = v
    raw = parse_cache(d)
    hidden = hidden_set(d)
    out["schema"] = SCHEMA
    out["values"] = vals
    out["fields"] = [{"key": k, "label": FIELD_LABELS[k][0], "on": k not in hidden} for k in FIELD_LABELS if k in raw]
    have = set()
    out["shares"] = []
    for r in db.shares(d["id"]):
        have.add(r["viewer_id"])
        out["shares"].append({"id": r["viewer_id"], "name": r["name"] or r["username"] or str(r["viewer_id"]),
                              "alerts": bool(r["alerts"])})
    out["candidates"] = [{"id": u["id"], "name": u["name"] or u["username"] or str(u["id"])}
                         for u in list(db.users("allowed")) + list(db.users("admin"))
                         if u["id"] != d["user_id"] and u["id"] not in have][:40]
    return out


_last_refresh: dict[int, float] = {}


async def web_action(bot_ctx: Any, d: Any, uid: int, action: str) -> str | None:
    owner = d["user_id"] == uid
    if action == "refresh":
        if time.time() - _last_refresh.get(d["id"], 0) < 10:
            return "Подождите несколько секунд"
        _last_refresh[d["id"]] = time.time()
        acc = db.account(d["account_id"])
        if not acc:
            return "Аккаунт не найден"
        try:
            await refresh_account(acc)
        except Exception as ex:
            db.set_account(acc["id"], last_err=str(ex))
            return f"Ошибка облака: {str(ex)[:120]}"
        return None
    if not owner:
        return "Только владелец"
    if action == "test":
        on, reason, raw = device_state(d)
        txt = build_transition_alert(d, True if on is None else bool(on), reason, raw, d["last_change_ts"])
        await notify(bot_ctx, uid, "🧪 <i>Тестовое уведомление:</i>\n\n" + txt,
                     M([[b("🔌 Открыть устройство", f"d:{d['id']}", "primary")]]))
        return None
    if action == "reset":
        reset_settings(d)
        return None
    if action == "delete":
        db.del_device(d["id"])
        return None
    return "Неизвестное действие"


async def web_share(bot_ctx: Any, d: Any, uid: int, op: str, vid: int) -> str | None:
    if op == "my_alerts":
        if d["user_id"] == uid or not db.is_shared(d["id"], uid):
            return "Нет доступа"
        db.share_toggle_alerts(d["id"], uid)
        return None
    if d["user_id"] != uid:
        return "Только владелец может делиться устройством"
    if op == "add":
        if not vid or vid == uid or not allowed(vid):
            return "Пользователь недоступен"
        db.share_add(d["id"], vid)
        owner = db.user(uid)
        await notify(bot_ctx, vid, f"👁 <b>{e((owner['name'] or owner['username'] or str(uid)) if owner else str(uid))}</b> "
                                   f"открыл(а) вам просмотр устройства <b>{e(d['name'])}</b>.",
                     M([[b("🔌 Открыть", f"d:{d['id']}", "primary"), HOME_BTN]]))
        return None
    if op == "del" and vid:
        db.share_del(d["id"], vid)
        return None
    if op == "alerts" and vid and db.is_shared(d["id"], vid):
        db.share_toggle_alerts(d["id"], vid)
        return None
    return "Неизвестная операция"


def web_admin_list() -> dict[str, Any]:
    out = []
    for u in db.users():
        if u["status"] in ("pending", "allowed", "admin"):
            out.append({"id": u["id"], "name": u["name"] or u["username"] or str(u["id"]), "username": u["username"],
                        "status": u["status"], "devices": len(db.devices(user_id=u["id"]))})
    return {"users": out}


async def web_admin_op(bot_ctx: Any, actor: int, op: str, target: int) -> str | None:
    if not target:
        return "Нужен Telegram ID"
    if op in ("ok", "add"):
        db.upsert_user(target, status="allowed")
        await notify(bot_ctx, target, "✅ <b>Доступ открыт!</b>", M([[b("Открыть меню", "home", "primary")]]))
        return None
    if op == "no":
        if target == actor or (ADMIN and target == ADMIN):
            return "Себя отклонить нельзя"
        db.upsert_user(target, status="denied")
        return None
    if op == "rm":
        u = db.user(target)
        if target == actor or (ADMIN and target == ADMIN) or (u and u["status"] == "admin"):
            return "Админа удалить нельзя"
        db.delete_user(target)
        return None
    return "Неизвестная операция"


def web_fields(d: Any, raw: dict[str, Any]) -> list[dict[str, str]]:
    hidden = hidden_set(d)
    no_power = power_missing(raw)
    live = bool(raw.get("_live"))
    out = []
    for k in FIELD_LABELS:
        if k not in raw or k in hidden or (live and k in ("load", "pv")) or (no_power and k in POWER_KEYS):
            continue
        out.append({"label": FIELD_LABELS[k][0], "value": format_field_value(k, raw.get(k), d)})
    return out


def web_state(d: Any) -> dict[str, Any]:
    on, reason, raw = device_state(d)
    now = time.time()
    last = LIVE.latest(str(d["devid"])) if LIVE else None
    fc = runtime_forecast(d, raw) if on is False and d["set_fc"] else None
    anoms = []
    if d["set_anom"] and LIVE:
        sm = [dict(r) for r in db.samples(d["id"], int(now) - 2700)]
        anoms = anomaly.detect(sm, float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH), on)
    f = lambda k: marstek.to_float(raw.get(k))
    return {
        "id": d["id"], "name": d["name"], "type": d["type"], "on": on, "reason": reason,
        "owner_id": d["user_id"], "fields": web_fields(d, raw),
        "live": bool(raw.get("_live")),
        "frame_age": int(now - last[0]) if last else None,
        "link_lost": link_lost_sec(d) or None,
        "since": d["last_change_ts"] if on is False else None,
        "capacity": float(d["capacity_kwh"] or marstek.DEFAULT_CAPACITY_KWH),
        "soc": f("soc"), "charge": f("charge"), "discharge": f("discharge"),
        "battery_power": f("battery_power"), "grid": f("grid"), "eps": f("eps"),
        "day_charge": f("day_charge_kwh"), "day_discharge": f("day_discharge_kwh"),
        "wifi": f("wifi_dbm"), "work_text": raw.get("_work_status_text"), "error_code": raw.get("error_code"),
        "forecast": {"hours": fc[0], "power": fc[1], "until": fc[2]} if fc else None,
        "anomalies": [{"title": a["title"], "text": a["text"]} for a in anoms],
        "settings": {k: d[k] for k in ("set_low", "set_full", "set_over", "set_link", "set_fc", "set_anom",
                                       "set_over_pct", "set_link_min")},
    }


def web_outages(d: Any, days: int) -> dict[str, Any]:
    now = int(time.time())
    since = now - days * 86400
    watch_from = max(since, int(d["created"] or since))
    window = max(1, now - watch_from)
    items = []
    for r in db.outages(d["id"], since):
        dur = max(0, (r["end_ts"] or now) - max(r["start_ts"], since))
        items.append({"start": r["start_ts"], "end": r["end_ts"], "dur": dur,
                      "start_soc": r["start_soc"], "end_soc": r["end_soc"]})
    total = sum(i["dur"] for i in items)
    return {"count": len(items), "total": total, "longest": max((i["dur"] for i in items), default=0),
            "uptime_pct": max(0.0, 100 - total * 100 / window), "items": items[:30]}


async def _tunnel_loop(app: Application) -> None:
    """Держит быстрый туннель cloudflared; при смене адреса обновляет кнопку меню бота."""
    global WEBAPP_URL
    exe = BASE / "tools" / "cloudflared.exe"
    if not exe.exists():
        log.warning("cloudflared не найден (%s) — Mini App недоступно снаружи", exe)
        return
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{WEBAPP_PORT}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _tunnel["proc"] = proc
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line.decode("utf-8", "replace"))
                if m and m.group(0) != WEBAPP_URL:
                    WEBAPP_URL = m.group(0)
                    log.info("Mini App URL: %s", WEBAPP_URL)
                    try:
                        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("📱 Приложение", WebAppInfo(WEBAPP_URL)))
                    except Exception as ex:
                        log.warning("set_chat_menu_button failed: %s", ex)
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("tunnel error: %s", ex)
        WEBAPP_URL = ""
        log.warning("cloudflared завершился — перезапуск через 10 с")
        await asyncio.sleep(10)


async def start_webapp(app: Application) -> None:
    if not WEBAPP_ON:
        return
    webapp.ctx.token = TOKEN
    webapp.ctx.db = db
    webapp.ctx.allowed = allowed
    webapp.ctx.build_state = web_state
    webapp.ctx.build_outages = web_outages
    webapp.ctx.touch_live = web_touch_live
    shim = SimpleNamespace(bot=app.bot)
    webapp.ctx.build_settings = web_settings
    webapp.ctx.apply_setting = apply_setting
    webapp.ctx.action = lambda d, uid, a: web_action(shim, d, uid, a)
    webapp.ctx.share = lambda d, uid, op, vid: web_share(shim, d, uid, op, vid)
    webapp.ctx.is_admin = is_admin
    webapp.ctx.admin_list = web_admin_list
    webapp.ctx.admin_op = lambda actor, op, target: web_admin_op(shim, actor, op, target)
    try:
        _tunnel["runner"] = await webapp.start(WEBAPP_PORT)
    except OSError as ex:
        log.warning("Mini App: порт %s занят (%s) — отключено", WEBAPP_PORT, ex)
        return
    _tunnel["task"] = asyncio.create_task(_tunnel_loop(app))


async def post_shutdown(app: Application) -> None:
    if _tunnel["task"]:
        _tunnel["task"].cancel()
    p = _tunnel["proc"]
    if p and p.returncode is None:
        try:
            p.terminate()
        except Exception:
            pass
    if _tunnel["runner"]:
        await _tunnel["runner"].cleanup()


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", ctx.error, exc_info=ctx.error)


async def post_init(app: Application) -> None:
    if LIVE:
        LIVE.start()
        # зарегистрировать уже известные устройства из БД, не дожидаясь первого опроса облака
        for dv in db.devices():
            c = parse_cache(dv)
            LIVE.register(str(dv["devid"]), str(c.get("type") or dv["type"] or ""),
                          str(c.get("mac") or dv["mac"] or ""), str(c.get("salt") or ""))
        log.info("MQTT live telemetry enabled (%s:%s)", LIVE.host[:6] + "…", LIVE.port)
    else:
        log.warning("MQTT certs not found in %s — live watts disabled", CERT_DIR)
    await start_webapp(app)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "⚡ Главное меню"),
            BotCommand("add", "➕ Подключить аккаунт Marstek"),
        ]
    )


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            f"Укажите BOT_TOKEN в переменных окружения или в файле {BASE / '.env'}"
        )
    # Сеть до api.telegram.org нестабильна (запросы иногда висят 10-15 с) — щедрые таймауты
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30)
        .read_timeout(40)
        .write_timeout(40)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .build()
    )
    for cmd_name, fn in [
        ("start", cmd_start),
        ("menu", cmd_start),
        ("status", cmd_start),
        ("help", cmd_start),
        ("add", cmd_add),
    ]:
        app.add_handler(CommandHandler(cmd_name, fn))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(poll_job, interval=POLL_SEC, first=5, name="marstek_poll")
    app.job_queue.run_repeating(sample_job, interval=60, first=30, name="marstek_samples")
    log.info("Marstek Grid Alert Bot started (poll=%ss, admin=%s)", POLL_SEC, ADMIN or "auto-first-user")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
