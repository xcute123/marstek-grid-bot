"""Детектор аномалий по истории телеметрии (чистые функции — легко тестировать).

sample: dict(ts, soc, bp, grid, eps, wifi, ws, st)
  bp  — мощность батареи со знаком, Вт (+ заряд, − разряд)
  st  — статус батареи (cel_s): "1" простой, "2" заряд, "3" разряд
  ws  — рабочий статус инвертора (grd_t)
"""
from __future__ import annotations

from statistics import median
from typing import Any

WIFI_BAD_DBM = -80
IDLE_DRAIN_W = 60
SOC_LOSS_PTS = 4.0
SOC_JUMP_PTS = 8.0
FLAP_CHANGES = 6


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def detect(samples: list[dict[str, Any]], cap_kwh: float, on_grid: bool | None) -> list[dict[str, str]]:
    """Список найденных аномалий: [{code, title, text}]. samples — по возрастанию ts."""
    out: list[dict[str, str]] = []
    if len(samples) < 5:
        return out
    now = samples[-1]["ts"]

    # --- слабый Wi-Fi (медиана за 10 минут)
    w = [x for x in samples if now - x["ts"] <= 600 and _f(x.get("wifi")) is not None]
    if len(w) >= 5:
        m = median(_f(x["wifi"]) for x in w)
        if m <= WIFI_BAD_DBM:
            out.append({"code": "wifi", "title": "📶 Слабый Wi-Fi у батареи",
                        "text": f"Сигнал {m:.0f} dBm (норма лучше −75). Связь может пропадать — "
                                "поднесите роутер ближе или добавьте точку доступа."})

    # --- тянет в простое
    idle = [x for x in samples if now - x["ts"] <= 900]
    if on_grid and len(idle) >= 10 and all(str(x.get("st")) == "1" for x in idle):
        vals = [abs(_f(x.get("bp")) or 0.0) for x in idle]
        mean = sum(vals) / len(vals)
        if mean > IDLE_DRAIN_W:
            out.append({"code": "idle", "title": "🔌 Станция тянет мощность в простое",
                        "text": f"Статус «простой», но батарея отдаёт/берёт в среднем {mean:.0f} Вт уже 15 мин "
                                "(обычно 7–15 Вт). Проверьте, что подключено к EPS-выходу."})

    # --- заряд падает быстрее, чем объясняет мощность
    win = [x for x in samples if now - x["ts"] <= 2400 and _f(x.get("soc")) is not None and _f(x.get("bp")) is not None]
    if len(win) >= 20 and win[-1]["ts"] - win[0]["ts"] >= 1500:
        gaps = max(b["ts"] - a["ts"] for a, b in zip(win, win[1:]))
        if gaps <= 300:
            net = thr = 0.0
            for a, b in zip(win, win[1:]):
                dt = (b["ts"] - a["ts"]) / 3600.0
                p = (_f(a["bp"]) + _f(b["bp"])) / 2.0
                net += p * dt
                thr += abs(p) * dt
            per = max(cap_kwh, 0.1) * 10.0          # Вт·ч на 1 % заряда
            expected = net / per                    # изменение SoC, п.п., которое объясняет мощность
            actual = _f(win[-1]["soc"]) - _f(win[0]["soc"])
            tol = SOC_LOSS_PTS + 0.12 * (thr / per)
            if actual - expected < -tol:
                out.append({"code": "soc_loss", "title": "🪫 Заряд уходит быстрее, чем показывает мощность",
                            "text": f"За {int((win[-1]['ts'] - win[0]['ts']) / 60)} мин заряд изменился на {actual:+.1f} п.п., "
                                    f"а по мощности ожидалось {expected:+.1f}. Возможна утечка, сбой калибровки BMS "
                                    "или нагрузка, которую не видно в телеметрии."})

    # --- скачок SoC
    for a, b in zip(samples, samples[1:]):
        if b["ts"] - a["ts"] <= 150:
            sa, sb = _f(a.get("soc")), _f(b.get("soc"))
            pa, pb = abs(_f(a.get("bp")) or 0.0), abs(_f(b.get("bp")) or 0.0)
            if sa is not None and sb is not None and abs(sb - sa) >= SOC_JUMP_PTS and max(pa, pb) < 500 \
                    and now - b["ts"] <= 1800:
                out.append({"code": "soc_jump", "title": "📉 Скачок заряда",
                            "text": f"Заряд внезапно изменился {sa:.0f}% → {sb:.0f}% без соответствующей мощности. "
                                    "Обычно это перекалибровка BMS."})
                break

    # --- частое переключение режимов
    ws = [x for x in samples if now - x["ts"] <= 600 and x.get("ws") not in (None, "")]
    if len(ws) >= 8:
        ch = sum(1 for a, b in zip(ws, ws[1:]) if a["ws"] != b["ws"])
        if ch >= FLAP_CHANGES:
            out.append({"code": "flap", "title": "🔁 Станция часто переключает режим",
                        "text": f"{ch} смен статуса за 10 минут. Это может быть нестабильная сеть "
                                "(скачки напряжения) или конфликт с другим устройством управления."})
    return out
