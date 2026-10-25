"""
Marstek Cloud API client (based on DoctaShizzle/marstek_cloud & eu.hamedata.com EMS API).

Endpoints:
1. Login / Obtain Token:
   POST https://eu.hamedata.com/app/Solar/v2_get_device.php?pwd=<MD5_PASSWORD>&mailbox=<EMAIL>
2. Fetch Live Device Telemetry:
   GET  https://eu.hamedata.com/ems/api/v1/getDeviceList?token=<TOKEN>&mailbox=<EMAIL>
3. Fetch 5-Day Historical Data (10-min intervals):
   GET  https://eu.hamedata.com/ems/api/v2/getFiveDays?mailbox=<EMAIL>&token=<TOKEN>&devid=<DEVID>&date=YYYY-MM-DD
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests

DEFAULT_BASE_URL = "https://eu.hamedata.com"
API_LOGIN_PATH = "/app/Solar/v2_get_device.php"
API_DEVICES_PATH = "/ems/api/v1/getDeviceList"
API_FIVE_DAYS_PATH = "/ems/api/v2/getFiveDays"
API_ADD_DEVICE_PATH = "/app/Solar/v2_add_device.php"

USER_AGENT = "Dart/2.19 (dart:io)"
DEFAULT_CAPACITY_KWH = 5.12
TOKEN_INVALID_CODES = {-1, "-1", 8, "8", 401, "401", 403, "403"}

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def ensure_md5(password_or_md5: str) -> str:
    """Return lowercase MD5 hex digest; if already 32-hex, keep as-is."""
    s = (password_or_md5 or "").strip()
    if _MD5_RE.match(s):
        return s.lower()
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def to_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_report_time(val: Any) -> int | None:
    """Parse report_time (unix seconds, ms, or datetime string) into unix epoch seconds."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        ts = int(val)
        return ts // 1000 if ts > 10_000_000_000 else ts
    s = str(val).strip()
    if s.isdigit():
        ts = int(s)
        return ts // 1000 if ts > 10_000_000_000 else ts
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            pass
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def translate_error(code: Any, msg: Any) -> str:
    c = "" if code is None else str(code)
    m = "" if msg is None else str(msg)
    if c == "3" or "暂未注册" in m:
        return f"Аккаунт (email) не зарегистрирован (code=3: {m})"
    if c == "4" or "密码错误" in m:
        return f"Неверный пароль (code=4: {m})"
    if c == "8" or "unauthorized" in m.lower():
        return f"Токен недействителен или нет доступа (code=8: {m})"
    return f"code={c}: {m}" if c else m


class MarstekError(Exception):
    pass


class MarstekAuthError(MarstekError):
    pass


