# Эндпоинты Marstek Cloud API (DoctaShizzle/marstek_cloud + eu.hamedata.com)

## 1. Базовые хосты (Регионы)
- Европа (основной в `marstek_cloud`): `https://eu.hamedata.com`
- Глобальный / Китай: `https://www.hamedata.com`
- Рекомендуемый заголовок `User-Agent`: `Dart/2.19 (dart:io)`

---

## 2. Авторизация и получение токена (`API_LOGIN`)
- Метод: `POST` (также поддерживается `GET`)
- URL: `https://eu.hamedata.com/app/Solar/v2_get_device.php`
- Query-параметры:
  - `mailbox` — Email аккаунта Marstek
  - `pwd` — MD5-хеш пароля в нижнем регистре (`hashlib.md5(password.encode('utf-8')).hexdigest()`)
- Пример запроса:
  `POST https://eu.hamedata.com/app/Solar/v2_get_device.php?pwd=<MD5_PASSWORD>&mailbox=<EMAIL>`
- Ответ JSON:
  ```json
  {
    "code": "2",
    "msg": "success",
    "token": "<TOKEN>",
    "data": [
      {
        "devid": "...",
        "name": "...",
        "sn": "...",
        "mac": "...",
        "type": "HMG-50",
        "access": "1",
        "bluetooth_name": "..."
      }
    ]
  }
  ```
- Коды ответа `v2_get_device.php`:
  - `"2"` — успешный вход, в `data` список привязанных устройств, в `token` сессионный токен
  - `"1"` — успешный вход, но на аккаунте пока нет привязанных устройств (`token` присутствует)
  - `"3"` — аккаунт (email) не зарегистрирован (`该邮箱暂未注册`)
  - `"4"` — неверный пароль (`密码错误`)

---

## 3. Получение списка устройств и live-телеметрии (`API_DEVICES`)
- Метод: `GET`
- URL: `https://eu.hamedata.com/ems/api/v1/getDeviceList`
- Query-параметры:
  - `token` — токен, полученный из `v2_get_device.php`
  - `mailbox` — (опционально) email аккаунта
- Пример запроса:
  `GET https://eu.hamedata.com/ems/api/v1/getDeviceList?token=<TOKEN>`
- Успешный ответ JSON (`code: 0`):
  ```json
  {
    "code": 0,
    "msg": "success",
    "data": [
      {
        "devid": "device123",
        "name": "Venus E",
        "type": "HMG-50",
        "sn": "SN123456789",
        "version": "1.2.3",
        "status": 1,
        "soc": 85,
        "charge": 1500,
        "discharge": 0,
        "load": 800,
        "pv": 2000,
        "grid": -700,
        "profit": 12.5,
        "report_time": 1705843200
      }
    ]
  }
  ```
- Поля устройства в `data`:
  - `devid` — уникальный ID устройства
  - `name` — имя устройства
  - `type` — модель (`HMG-50` Venus E V1/V2, `VNSE3-0` Venus E V3, `HME-5`, `HMJ-2` B2500D, `HME-4` CT002, `HME-3` CT003)
  - `sn` — серийный номер
  - `version` — версия прошивки
  - `status` — статус связи в облаке (`1` = online, `0` = offline)
  - `soc` — уровень заряда батареи (%)
  - `charge` — мощность зарядки (Вт)
  - `discharge` — мощность разрядки (Вт)
  - `load` — мощность нагрузки (Вт)
  - `pv` — мощность солнечных панелей (Вт)
  - `grid` — мощность сети (Вт; положительная/отрицательная при импорте/экспорте)
  - `profit` — накопленная экономия (€)
  - `report_time` — время последнего отчёта устройства (Unix timestamp или ISO-строка)
- Коды ошибок `getDeviceList`:
  - `code: 8` (`{"msg": "Unauthorized", "code": 8}`) или `-1`, `401`, `403` — токен истёк или инвалидирован (например, при входе из мобильного приложения Marstek). Требуется повторный вызов `v2_get_device.php` для получения нового `token`.

---

## 4. Дополнительные облачные и локальные эндпоинты экосистемы Marstek
- **История за 5 дней (интервалы по 10 минут, 144 точки/сутки)**:
  `GET https://eu.hamedata.com/ems/api/v2/getFiveDays?mailbox=<EMAIL>&token=<TOKEN>&devid=<DEVID>&date=YYYY-MM-DD`
