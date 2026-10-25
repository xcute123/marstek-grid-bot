"""Pull public ghcr.io/tomquist/hame-relay image layers (no docker) and extract /app/certs + brokers.json."""
import io, os, tarfile, gzip, requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mqtt_certs")
os.makedirs(OUT, exist_ok=True)
REPO = "tomquist/hame-relay"
S = requests.Session()

tok = S.get(f"https://ghcr.io/token?scope=repository:{REPO}:pull&service=ghcr.io", timeout=30).json()["token"]
H = {"Authorization": f"Bearer {tok}",
     "Accept": ",".join([
         "application/vnd.oci.image.index.v1+json",
         "application/vnd.docker.distribution.manifest.list.v2+json",
         "application/vnd.oci.image.manifest.v1+json",
         "application/vnd.docker.distribution.manifest.v2+json"])}

idx = S.get(f"https://ghcr.io/v2/{REPO}/manifests/latest", headers=H, timeout=30).json()
man = idx
if "manifests" in idx:
    amd = [m for m in idx["manifests"] if m.get("platform", {}).get("architecture") == "amd64"
           and m.get("platform", {}).get("os") == "linux"]
    dig = amd[0]["digest"]
    man = S.get(f"https://ghcr.io/v2/{REPO}/manifests/{dig}", headers=H, timeout=30).json()
layers = man["layers"]
print("layers:", len(layers), [round(l["size"] / 1024) for l in layers], "KB")

found = {}
# certs/brokers layers are tiny and near the end — scan small layers newest-first
for l in sorted(layers, key=lambda x: x["size"])[:12]:
    r = S.get(f"https://ghcr.io/v2/{REPO}/blobs/{l['digest']}", headers=H, timeout=120)
    data = r.content
    try:
        tf = tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)))
    except Exception:
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data))
        except Exception as ex:
            print("skip layer", l["digest"][:19], ex); continue
    for m in tf.getmembers():
        n = m.name.lstrip("./")
        if m.isfile() and (n.startswith("app/certs/") or n == "app/brokers.json"):
            b = tf.extractfile(m).read()
            dst = os.path.join(OUT, os.path.basename(n))
            open(dst, "wb").write(b)
            found[n] = len(b)
print("extracted:")
for k, v in sorted(found.items()):
    print(f"  {k}  {v} bytes")
# safe metadata only (no secret contents)
for f in sorted(os.listdir(OUT)):
    p = os.path.join(OUT, f)
    head = open(p, "rb").read(40)
    kind = "PEM cert" if b"BEGIN CERT" in head else "PEM key" if b"PRIVATE KEY" in head else "text"
    print(f"  {f}: {os.path.getsize(p)} B, {kind}")
