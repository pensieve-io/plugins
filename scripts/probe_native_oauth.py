"""Disposable Claude native OAuth + private plugin header probe; no real credentials."""

import json
import os
import pty
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

HOST_TOKEN = "synthetic-host-oauth-not-a-real-token"
HEADER_FILE = None
REGISTERED = False
OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
INSTALLATION = "193e0b53-8178-4a3c-8d40-a07414144741"
events = []
base = ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body, headers=None):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/.well-known/oauth-protected-resource"):
            self.reply(200, {"resource": base + "/mcp", "authorization_servers": [base]})
        elif path.startswith("/.well-known/oauth-authorization-server") or path.startswith(
            "/.well-known/openid-configuration"
        ):
            self.reply(
                200,
                {
                    "issuer": base,
                    "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token",
                    "registration_endpoint": base + "/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        elif path == "/authorize":
            params = parse_qs(urlsplit(self.path).query)
            callback = params["redirect_uri"][0]
            callback += ("&" if "?" in callback else "?") + urlencode(
                {"code": "synthetic-code", "state": params["state"][0]}
            )
            events.append(
                {"event": "authorization", "pkce": params.get("code_challenge_method") == ["S256"]}
            )
            self.send_response(302)
            self.send_header("Location", callback)
            self.end_headers()
        else:
            self.reply(405, {})

    def do_POST(self):
        global REGISTERED
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        path = urlsplit(self.path).path
        if path == "/register":
            body = json.loads(raw)
            self.reply(
                201, {**body, "client_id": "synthetic-client", "token_endpoint_auth_method": "none"}
            )
            return
        if path == "/token":
            events.append(
                {"event": "token_exchange", "grant": parse_qs(raw.decode()).get("grant_type")}
            )
            self.reply(
                200,
                {
                    "access_token": HOST_TOKEN,
                    "refresh_token": "synthetic-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
            return
        proof_value = (
            json.loads(HEADER_FILE.read_text())["X-Pensieve-Plugin"] if HEADER_FILE.exists() else ""
        )
        if path in {"/hooks/connection", "/hooks/briefing"}:
            if (
                not REGISTERED
                or self.headers.get("Authorization") != "Bearer " + proof_value.split(" ", 1)[-1]
            ):
                self.reply(403, {})
            elif path == "/hooks/connection":
                self.reply(
                    200,
                    {
                        "user_id": OWNER,
                        "client": "claude",
                        "context_id": None,
                        "installation_id": INSTALLATION,
                    },
                )
            else:
                self.reply(
                    200,
                    {
                        "hookSpecificOutput": {
                            "hookEventName": json.loads(raw)["event"],
                            "additionalContext": "NATIVE_OAUTH_HOOK_BRIEFING",
                        }
                    },
                )
            return
        if path != "/mcp":
            self.reply(404, {})
            return
        request = json.loads(raw)
        authenticated = self.headers.get("Authorization") == "Bearer " + HOST_TOKEN
        proof = bool(proof_value) and self.headers.get("X-Pensieve-Plugin") == proof_value
        events.append(
            {
                "event": "mcp",
                "method": request.get("method"),
                "native_oauth": authenticated,
                "plugin_proof": proof,
            }
        )
        if not authenticated:
            self.reply(
                401,
                {"error": "unauthorized"},
                {
                    "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'
                },
            )
            return
        REGISTERED = REGISTERED or proof
        method = request.get("method")
        if "id" not in request:
            self.reply(202, {})
            return
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "native-oauth-probe", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "ordinary",
                        "description": "Synthetic fixture",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        else:
            result = {"content": [{"type": "text", "text": "Synthetic authenticated result"}]}
        self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})


def run():
    global base, HEADER_FILE
    with tempfile.TemporaryDirectory(prefix="pr1085-native-oauth-") as directory:
        root = Path(directory)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        base = f"http://127.0.0.1:{server.server_port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        plugin = root / "plugin"
        shutil.copytree(Path(__file__).resolve().parents[1] / "pensieve", plugin)
        (plugin / ".claude-plugin/plugin.json").write_text(
            json.dumps({"name": "pensieve-auth-fixture", "version": "0.0.1"})
        )
        # Login/discovery probe: run the actual hook explicitly after authentication.
        shutil.rmtree(plugin / "hooks")
        manifest = json.loads((plugin / ".mcp.json").read_text())
        manifest["mcpServers"]["pensieve"]["url"] = base + "/mcp"
        manifest["mcpServers"]["pensieve"]["headersHelper"] = (
            "HOME="
            + shlex.quote(str(root))
            + " "
            + manifest["mcpServers"]["pensieve"]["headersHelper"]
        )
        (plugin / ".mcp.json").write_text(json.dumps(manifest))
        HEADER_FILE = root / ".config/pensieve/mcp-headers-claude.json"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "ANTHROPIC", "AWS_", "OTEL_"))
        }
        env.update(
            {
                "CLAUDE_CONFIG_DIR": str(root / "config"),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_ERROR_REPORTING": "1",
            }
        )
        common = ["claude", "--setting-sources", "", "--plugin-dir", str(plugin)]
        output = queue.Queue()
        master, slave = pty.openpty()
        login = subprocess.Popen(
            [*common, "mcp", "login", "plugin:pensieve-auth-fixture:pensieve", "--no-browser"],
            cwd=root,
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def read():
            for line in login.stdout:
                output.put(line)

        threading.Thread(target=read, daemon=True).start()
        seen = []
        authorized = False
        deadline = time.monotonic() + 35
        try:
            while time.monotonic() < deadline:
                if login.poll() is not None and output.empty():
                    break
                try:
                    line = output.get(timeout=0.25)
                except queue.Empty:
                    continue
                seen.append(line)
                match = re.search(re.escape(base) + r"/authorize[^\s\x1b]+", line)
                if match and not authorized:
                    url = match.group(0)
                    try:
                        with urlopen(url, timeout=5) as response:
                            response.read(1024)
                    except Exception as exc:
                        events.append({"event": "callback_error", "kind": type(exc).__name__})
                    params = parse_qs(urlsplit(url).query)
                    callback = (
                        params["redirect_uri"][0]
                        + "?"
                        + urlencode({"code": "synthetic-code", "state": params["state"][0]})
                    )
                    os.write(master, (callback + "\n").encode())
                    authorized = True
            if login.poll() is None:
                login.terminate()
            login.wait(timeout=5)
            # Native `mcp get` connects using the host's saved OAuth grant.
            check = subprocess.run(
                [*common, "mcp", "get", "plugin:pensieve-auth-fixture:pensieve"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=25,
            )
            hook = subprocess.run(
                [
                    sys.executable,
                    str(plugin / "scripts/context_briefing.py"),
                    "--client",
                    "claude",
                    "--config",
                    str(root / ".config/pensieve/capture.json"),
                    "--endpoint",
                    base + "/hooks/briefing",
                    "--connection-endpoint",
                    base + "/hooks/connection",
                ],
                input=json.dumps(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "923bbd76-d864-4b8f-b252-c2b7c3692492",
                    }
                ),
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            device_token = (
                json.loads(HEADER_FILE.read_text())["X-Pensieve-Plugin"].split(" ", 1)[1]
                if HEADER_FILE.exists()
                else "missing"
            )
            report = {
                "packaged_hook_briefing": "NATIVE_OAUTH_HOOK_BRIEFING" in hook.stdout,
                "hook_exit": hook.returncode,
                "login_exit": login.returncode,
                "browser_authorization_exercised": authorized,
                "get_exit": check.returncode,
                "events": events,
                "native_oauth_and_private_header": any(
                    e.get("native_oauth") and e.get("plugin_proof") for e in events
                ),
            }
            if login.returncode:
                report["login_output"] = (
                    "".join(seen)
                    .replace(HOST_TOKEN, "[redacted]")
                    .replace(device_token, "[redacted]")
                )
            report["get_output"] = check.stdout.replace(HOST_TOKEN, "[redacted]").replace(
                device_token, "[redacted]"
            )
            print(json.dumps(report, indent=2))
            if not (
                report["native_oauth_and_private_header"]
                and report["packaged_hook_briefing"]
                and login.returncode == check.returncode == hook.returncode == 0
            ):
                raise SystemExit(1)
        finally:
            if login.poll() is None:
                login.kill()
                login.wait()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    run()
