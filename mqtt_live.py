"""
Live Marstek telemetry over the cloud MQTT broker (same channel the official app uses).

One persistent TLS (mTLS) connection; every `interval` seconds publish "cd=1" to each registered
device's App topic and cache the CSV reply from its device topic.

Certificates: ./mqtt_certs (ca.crt, hame-2025.crt/.key, hame-2025-url, hame-2025-topic-encryption-key),
taken from the public ghcr.io/tomquist/hame-relay image (see pull_certs.py).
"""
from __future__ import annotations

import logging
import os
import random
import ssl
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

import hame_mqtt as hm

log = logging.getLogger("marstek_mqtt")

PREFIX = "marstek_energy/"


class MarstekLive:
    def __init__(self, cert_dir: str, interval: int = 30):
        self.cert_dir = cert_dir
        self.interval = max(10, interval)
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}   # devid -> {type, mac, ids:[...]}
        self._latest: dict[str, tuple[float, dict[str, str]]] = {}  # devid -> (ts, frame)
        self._topic_to_dev: dict[str, str] = {}
        self._client: mqtt.Client | None = None
        self._connected = False
        self._stop = threading.Event()
        self.fast_until = 0.0   # до этого момента опрашиваем каждые ~3 с (live-режим), потом обычный interval
        self._thread: threading.Thread | None = None
        rd = lambda f: open(os.path.join(cert_dir, f), encoding="utf-8").read().strip()
        url = rd("hame-2025-url")
        host = url.split("://")[-1].split("/")[0]
        port = 8883
        if ":" in host:
            host, p = host.rsplit(":", 1)
            port = int(p)
        self.host, self.port = host, port
        self.aes_key = rd("hame-2025-topic-encryption-key")

    # ------------------------------------------------------------ public
    @staticmethod
    def available(cert_dir: str) -> bool:
        need = ("ca.crt", "hame-2025.crt", "hame-2025.key", "hame-2025-url", "hame-2025-topic-encryption-key")
        return all(os.path.exists(os.path.join(cert_dir, f)) for f in need)

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="marstek-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass

    def register(self, devid: str, dtype: str, mac: str, salt: str) -> None:
        """Add/refresh a device. Topic id from salt (cq) when confirmed, plus AES id as fallback."""
        if not (devid and dtype and mac):
            return
        _, sid = hm.resolve_topic_id(salt or "", mac, dtype)
        ids = [i for i in (sid, hm.aes_topic_id(self.aes_key, mac)) if i]
        with self._lock:
            old = self._devices.get(devid)
            if old and old["ids"] == ids and old["type"] == dtype:
                return
            self._devices[devid] = {"type": dtype, "mac": mac, "ids": ids}
            for i in ids:
                self._topic_to_dev[f"{PREFIX}{dtype}/device/{i}/ctrl"] = devid
        if self._connected and self._client:
            self._subscribe_one(devid)
            self._poll_one(devid)

    def latest(self, devid: str) -> tuple[float, dict[str, str]] | None:
        with self._lock:
            return self._latest.get(devid)

    def fresh(self, devid: str, max_age: float | None = None) -> dict[str, str] | None:
        """Last frame if not older than max_age (default 3 poll intervals)."""
        item = self.latest(devid)
        if not item:
            return None
        ts, frame = item
        if time.time() - ts > (max_age or self.interval * 3 + 10):
            return None
        return frame

    def poll_now(self) -> None:
        with self._lock:
            ids = list(self._devices)
        for d in ids:
            self._poll_one(d)

    # ------------------------------------------------------------ internals
    def _make_client(self) -> mqtt.Client:
        cid = "mst_" + "".join(random.choice("0123456789abcdef") for _ in range(12))
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, protocol=mqtt.MQTTv311)
        ctx = ssl.create_default_context(cafile=os.path.join(self.cert_dir, "ca.crt"))
        # Same as hame-relay: broker presents a cert not matching the endpoint name chain we pin
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(os.path.join(self.cert_dir, "hame-2025.crt"),
                            os.path.join(self.cert_dir, "hame-2025.key"))
        cl.tls_set_context(ctx)
        cl.on_connect = self._on_connect
        cl.on_disconnect = self._on_disconnect
        cl.on_message = self._on_message
        cl.reconnect_delay_set(min_delay=5, max_delay=120)
        return cl

    def _on_connect(self, c, u, flags, rc, props=None):
        ok = not getattr(rc, "is_failure", False) and str(rc) in ("Success", "0")
        self._connected = ok
        log.info("MQTT connect: %s", rc)
        if ok:
            with self._lock:
                ids = list(self._devices)
            for d in ids:
                self._subscribe_one(d)
            self.poll_now()

    def _on_disconnect(self, c, u, flags, rc, props=None):
        self._connected = False
        log.warning("MQTT disconnected: %s", rc)

    def _on_message(self, c, u, msg):
        with self._lock:
            devid = self._topic_to_dev.get(msg.topic)
        if not devid:
            return
        payload = msg.payload.decode("utf-8", "replace")
        frame = hm.parse_kv(payload)
        if frame.get("cd") not in (None, "1"):
            return
        # only full runtime frames (they always carry SoC + working status)
        if "cel_c" not in frame or "grd_t" not in frame:
            return
        with self._lock:
            self._latest[devid] = (time.time(), frame)

    def _subscribe_one(self, devid: str) -> None:
        with self._lock:
            d = self._devices.get(devid)
        if not d or not self._client:
            return
        for i in d["ids"]:
            self._client.subscribe(f"{PREFIX}{d['type']}/device/{i}/ctrl", qos=0)

    def _poll_one(self, devid: str) -> None:
        with self._lock:
            d = self._devices.get(devid)
        if not d or not self._client or not self._connected:
            return
        for i in d["ids"]:
            self._client.publish(f"{PREFIX}{d['type']}/App/{i}/ctrl", "cd=1", qos=0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._client = self._make_client()
                self._client.connect(self.host, self.port, keepalive=30)
                self._client.loop_start()
                last_poll = 0.0
                while not self._stop.is_set():
                    now = time.time()
                    period = 3 if now < self.fast_until else self.interval
                    if self._connected and now - last_poll >= period:
                        self.poll_now()
                        last_poll = now
                    self._stop.wait(1)   # короткий шаг: быстрый режим включается сразу, а не после долгого сна
            except Exception as ex:
                log.warning("MQTT loop error: %s", ex)
            finally:
                try:
                    if self._client:
                        self._client.loop_stop()
                        self._client.disconnect()
                except Exception:
                    pass
                self._connected = False
            self._stop.wait(15)


# ---------------------------------------------------------------- frame -> human values
WORK_STATUS = {"0": "сон", "1": "ожидание", "2": "заряд", "3": "разряд", "4": "резерв (EPS, без сети)",
               "5": "обновление", "6": "байпас"}
BAT_STATUS = {"1": "простой", "2": "заряд", "3": "разряд"}
WORK_MODE = {"0": "авто", "1": "ручной", "2": "трейдинг", "5": "AI"}


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def decode(frame: dict[str, str]) -> dict[str, Any]:
    """Venus runtime frame (cd=1) -> normalized dict. Field meanings per tomquist/hm2mqtt venus.ts."""
    f = frame
    bp = _num(f.get("bp"))
    st = f.get("cel_s")
    charge = discharge = 0.0
    if bp is not None:
        if st == "2":
            charge = abs(bp)
        elif st == "3":
            discharge = abs(bp)
        elif bp > 0:
            charge = bp
        elif bp < 0:
            discharge = -bp
    out = {
        "soc": _num(f.get("cel_c")),
        "battery_wh": (_num(f.get("cel_p")) or 0) * 10 if f.get("cel_p") else None,
        "battery_power": bp,
        "charge": charge,
        "discharge": discharge,
        "grid_power": _num(f.get("gp")) if f.get("gp") is not None else _num(f.get("grd_o")),
        "combined_power": _num(f.get("grd_o")),
        "eps_power": _num(f.get("grd_f")),
        "work_status": f.get("grd_t"),
        "work_status_text": WORK_STATUS.get(f.get("grd_t", ""), f.get("grd_t")),
        "bat_status_text": BAT_STATUS.get(st or "", st),
        "work_mode_text": WORK_MODE.get(f.get("wor_m", ""), f.get("wor_m")),
        "day_charge_kwh": (_num(f.get("ele_d")) or 0) / 100 if f.get("ele_d") else None,
        "day_discharge_kwh": (_num(f.get("grd_d")) or 0) / 100 if f.get("grd_d") else None,
        "total_charge_kwh": (_num(f.get("tot_i")) or 0) / 100 if f.get("tot_i") else None,
        "total_discharge_kwh": (_num(f.get("tot_o")) or 0) / 100 if f.get("tot_o") else None,
        "wifi_dbm": -(_num(f.get("wif_s")) or 0) if f.get("wif_s") else None,
        "wifi_name": f.get("wifi_n"),
        "error_code": f.get("err_t"),
        "warning_code": f.get("err_a"),
        "max_charge_w": _num(f.get("mcp_w")),
        "max_discharge_w": _num(f.get("mdp_w")),
        "local_api": f.get("api") == "1",
        "local_api_port": f.get("port"),
    }
    return out


def grid_from_live(live: dict[str, Any]) -> tuple[bool | None, str]:
    """Grid presence from a decoded frame. Working status 4 = backup/EPS = mains lost."""
    ws = live.get("work_status")
    if ws == "4":
        return False, "батарея в режиме резерва (EPS) — сети нет"
    gp = live.get("grid_power")
    parts = [f"статус: {live.get('work_status_text')}"]
    if gp is not None:
        parts.append(f"сеть {gp:+.0f} Вт")
    return True, ", ".join(parts)
