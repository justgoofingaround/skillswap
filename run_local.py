"""Run SkillSwap locally: python3 server.py  ->  http://localhost:8787

Serves public/ and the same API code Vercel runs (api/index.py).
Uses a local SQLite file unless DATABASE_URL points at Postgres.
"""
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from api.index import handler as ApiHandler, init_db  # noqa: E402

PUBLIC = ROOT / "public"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
         ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}


class LocalHandler(ApiHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.route("GET")
        path = self.path.split("?")[0]
        f = PUBLIC / ("index.html" if path in ("/", "") else path.lstrip("/"))
        try:
            f = f.resolve()
            f.relative_to(PUBLIC)
        except (ValueError, OSError):
            return self.send_json(404, {"error": "Not found"})
        if not f.is_file():
            f = PUBLIC / "index.html"
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    init_db()
    print(f"SkillSwap running on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), LocalHandler).serve_forever()
