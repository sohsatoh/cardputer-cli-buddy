#!/usr/bin/env python3
"""PermissionRequest hook: relays the dialog to buddyd and answers with the device's decision.

Anything other than an explicit device decision prints nothing, which leaves the native dialog in charge.
"""
import http.client
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import quote

WAIT_SEC = 25
# AskUserQuestion is relayed by the mod itself; a plan should be read in full at the terminal before approval.
NOT_ON_DEVICE = ("AskUserQuestion", "ExitPlanMode")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("buddyd", timeout=WAIT_SEC + 10)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def socket_path() -> str:
    home = os.environ.get("CARDBUDDY_HOME") or Path.home() / ".cardbuddy"
    return str(Path(home) / "buddyd.sock")


def request(method: str, path: str, body=None):
    conn = UnixHTTPConnection(socket_path())
    try:
        data = None if body is None else json.dumps(body).encode()
        headers = {} if data is None else {"Content-Type": "application/json"}
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        return r.status, json.loads(r.read() or b"null")
    finally:
        conn.close()


def decide(e: dict):
    if e.get("tool_name") in NOT_ON_DEVICE:
        return None
    status, body = request("POST", "/perm", {"sid": e.get("session_id"), "tool": e.get("tool_name"), "input": e.get("tool_input")})
    req = body.get("req") if status == 200 and isinstance(body, dict) else None
    if not isinstance(req, str):
        return None
    while True:
        status, body = request("GET", f"/perm/{quote(req, safe='')}?timeout={WAIT_SEC}")
        if status != 200 or not isinstance(body, dict):
            return None
        if body.get("decision") in ("allow", "deny"):
            return body["decision"]
        if body:
            return None


def output(decision: str) -> str:
    d = {"behavior": "allow"} if decision == "allow" else {"behavior": "deny", "message": "Denied on Cardputer"}
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}})


def main() -> int:
    try:
        decision = decide(json.load(sys.stdin))
    except Exception:
        return 0
    if decision is not None:
        print(output(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
