"""
SQLite persistence for the Marstek Grid Alert Telegram Bot.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    status TEXT NOT NULL,          -- admin | allowed | pending | denied
    created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    pwd_md5 TEXT NOT NULL,
    token TEXT,
    base_url TEXT NOT NULL DEFAULT 'https://eu.hamedata.com',
    last_ok INTEGER NOT NULL DEFAULT 0,
    last_err TEXT,
    created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    devid TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    sn TEXT NOT NULL DEFAULT '',
    mac TEXT NOT NULL DEFAULT '',
    capacity_kwh REAL NOT NULL DEFAULT 5.12,
    mode TEXT NOT NULL DEFAULT 'auto',       -- auto | online | grid
    grid_threshold REAL NOT NULL DEFAULT 5.0,
    muted INTEGER NOT NULL DEFAULT 0,
    grid_was_on INTEGER,                     -- NULL | 0 | 1
    last_change_ts INTEGER,                  -- unix ts of last grid state transition
    hidden_fields TEXT,                      -- JSON list of hidden telemetry keys
    cache TEXT,                              -- JSON dict of last device payload
    checked INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL,
    UNIQUE(account_id, devid)
);

CREATE TABLE IF NOT EXISTS outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,                          -- NULL = отключение продолжается
    start_soc REAL,
    end_soc REAL
);
CREATE INDEX IF NOT EXISTS idx_outages_dev ON outages(device_id, start_ts);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    val TEXT
);

-- доступ «только просмотр» к чужому устройству
CREATE TABLE IF NOT EXISTS shares (
    device_id INTEGER NOT NULL,
    viewer_id INTEGER NOT NULL,
    alerts INTEGER NOT NULL DEFAULT 1,   -- слать зрителю уведомления об этом устройстве
    created INTEGER NOT NULL,
    PRIMARY KEY (device_id, viewer_id)
);

-- история телеметрии (для графика в приложении и детектора аномалий)
CREATE TABLE IF NOT EXISTS samples (
    device_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    soc REAL,
    bp REAL,          -- мощность батареи, Вт (+заряд / -разряд)
    grid REAL,        -- мощность по счётчику, Вт
    eps REAL,         -- выход EPS, Вт
    wifi REAL,        -- dBm
    ws TEXT,          -- рабочий статус (grd_t)
    st TEXT,          -- статус батареи (cel_s): 1 простой / 2 заряд / 3 разряд
    PRIMARY KEY (device_id, ts)
);
"""

# колонки, добавленные после первой версии схемы (миграция через ALTER TABLE)
DEVICE_MIGRATIONS = {
    "soc_level": "INTEGER",                         # самый низкий уже отправленный порог SoC в текущем отключении
    "full_pending": "INTEGER NOT NULL DEFAULT 0",   # ждём сообщение «батарея заряжена»
    "alert_over": "INTEGER NOT NULL DEFAULT 0",     # алерт о перегрузке уже отправлен
    "alert_link": "INTEGER NOT NULL DEFAULT 0",     # алерт «связь потеряна» уже отправлен
    # --- пользовательские настройки алертов
    "set_low": "INTEGER NOT NULL DEFAULT 1",            # предупреждения о низком заряде вкл/выкл
    "set_levels": "TEXT NOT NULL DEFAULT '30,15,10'",   # пороги SoC через запятую
    "set_full": "INTEGER NOT NULL DEFAULT 1",           # «батарея заряжена до 100%»
    "set_over": "INTEGER NOT NULL DEFAULT 1",           # алерт перегрузки
    "set_over_pct": "INTEGER NOT NULL DEFAULT 90",      # % от предела, при котором срабатывает
    "set_over_w": "REAL NOT NULL DEFAULT 0",            # предел, Вт (0 = брать у батареи / 2500)
    "set_link": "INTEGER NOT NULL DEFAULT 1",           # алерт потери связи
    "set_link_min": "INTEGER NOT NULL DEFAULT 4",       # через сколько минут молчания алертить
    "set_fc": "INTEGER NOT NULL DEFAULT 1",             # показывать прогноз
    "set_anom": "INTEGER NOT NULL DEFAULT 1",           # детектор аномалий
}