class MarstekClient:
    def __init__(
        self,
        email: str,
        password_or_md5: str,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 15,
    ):
        self.email = email.strip()
        self.pwd_md5 = ensure_md5(password_or_md5)
        self.token = token
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.bound_devices: list[dict[str, Any]] = []
        self.last_latency_ms: float | None = None
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )

    @property
    def login_url(self) -> str:
        return f"{self.base_url}{API_LOGIN_PATH}"

    @property
    def devices_url(self) -> str:
        return f"{self.base_url}{API_DEVICES_PATH}"

    @property
    def five_days_url(self) -> str:
        return f"{self.base_url}{API_FIVE_DAYS_PATH}"

    def login(self) -> dict[str, Any]:
        """Authenticate against v2_get_device.php using MD5 password and store token."""
        params = {"pwd": self.pwd_md5, "mailbox": self.email}
        t0 = time.perf_counter()
        try:
            resp = self._session.post(self.login_url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MarstekError(f"Ошибка сети при входе ({self.login_url}): {exc}") from exc
        self.last_latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        if resp.status_code == 401:
            raise MarstekAuthError("Неверный email или пароль (HTTP 401)")
        if resp.status_code >= 500:
            raise MarstekError(f"Сервер Marstek временно недоступен (HTTP {resp.status_code})")

        try:
            data = resp.json()
        except ValueError as exc:
            raise MarstekError(f"Некорректный JSON при входе: {resp.text[:200]}") from exc

        if not isinstance(data, dict):
            raise MarstekError(f"Неожиданный формат ответа при входе: {data}")

        code = str(data.get("code", ""))
        token = data.get("token")
        if not token:
            err_text = translate_error(code, data.get("msg") or data.get("message"))
            if code in ("3", "4"):
                raise MarstekAuthError(err_text)
            raise MarstekError(f"Не удалось получить токен: {err_text}")

        self.token = str(token)
        raw_devs = data.get("data")
        self.bound_devices = raw_devs if isinstance(raw_devs, list) else []
        return data

    def _fetch_device_list_raw(self) -> dict[str, Any]:
        params = {"token": self.token, "mailbox": self.email}
        t0 = time.perf_counter()
        try:
            resp = self._session.get(self.devices_url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MarstekError(f"Ошибка сети при запросе устройств ({self.devices_url}): {exc}") from exc
        self.last_latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        if resp.status_code >= 500:
            raise MarstekError(f"Сервер Marstek временно недоступен (HTTP {resp.status_code})")

        try:
            data = resp.json()
        except ValueError as exc:
            raise MarstekError(f"Некорректный JSON от getDeviceList: {resp.text[:200]}") from exc

        if not isinstance(data, dict):
            raise MarstekError(f"Неожиданный ответ getDeviceList: {data}")
        return data

    def get_devices(self, ignore_types: set[str] | None = None) -> list[dict[str, Any]]:
        """
        Fetch live device list via /ems/api/v1/getDeviceList.
        Automatically refreshes token via v2_get_device.php if expired/invalid (including code 8).
        """
        if not self.token:
            self.login()

        data = self._fetch_device_list_raw()
        code = data.get("code")
        msg_lower = str(data.get("msg") or data.get("message") or "").lower()

        if code in TOKEN_INVALID_CODES or "token" in msg_lower or "unauthorized" in msg_lower:
            self.login()
            data = self._fetch_device_list_raw()

        if "data" not in data or not isinstance(data.get("data"), list):
            # If account has bound_devices from login but getDeviceList returned empty/error
            err_text = translate_error(data.get("code"), data.get("msg") or data.get("message"))
            raise MarstekError(f"Ошибка получения списка устройств: {err_text}")

        ems_list: list[dict[str, Any]] = [d for d in data["data"] if isinstance(d, dict)]
        bound_by_id = {
            str(d.get("devid")): d
            for d in self.bound_devices
            if isinstance(d, dict) and d.get("devid")
        }

        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for dev in ems_list:
            devid = str(dev.get("devid") or "")
            if not devid:
                continue
            if ignore_types and str(dev.get("type") or "") in ignore_types:
                continue
            seen_ids.add(devid)
            b = bound_by_id.get(devid, {})
            item = {**b, **dev}
            item["devid"] = devid
            item["name"] = item.get("name") or f"Marstek {devid[-4:]}"
            item["type"] = item.get("type") or "Unknown"
            merged.append(item)

        # Also include any bound devices from login not present in ems_list
        for devid, b in bound_by_id.items():
            if devid not in seen_ids:
                if ignore_types and str(b.get("type") or "") in ignore_types:
                    continue
                item = dict(b)
                item["devid"] = devid
                item["name"] = item.get("name") or f"Marstek {devid[-4:]}"
                item["type"] = item.get("type") or "Unknown"
                merged.append(item)

        return merged

    def get_five_days(self, devid: str, date_str: str | None = None) -> dict[str, Any]:
        """Fetch 5-day 10-minute historical data via /ems/api/v2/getFiveDays."""
        if not self.token:
            self.login()
        if not date_str:
            date_str = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d")
        params = {
            "mailbox": self.email,
            "token": self.token,
            "devid": devid,
            "date": date_str,
        }
        try:
            resp = self._session.get(self.five_days_url, params=params, timeout=self.timeout)
            data = resp.json()
        except Exception as exc:
            raise MarstekError(f"Ошибка getFiveDays: {exc}") from exc
        if isinstance(data, dict) and data.get("code") in TOKEN_INVALID_CODES:
            self.login()
            params["token"] = self.token
            resp = self._session.get(self.five_days_url, params=params, timeout=self.timeout)
            data = resp.json()
        return data if isinstance(data, dict) else {"data": data}


def detect_grid_state(
    dev: dict[str, Any],
    mode: str = "auto",
    grid_threshold_w: float = 5.0,
    max_stale_sec: int = 1200,
) -> tuple[bool | None, str]:
    """
    Determine whether grid power (сеть) is present from a Marstek device telemetry dict.

    Modes:
      - 'online': Grid is considered ON when device is online (status == 1) and report_time is not stale.
      - 'grid':   Grid is considered ON when device is online AND (|grid| >= threshold OR AC charging > PV).
      - 'auto':   Online check + power telemetry check (if device is offline -> False; if online -> True with power info).
    Returns:
      (is_grid_on: bool | None, reason: str)
    """
    if not dev:
        return None, "нет данных"

    status_raw = dev.get("status")
    status_int: int | None = None
    if status_raw is not None and str(status_raw).strip() != "":
        try:
            status_int = int(float(str(status_raw)))
        except ValueError:
            if str(status_raw).lower() in ("online", "true", "on"):
                status_int = 1
            elif str(status_raw).lower() in ("offline", "false", "off"):
                status_int = 0

    grid_w = to_float(dev.get("grid"))
    charge_w = to_float(dev.get("charge"))
    discharge_w = to_float(dev.get("discharge"))
    pv_w = to_float(dev.get("pv"))
    load_w = to_float(dev.get("load"))
    report_ts = parse_report_time(dev.get("report_time"))

    now = int(time.time())
    stale_mins: int | None = None
    if report_ts and max_stale_sec > 0:
        age = now - report_ts
        if age > max_stale_sec:
            stale_mins = max(1, age // 60)

    if mode == "online":
        if status_int == 0:
            return False, "устройство офлайн (нет связи)"
        if stale_mins is not None and status_int is None:
            return False, f"нет отчётов {stale_mins} мин"
        if status_int == 1 or report_ts is not None or dev.get("soc") is not None:
            return True, "устройство на связи (online)"
        return None, "статус неизвестен"

    power_vals = (grid_w, charge_w, discharge_w, pv_w, load_w)
    # Для части моделей (VNSE3 и др.) облако отдаёт мощности всегда 0 — это «нет данных», а не 0 Вт
    no_power = all(v in (None, 0.0) for v in power_vals)

    if mode == "grid":
        if status_int == 0:
            return False, "устройство офлайн"
        if no_power:
            return None, "облако не отдаёт мощности для этой модели — выберите режим Авто/Online"
        if grid_w is not None and abs(grid_w) >= grid_threshold_w:
            return True, f"мощность сети {grid_w:+.0f} Вт"
        if charge_w is not None and charge_w >= grid_threshold_w:
            pv_val = pv_w or 0.0
            if charge_w > pv_val + grid_threshold_w:
                return True, f"заряд от сети {charge_w:.0f} Вт"
        if grid_w is not None or charge_w is not None:
            return False, f"поток сети 0 Вт (разряд {discharge_w or 0:.0f} Вт, нагрузка {load_w or 0:.0f} Вт)"
        return None, "нет датчика мощности сети (grid)"

    # mode == "auto" (default)
    if status_int == 0:
        return False, "устройство офлайн (нет связи с облаком)"
    if not no_power and grid_w is not None and abs(grid_w) >= grid_threshold_w:
        return True, f"сеть активна ({grid_w:+.0f} Вт)"
    if not no_power and charge_w is not None and charge_w >= grid_threshold_w and (pv_w is None or charge_w > (pv_w or 0) + grid_threshold_w):
        return True, f"идёт заряд от сети ({charge_w:.0f} Вт)"
    if stale_mins is not None and status_int is None:
        return False, f"отчёт устарел ({stale_mins} мин назад)"
    if status_int == 1 or dev.get("soc") is not None:
        details = []
        if not no_power:
            if grid_w is not None:
                details.append(f"сеть {grid_w:+.0f} Вт")
            if charge_w:
                details.append(f"заряд {charge_w:.0f} Вт")
            if discharge_w:
                details.append(f"разряд {discharge_w:.0f} Вт")
        suffix = f" ({', '.join(details)})" if details else ""
        return True, f"устройство онлайн{suffix}"

    return None, "ожидание телеметрии"


if __name__ == "__main__":
    import argparse
    import getpass
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    parser = argparse.ArgumentParser(description="Проверка Marstek Cloud API (eu.hamedata.com)")
    parser.add_argument("--email", help="Email аккаунта Marstek")
    parser.add_argument("--password", help="Пароль или MD5-хеш пароля")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Базовый URL облака")
    args = parser.parse_args()

    email = args.email or input("Marstek Email: ").strip()
    pwd = args.password or getpass.getpass("Password (или MD5): ").strip()

    client = MarstekClient(email=email, password_or_md5=pwd, base_url=args.base_url)
    try:
        login_res = client.login()
        console.print(
            Panel(
                f"[bold green]Успешный вход![/bold green]\n"
                f"URL: [cyan]{client.login_url}[/cyan]\n"
                f"Token: [yellow]{client.token[:12]}...[/yellow]\n"
                f"Latency: [magenta]{client.last_latency_ms} ms[/magenta]",
                title="Marstek Cloud Auth",
            )
        )
        devices = client.get_devices()
        tbl = Table(title=f"Устройства ({len(devices)}) — {client.devices_url}")
        tbl.add_column("ID (devid)", style="cyan")
        tbl.add_column("Имя", style="bold")
        tbl.add_column("Модель")
        tbl.add_column("Статус")
        tbl.add_column("SoC %", justify="right")
        tbl.add_column("Grid (W)", justify="right")
        tbl.add_column("Charge (W)", justify="right")
        tbl.add_column("Discharge (W)", justify="right")
        tbl.add_column("Load (W)", justify="right")
        tbl.add_column("PV (W)", justify="right")
        tbl.add_column("Сеть (auto)")

        for d in devices:
            on, reason = detect_grid_state(d, mode="auto")
            state_str = "[green]🟢 ЕСТЬ[/green]" if on else ("[red]🔴 НЕТ[/red]" if on is False else "[yellow]⏳ ?[/yellow]")
            tbl.add_row(
                str(d.get("devid", "")),
                str(d.get("name", "")),
                str(d.get("type", "")),
                str(d.get("status", "-")),
                str(d.get("soc", "-")),
                str(d.get("grid", "-")),
                str(d.get("charge", "-")),
                str(d.get("discharge", "-")),
                str(d.get("load", "-")),
                str(d.get("pv", "-")),
                f"{state_str} {reason}",
            )
        console.print(tbl)
    except Exception as e:
        console.print(Panel(f"[bold red]{e}[/bold red]", title="Ошибка Marstek API"))
