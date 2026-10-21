"""Optional local STT server. Install faster-whisper==1.2.1 separately.

Download (explicit consent): xdsh-speech download --model-dir PATH --approve-download
Serve: XDSH_STT_LOCAL_KEY=... xdsh-speech serve --model-dir PATH
Configure the Bot with XDSH_STT_LOCAL_URL=http://127.0.0.1:18081/transcribe,
XDSH_STT_LOCAL_PROTOCOL=raw, XDSH_STT_LOCAL_MODEL=small and the same key reference.
No Docker socket, automatic package installation or automatic downloads.
"""

from __future__ import annotations

import argparse
import hmac
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "serve", "worker"))
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--approve-download", action="store_true")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--audio", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.model_dir.expanduser().resolve()
    if args.action == "download":
        if not args.approve_download:
            parser.error("Model download requires --approve-download")
        try:
            download = importlib.import_module("faster_whisper.utils").download_model
        except ImportError:
            parser.error("Install the optional faster-whisper==1.2.1 package first")
        download("small", output_dir=str(root))
        print("Local small model downloaded. No transcription service started.")
        return
    if not (root / "model.bin").is_file():
        parser.error("Local model missing; explicitly approve a download first")
    if args.action == "worker":
        if args.audio is None:
            parser.error("Worker requires an audio file")
        whisper = importlib.import_module("faster_whisper").WhisperModel
        model = whisper(str(root), device=args.device, compute_type="int8", cpu_threads=2)
        segments, info = model.transcribe(str(args.audio), beam_size=1, vad_filter=True)
        if info.duration > 600:
            raise SystemExit("Audio exceeds ten minutes")
        text = ""
        for segment in segments:
            text += segment.text
            if len(text) > 20_000:
                raise SystemExit("Transcript exceeds limit")
        print(json.dumps({"text": text}, ensure_ascii=False))
        return
    key = os.environ.get("XDSH_STT_LOCAL_KEY", "")
    if not 24 <= len(key) <= 4096 or not key.isascii() or any(c.isspace() for c in key):
        parser.error("Set a separate XDSH_STT_LOCAL_KEY (24-4096 ASCII characters)")
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024-65535")
    gate = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            pass

        def do_POST(self) -> None:
            self.connection.settimeout(15)
            authorized = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(authorized, ("Bearer " + key).encode()):
                self.send_error(401)
                return
            if self.path != "/transcribe" or self.headers.get("Transfer-Encoding"):
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 10_000_000:
                self.send_error(413)
                return
            if not gate.acquire(blocking=False):
                self.send_error(429)
                return
            try:
                audio = self.rfile.read(length)
                if len(audio) != length:
                    raise ValueError("Incomplete audio")
                with tempfile.TemporaryDirectory(prefix="xdsh-stt-") as directory:
                    path = Path(directory) / "audio.ogg"
                    path.write_bytes(audio)
                    result = subprocess.run([
                        sys.executable, "-m", "xdsh.ops.speech", "worker", "--model-dir",
                        str(root), "--device", args.device, "--audio", str(path),
                    ], capture_output=True, timeout=240, check=True)
                if len(result.stdout) > 262_144:
                    raise ValueError("Oversized result")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(result.stdout)))
                self.end_headers()
                self.wfile.write(result.stdout)
            except (OSError, ValueError, subprocess.SubprocessError):
                self.send_error(502, "Local transcription failed; no cloud fallback")
            finally:
                gate.release()

    class Server(ThreadingHTTPServer):
        slots = threading.BoundedSemaphore(4)
        request_queue_size = 4

        def process_request(
            self, request: socket.socket | tuple[bytes, socket.socket], client_address: object,
        ) -> None:
            if not isinstance(request, socket.socket):
                return
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(
            self, request: socket.socket | tuple[bytes, socket.socket], client_address: object,
        ) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

        def handle_error(self, request: object, client_address: object) -> None:
            pass

    # Loopback only; operator chooses container isolation or a host service.
    server = Server(("127.0.0.1", args.port), Handler)
    print("XDSH local speech ready; model loads only during a request", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