class DB:
    def __init__(self, path: str, admin_id: int = 0):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        have = {r["name"] for r in self.c.execute("PRAGMA table_info(devices)")}
        for col, decl in DEVICE_MIGRATIONS.items():
            if col not in have:
                self.c.execute(f"ALTER TABLE devices ADD COLUMN {col} {decl}")
        # устройства, которые уже сейчас без сети, но журнала ещё нет
        self.c.execute(
            "INSERT INTO outages(device_id, start_ts) "
            "SELECT id, COALESCE(last_change_ts, ?) FROM devices WHERE grid_was_on=0 "
            "AND id NOT IN (SELECT device_id FROM outages WHERE end_ts IS NULL)",
            (int(time.time()),),
        )
        if admin_id:
            self.c.execute(
                "INSERT INTO users(id, name, status, created) VALUES(?, ?, 'admin', ?) "
                "ON CONFLICT(id) DO UPDATE SET status='admin'",
                (admin_id, "admin", int(time.time())),
            )
        self.c.commit()

    # ------------------------------------------------------------------ users
    def user(self, uid: int):
        return self.c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def has_admin(self) -> bool:
        return self.c.execute("SELECT 1 FROM users WHERE status='admin' LIMIT 1").fetchone() is not None

    def admins(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM users WHERE status='admin' ORDER BY created").fetchall()

    def upsert_user(
        self,
        uid: int,
        username: str | None = None,
        name: str | None = None,
        status: str | None = None,
    ):
        u = self.user(uid)
        if u is None:
            # First user becomes admin automatically if no admin is configured yet
            effective_status = status or ("admin" if not self.has_admin() else "pending")
            self.c.execute(
                "INSERT INTO users(id, username, name, status, created) VALUES(?, ?, ?, ?, ?)",
                (uid, username, name, effective_status, int(time.time())),
            )
        else:
            self.c.execute(
                "UPDATE users SET username=COALESCE(?, username), name=COALESCE(?, name), "
                "status=COALESCE(?, status) WHERE id=?",
                (username, name, status, uid),
            )
        self.c.commit()
        return self.user(uid)

    def users(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.c.execute("SELECT * FROM users WHERE status=? ORDER BY created", (status,)).fetchall()
        return self.c.execute("SELECT * FROM users ORDER BY created").fetchall()

    def delete_user(self, uid: int):
        mine = "(SELECT id FROM devices WHERE user_id=?)"
        self.c.execute(f"DELETE FROM outages WHERE device_id IN {mine}", (uid,))
        self.c.execute(f"DELETE FROM samples WHERE device_id IN {mine}", (uid,))
        self.c.execute(f"DELETE FROM kv WHERE key LIKE 'anom:%' AND CAST(substr(key, 6, instr(substr(key, 6), ':') - 1) AS INTEGER) IN {mine}", (uid,))
        self.c.execute("DELETE FROM shares WHERE viewer_id=? OR device_id IN (SELECT id FROM devices WHERE user_id=?)", (uid, uid))
        self.c.execute("DELETE FROM devices WHERE user_id=?", (uid,))
        self.c.execute("DELETE FROM accounts WHERE user_id=?", (uid,))
        self.c.execute("DELETE FROM users WHERE id=?", (uid,))
        self.c.commit()

    # ------------------------------------------------------------------ accounts
    def upsert_account(
        self,
        user_id: int,
        email: str,
        pwd_md5: str,
        token: str | None,
        base_url: str = "https://eu.hamedata.com",
        label: str = "",
    ) -> int:
        now = int(time.time())
        row = self.c.execute(
            "SELECT id FROM accounts WHERE user_id=? AND lower(email)=lower(?) AND base_url=?",
            (user_id, email.strip(), base_url),
        ).fetchone()
        if row:
            aid = int(row["id"])
            self.c.execute(
                "UPDATE accounts SET pwd_md5=?, token=?, label=COALESCE(NULLIF(?, ''), label), "
                "last_ok=?, last_err=NULL WHERE id=?",
                (pwd_md5, token, label, now, aid),
            )
        else:
            cur = self.c.execute(
                "INSERT INTO accounts(user_id, label, email, pwd_md5, token, base_url, last_ok, created) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, label or email.strip(), email.strip(), pwd_md5, token, base_url, now, now),
            )
            aid = int(cur.lastrowid)
        self.c.commit()
        return aid

    def accounts(self, user_id: int | None = None) -> list[sqlite3.Row]:
        if user_id is not None:
            return self.c.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return self.c.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def account(self, aid: int):
        return self.c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()

    def set_account(self, aid: int, **kw: Any):
        for k, v in kw.items():
            self.c.execute(f"UPDATE accounts SET {k}=? WHERE id=?", (v, aid))
        self.c.commit()

    def del_account(self, aid: int):
        self.c.execute("DELETE FROM devices WHERE account_id=?", (aid,))
        self.c.execute("DELETE FROM accounts WHERE id=?", (aid,))
        self.c.commit()

    # ------------------------------------------------------------------ devices
    def sync_account_devices(self, account_id: int, user_id: int, dev_list: list[dict[str, Any]]) -> list[sqlite3.Row]:
        now = int(time.time())
        for d in dev_list:
            devid = str(d.get("devid") or "").strip()
            if not devid:
                continue
            name = str(d.get("name") or f"Marstek {devid[-4:]}")
            dtype = str(d.get("type") or "Unknown")
            sn = str(d.get("sn") or "")
            mac = str(d.get("mac") or "")
            cache_json = json.dumps(d, ensure_ascii=False)

            existing = self.c.execute(
                "SELECT id FROM devices WHERE account_id=? AND devid=?",
                (account_id, devid),
            ).fetchone()
            if existing:
                self.c.execute(
                    "UPDATE devices SET name=?, type=?, sn=COALESCE(NULLIF(?, ''), sn), "
                    "mac=COALESCE(NULLIF(?, ''), mac), cache=?, checked=? WHERE id=?",
                    (name, dtype, sn, mac, cache_json, now, existing["id"]),
                )
            else:
                self.c.execute(
                    "INSERT INTO devices(account_id, user_id, devid, name, type, sn, mac, cache, checked, created) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (account_id, user_id, devid, name, dtype, sn, mac, cache_json, now, now),
                )
        self.c.commit()
        return self.devices(user_id=user_id, account_id=account_id)

    def devices(self, user_id: int | None = None, account_id: int | None = None) -> list[sqlite3.Row]:
        conds, args = [], []
        if user_id is not None:
            conds.append("user_id=?")
            args.append(user_id)
        if account_id is not None:
            conds.append("account_id=?")
            args.append(account_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return self.c.execute(f"SELECT * FROM devices{where} ORDER BY id", tuple(args)).fetchall()

    def device(self, did: int):
        return self.c.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()

    def set_device(self, did: int, **kw: Any):
        for k, v in kw.items():
            self.c.execute(f"UPDATE devices SET {k}=? WHERE id=?", (v, did))
        self.c.commit()

    def del_device(self, did: int):
        self.c.execute("DELETE FROM shares WHERE device_id=?", (did,))
        self.c.execute("DELETE FROM kv WHERE key LIKE ?", (f"anom:{did}:%",))
        self.c.execute("DELETE FROM outages WHERE device_id=?", (did,))
        self.c.execute("DELETE FROM samples WHERE device_id=?", (did,))
        self.c.execute("DELETE FROM devices WHERE id=?", (did,))
        self.c.commit()

    # ------------------------------------------------------------------ outages
    def open_outage(self, did: int):
        return self.c.execute(
            "SELECT * FROM outages WHERE device_id=? AND end_ts IS NULL ORDER BY start_ts DESC LIMIT 1", (did,)
        ).fetchone()

    def start_outage(self, did: int, ts: int, soc: float | None = None):
        if self.open_outage(did):
            return
        self.c.execute("INSERT INTO outages(device_id, start_ts, start_soc) VALUES(?, ?, ?)", (did, ts, soc))
        self.c.commit()

    def end_outage(self, did: int, ts: int, soc: float | None = None, started: int | None = None):
        o = self.open_outage(did)
        if o:
            self.c.execute("UPDATE outages SET end_ts=?, end_soc=? WHERE id=?", (ts, soc, o["id"]))
        elif started:
            self.c.execute(
                "INSERT INTO outages(device_id, start_ts, end_ts, end_soc) VALUES(?, ?, ?, ?)",
                (did, started, ts, soc),
            )
        self.c.commit()

    def outages(self, did: int, since: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM outages WHERE device_id=? AND (end_ts IS NULL OR end_ts>=?) ORDER BY start_ts DESC",
            (did, since),
        ).fetchall()

    # ------------------------------------------------------------------ kv
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.c.execute("SELECT val FROM kv WHERE key=?", (key,)).fetchone()
        return r["val"] if r else default

    def kv_set(self, key: str, val: str | None):
        if val is None:
            self.c.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.c.execute("INSERT INTO kv(key, val) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET val=excluded.val", (key, val))
        self.c.commit()

    # ------------------------------------------------------------------ samples
    def add_sample(self, did: int, ts: int, soc, bp, grid, eps, wifi, ws, st=None):
        self.c.execute(
            "INSERT OR REPLACE INTO samples(device_id, ts, soc, bp, grid, eps, wifi, ws, st) VALUES(?,?,?,?,?,?,?,?,?)",
            (did, ts, soc, bp, grid, eps, wifi, ws, st),
        )
        self.c.commit()

    def samples(self, did: int, since: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT * FROM samples WHERE device_id=? AND ts>=? ORDER BY ts", (did, since)
        ).fetchall()

    def prune_samples(self, older_than: int):
        self.c.execute("DELETE FROM samples WHERE ts<?", (older_than,))
        self.c.commit()

    # ------------------------------------------------------------------ shares (доступ только на просмотр)
    def shares(self, did: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT s.*, u.name, u.username FROM shares s LEFT JOIN users u ON u.id=s.viewer_id "
            "WHERE s.device_id=? ORDER BY s.created", (did,)).fetchall()

    def share_add(self, did: int, viewer_id: int) -> None:
        self.c.execute("INSERT OR IGNORE INTO shares(device_id, viewer_id, alerts, created) VALUES(?,?,1,?)",
                       (did, viewer_id, int(time.time())))
        self.c.commit()

    def share_del(self, did: int, viewer_id: int) -> None:
        self.c.execute("DELETE FROM shares WHERE device_id=? AND viewer_id=?", (did, viewer_id))
        self.c.commit()

    def share_toggle_alerts(self, did: int, viewer_id: int) -> None:
        self.c.execute("UPDATE shares SET alerts=1-alerts WHERE device_id=? AND viewer_id=?", (did, viewer_id))
        self.c.commit()

    def is_shared(self, did: int, viewer_id: int) -> bool:
        return self.c.execute("SELECT 1 FROM shares WHERE device_id=? AND viewer_id=?", (did, viewer_id)).fetchone() is not None

    def shared_devices(self, viewer_id: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT d.* FROM devices d JOIN shares s ON s.device_id=d.id WHERE s.viewer_id=? "
            # та же физическая станция уже подключена у самого зрителя — дубль с глазиком не показываем
            "AND NOT EXISTS (SELECT 1 FROM devices m WHERE m.user_id=? AND m.devid=d.devid) ORDER BY d.id",
            (viewer_id, viewer_id)).fetchall()

    def alert_viewers(self, did: int) -> list[int]:
        return [r[0] for r in self.c.execute("SELECT viewer_id FROM shares WHERE device_id=? AND alerts=1", (did,))]
