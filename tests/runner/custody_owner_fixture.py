"""Isolated retained session owner for custody end and stop-grace tests.

Usage: ``python -m tests.runner.custody_owner_fixture MODE SESSION SOCKET_DIR [HELPER_PID]``.
The owner records custody under ``OMNIGENT_DATA_DIR`` and writes ``ready-SESSION.json``.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from pathlib import Path

from omnigent.native.state_files import write_state_file
from omnigent.runner.custody import SessionCustody, make_custody_context

HOST_ID = "host_owner_fixture"
ENDPOINT = "http://127.0.0.1:38129"


def main() -> None:
    mode, session_id, socket_dir = sys.argv[1:4]
    base = Path(os.environ["OMNIGENT_DATA_DIR"])
    context = make_custody_context(
        session_id=session_id,
        runner_id="runner_" + session_id,
        host_id=HOST_ID,
        endpoint=ENDPOINT,
        workspace=str(base),
        native_env={},
        inference_path=None,
    )
    owner = SessionCustody.create(base, context)
    socket_path = Path(socket_dir) / (session_id + ".sock")
    listener = None
    if mode in {"dead-released", "dead-retained-socket", "dead-unretired-terminal"}:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        owner.publish_terminal("codex", "main", socket_path, os.getpid(), os.getpid())
        if mode != "dead-unretired-terminal":
            owner.publish_helper("codex_app_server", os.getpid())
    if mode in {"dead-helper", "dead-unretired-helper"}:
        owner.publish_helper("codex_app_server", int(sys.argv[4]))
    if mode == "dead-policy":
        policy = base / (session_id + "-policy.json")
        write_state_file(policy, {"policy": "synthetic-deny"})
        owner.publish_resource("tool_policy_bridge:" + session_id, policy)
    if mode in {"live", "slow-stop"}:
        owner.publish_helper("codex_app_server", os.getpid())

        def stop(_signum: int, _frame: object) -> None:
            if mode == "slow-stop":
                # Longer than the old fixed five-second host grace.
                time.sleep(6)
            owner.retire_helper("codex_app_server", owner.record.helpers["codex_app_server"])
            owner.finish()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
    write_state_file(
        base / ("ready-" + session_id + ".json"), owner.record.model_dump(mode="json")
    )
    if mode.startswith("dead-"):
        if listener is not None:
            listener.close()
            if mode == "dead-released":
                socket_path.unlink()
        if mode.startswith("dead-unretired-"):
            owner.finish()
        os._exit(0)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
