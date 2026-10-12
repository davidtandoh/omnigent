"""Read the public contract from an isolated installed CLI and synthetic endpoint."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def test_launch_contract_cli_during_trace_outage(tmp_path: Path) -> None:
    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.path == "/health"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
        "OMNIGENT_SKIP_UPDATE_CHECK": "1",
    }
    try:
        result = subprocess.run(
            [
                str(Path(sys.executable).parent / "omnigent"),
                "runtime",
                "launch-contract",
                "--server",
                endpoint,
                "--json",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        receipt = json.loads(result.stdout)
        assert receipt["availability"] == "ready"
        assert receipt["endpoint"] == endpoint
        assert receipt["runtime"]["generation"].startswith("sha256:")
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in receipt["client_environment"]
        assert not (tmp_path / "data" / "local_server.pid").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
