#!/usr/bin/env python3
"""Standalone data download dashboard and API server."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from engine.download.manager import DataDownloadManager, DownloadStartRequest
else:
    from .manager import DataDownloadManager, DownloadStartRequest


STATIC_DIR = Path(__file__).resolve().parent / "static"


class DownloadHandler(BaseHTTPRequestHandler):
    manager = DataDownloadManager()

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/api/download/status":
                run_id = _first(query.get("run_id"))
                self.send_json(200, self.manager.status(run_id))
                return
            if path == "/api/download/runs":
                self.send_json(200, self.manager.list_runs())
                return
            if path == "/api/download/quality":
                run_id = _first(query.get("run_id"))
                refresh = _first(query.get("refresh")) in {"1", "true", "yes"}
                self.send_json(200, {"ok": True, "quality": self.manager.quality_summary(run_id, refresh=refresh)})
                return
            if path == "/api/health":
                self.send_json(200, {"ok": True, "service": "data-download"})
                return
            self.serve_static(path)
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            payload = self.read_json_body()
            if path == "/api/download/start":
                self.send_json(200, self.manager.start(DownloadStartRequest.from_payload(payload)))
                return
            if path == "/api/download/pause":
                self.send_json(200, self.manager.pause(_clean_run_id(payload)))
                return
            if path == "/api/download/resume":
                self.send_json(200, self.manager.resume(_clean_run_id(payload)))
                return
            if path == "/api/download/register":
                run_id = _clean_run_id(payload)
                if not run_id:
                    raise ValueError("run_id is required")
                run_dir = self.manager.run_dir(run_id)
                if not run_dir:
                    raise ValueError(f"download manifest not found for run_id={run_id}")
                manifest = self.manager.status(run_id).get("manifest", {})
                quality = self.manager.quality_summary(run_id, refresh=True)
                dataset_id = str(payload.get("dataset_id") or "").strip() or None
                self.send_json(200, {"ok": True, "record": self.manager.register_completed_run(run_id, run_dir, manifest, quality, dataset_id)})
                return
            self.send_json(404, {"ok": False, "error": f"unknown endpoint: {path}"})
        except Exception as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})

    def read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def serve_static(self, path: str) -> None:
        if path == "/":
            target = STATIC_DIR / "index.html"
        else:
            target = (STATIC_DIR / path.lstrip("/")).resolve()
            if STATIC_DIR.resolve() not in target.parents:
                self.send_json(403, {"ok": False, "error": "forbidden"})
                return
        if not target.exists() or not target.is_file():
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _clean_run_id(payload: dict[str, object]) -> str | None:
    value = payload.get("run_id")
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the personal quant data download dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DownloadHandler)
    print(f"data download dashboard: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
