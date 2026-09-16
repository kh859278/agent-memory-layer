import json
import time
import urllib.request

URLS = [
    ("npmjs(完整)", "https://registry.npmjs.org/@deepseek-ai%2Fdsh", "application/json"),
    ("npmjs(精简)", "https://registry.npmjs.org/@deepseek-ai%2Fdsh",
     "application/vnd.npm.install-v1+json"),
    ("npmmirror", "https://registry.npmmirror.com/@deepseek-ai%2Fdsh", "application/json"),
]

for label, url, accept in URLS:
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aml", "Accept": accept})
        with urllib.request.urlopen(req, timeout=30) as f:
            data = f.read()
        payload = json.loads(data)
        tags = payload.get("dist-tags")
        print(f"  {label}: {len(data)/1024:.0f} KB, {time.time()-start:.1f}s, dist-tags={tags}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  {label}: {type(e).__name__} {str(e)[:60]} ({time.time()-start:.1f}s)", flush=True)
