"""Live test: connect to Marstek cloud MQTT with hame-relay certs, send cd=1 to the user's VNSE3, print reply."""
import json, os, random, sqlite3, ssl, sys, time
import paho.mqtt.client as mqtt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import hame_mqtt as hm

C = os.path.join(ROOT, "mqtt_certs")
rd = lambda f: open(os.path.join(C, f), encoding="utf-8").read().strip()

db = sqlite3.connect(os.path.join(ROOT, "data.db")); db.row_factory = sqlite3.Row
dev = json.loads(db.execute("SELECT cache FROM devices").fetchone()["cache"])
mac, typ, salt = dev["mac"], dev["type"], dev.get("salt", "")
kind, sid = hm.resolve_topic_id(salt, mac, typ)
aid = hm.aes_topic_id(rd("hame-2025-topic-encryption-key"), mac)
print("type", typ, "| salt kind", kind, "| cq id", sid, "| aes id", aid[:16] + "...")

url = rd("hame-2025-url")  # e.g. mqtts://host:8883
host = url.split("://")[-1].split("/")[0]
port = 8883
if ":" in host:
    host, port = host.rsplit(":", 1); port = int(port)
print("broker host:", host[:6] + "..." + host[-18:], "port", port)

ids = [i for i in (sid, aid) if i]
prefixes = ["marstek_energy/"]
got = []

def on_connect(c, u, flags, rc, props=None):
    print("CONNECT rc=", rc)
    for p in prefixes:
        for i in ids:
            c.subscribe(f"{p}{typ}/device/{i}/ctrl", qos=0)
    for p in prefixes:
        for i in ids:
            c.publish(f"{p}{typ}/App/{i}/ctrl", "cd=1", qos=0)
    print("subscribed + sent cd=1 on", len(ids) * len(prefixes), "topic(s)")

def on_message(c, u, msg):
    t = msg.topic
    which = "cq" if sid and sid in t else "aes"
    pl = msg.payload.decode("utf-8", "replace")
    got.append((which, pl))
    print(f"MSG [{which}] len={len(pl)}:", pl[:900])

cid = "mst_" + "".join(random.choice("0123456789abcdef") for _ in range(12))
cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, protocol=mqtt.MQTTv311)
ctx = ssl.create_default_context(cafile=os.path.join(C, "ca.crt"))
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
ctx.load_cert_chain(os.path.join(C, "hame-2025.crt"), os.path.join(C, "hame-2025.key"))
cl.tls_set_context(ctx)
cl.on_connect = on_connect
cl.on_message = on_message
cl.connect(host, port, keepalive=30)
cl.loop_start()
t0 = time.time()
while time.time() - t0 < 25 and not got:
    time.sleep(0.5)
if not got:
    # retry poll once
    for i in ids:
        cl.publish(f"marstek_energy/{typ}/App/{i}/ctrl", "cd=1")
    t1 = time.time()
    while time.time() - t1 < 15 and not got:
        time.sleep(0.5)
cl.loop_stop(); cl.disconnect()
print("replies:", len(got))
