import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "perm_hook.py"
STDIN = {
    "session_id": "sid-1",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf ./build", "description": "消す"},
    "permission_suggestions": [],
}


class FakeBuddyd(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path, script):
        if os.path.exists(path):
            os.unlink(path)
        self.script = list(script)
        self.seen = []
        super().__init__(path, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        self.server.seen.append((self.command, self.path, body))
        status, out = self.server.script.pop(0) if self.server.script else (200, {})
        raw = out if isinstance(out, bytes) else json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _answer


class PermHookTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cb", dir=os.environ.get("TMPDIR", "/tmp"))

    def run_hook(self, script, stdin=STDIN):
        server = FakeBuddyd(os.path.join(self.home, "buddyd.sock"), script) if script is not None else None
        if server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            p = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(stdin) if isinstance(stdin, dict) else stdin,
                capture_output=True,
                text=True,
                timeout=20,
                env={**os.environ, "CARDBUDDY_HOME": self.home},
            )
        finally:
            if server:
                server.shutdown()
                server.server_close()
        return p, (server.seen if server else [])

    def test_allow_after_empty_waits(self):
        p, seen = self.run_hook([(200, {"req": "r1"}), (200, {}), (200, {}), (200, {"decision": "allow"})])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(
            json.loads(p.stdout),
            {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}},
        )
        self.assertEqual(seen[0], ("POST", "/perm", {"sid": "sid-1", "tool": "Bash", "input": STDIN["tool_input"]}))
        self.assertEqual([s[1] for s in seen[1:]], ["/perm/r1?timeout=25"] * 3)

    def test_deny(self):
        p, _ = self.run_hook([(200, {"req": "r1"}), (200, {"decision": "deny"})])
        self.assertEqual(
            json.loads(p.stdout),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": "Denied on Cardputer"},
                }
            },
        )

    def test_silent_cases(self):
        cases = {
            "released": [(200, {"req": "r1"}), (200, {"released": True})],
            "unknown req": [(200, {"req": "r1"}), (404, {"error": "unknown req"})],
            "unknown session": [(404, {"error": "unknown session"})],
            "server error": [(200, {"req": "r1"}), (500, {"error": "x"})],
            "not json": [(200, {"req": "r1"}), (200, b"<html>")],
            "allow-ish": [(200, {"req": "r1"}), (200, {"decision": "Allow"})],
            "decision not str": [(200, {"req": "r1"}), (200, {"decision": ["allow"]})],
            "no req": [(200, {})],
        }
        for name, script in cases.items():
            with self.subTest(name):
                p, _ = self.run_hook(script)
                self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_no_daemon(self):
        p, _ = self.run_hook(None)
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_bad_stdin(self):
        p, seen = self.run_hook([], stdin="not json")
        self.assertEqual((p.returncode, p.stdout, seen), (0, "", []))

    def test_tools_kept_off_the_device(self):
        for tool in ("AskUserQuestion", "ExitPlanMode"):
            with self.subTest(tool):
                p, seen = self.run_hook([], stdin={**STDIN, "tool_name": tool, "tool_input": {}})
                self.assertEqual((p.returncode, p.stdout, seen), (0, "", []))

    def test_req_is_quoted_in_path(self):
        _, seen = self.run_hook([(200, {"req": "a/b?c"}), (200, {"released": True})])
        self.assertEqual(seen[1][1], "/perm/a%2Fb%3Fc?timeout=25")


if __name__ == "__main__":
    unittest.main()