- **Привязка нового устройства**:
  `GET/POST https://eu.hamedata.com/app/Solar/v2_add_device.php`
- **Проверка OTA-обновлений прошивки**:
  - Балконные/стандартные БЭС (Venus E): `https://eu.hamedata.com/ems/api/v2/checkSmallBalconyOTA`
  - AC-Couple: `https://eu.hamedata.com/ems/api/v1/checkAcCoupleOta`
  - B2500D (HMJ-2): `https://eu.hamedata.com/app/neng/v2_get_otadevice_b2500.php`
  - Wi-Fi модуль: `https://eu.hamedata.com/ems/api/v1/getCheckWifiOta`
- **Локальный UDP JSON-RPC API устройства (порт `30000`)**:
  - `{"id":0,"method":"Marstek.GetDevice","params":{"ble_mac":"0"}}`
  - `{"id":1,"method":"Wifi.GetStatus","params":{"id":0}}`
  - `{"id":1,"method":"Bat.GetStatus","params":{"id":0}}`
  - `{"id":1,"method":"ES.GetStatus","params":{"id":0}}`

---

## 5. Разбор APK Marstek 1.6.78 (com.hamedata.marstek, Flutter, libapp.so)

Приложение на Flutter: вся логика в `lib/arm64-v8a/libapp.so` (Dart AOT), строки вытащены в `apk/libapp_strings.txt`.

### Итог: живые мощности по HTTP НЕ отдаются
- Экран «сейчас» для Venus (`VenusPagePresenterMqttNowBinding`, `venus_telemetry_adapter.dart`,
  `TelemetrySourceChannel`) получает данные по **MQTT** (`cd=1` → кадр телеметрии) или по **BLE**,
  плюс опрос «Every 15s» — тоже по MQTT. HTTP-эндпоинта с текущими ваттами в приложении нет.
- MQTT-брокеры: AWS IoT `a40nr6osvmmaw-ats.iot.eu-central-1.amazonaws.com` / `...eu-west-3...`,
  префиксы топиков `marstek_energy/` (новые, 2025) и `hame_energy/` (старые), формат
  `<prefix>/<type>/App/<id>/ctrl` → `<prefix>/<type>/device/<id>/ctrl`. Авторизация — клиентский
  сертификат (mTLS), в APK сертификатов нет (`mqtt_client_cert`/`mqtt_private_key` приходят с сервера при настройке).

### Проверено вживую с токеном аккаунта (VNSE3-0)
| Эндпоинт | Ответ |
|---|---|
| `GET /ems/api/v1/getDeviceList` | SoC, status, report_time — живые; charge/discharge/load/pv/grid **всегда 0** |
| `GET /app/Solar/v2_get_deviceinfo.php` | только паспорт (devid/sn/mac/type/version) |
| `GET /ems/api/v1/getDeviceMqttStatus?devid&token` | `{salt, mqtt:"", ms:"", datetime:""}` |
| `POST /ems/api/v1/getMqttCrvCfg` (form: mac,type,…) | `code:1, data:[]` |
| `POST /ems/api/v1/setRealtimeData` | «上报成功» — это ЗАГРУЗКА данных от приложения, не чтение |
| `POST /ems/api/v1/getIsStatus` | `code:1, data:[]` |
| `/ems/api/v2/getFiveDays`, `/v2/getTotal`, `/app/Solar/v2_get_day.php`, `get_dayrecord.php`, `v2_get_tenmins_one.php`, `/ems/api/v1/getSmallBalcony*`, `combinationDevice/detail` | `32002 权限不足` (нет прав для этой модели/аккаунта) |
| `/app/Solar/v2_get_tenmins.php`, `get_day.php` | `{"code":"0"}` (пусто) |
| `POST /ems/api/v3/getReport` | требует `type=day|month|year|total` — статистика энергии, не мощность |

### Реальные пути получить ватты
1. MQTT: подключиться к брокеру Marstek с сертификатом (как делает проект `tomquist/hame-relay`),
   подписаться на `marstek_energy/VNSE3-0/device/<topic_id>/ctrl`, слать `cd=1`. topic_id считается
   из `salt` (есть в getDeviceList) и MAC.
2. Локальный API батареи: UDP `:30000`, JSON-RPC `ES.GetStatus` / `Bat.GetStatus` (включается в
   приложении / через поддержку).
