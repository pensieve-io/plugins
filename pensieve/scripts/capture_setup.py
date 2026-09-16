"""Configure capture locally using a separate browser OAuth PKCE grant.

No host credentials are read. OAuth tokens live only in this process; the
private config contains a revocable upload-only key, never a read credential.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from conversation_capture import (
    CONFIG_PATH,
    MAX_CONFIG_BYTES,
    NoRedirects,
    encoded,
    private_directory,
    private_file,
    profiles,
)

SERVICE = "https://mcp.pensieve.uk"
KEYS = "/hooks/conversation-capture/keys"


def request_json(url, *, method="GET", body=None, token=None, form=False):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
        data = urlencode(body).encode() if form else encoded(body)
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(ProxyHandler({}), NoRedirects()).open(
            Request(url, data=data, headers=headers, method=method), timeout=20
        ) as response:
            if response.status == 204:
                return None
            raw = response.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError("Setup response exceeds its size limit")
            return json.loads(raw)
    except HTTPError as error:
        # Response bodies and request URLs can carry credentials. Never echo them.
        raise RuntimeError(f"Capture setup request failed (HTTP {error.code})") from None


def oauth_endpoints(metadata):
    issuer = urlsplit(metadata["issuer"])
    if issuer.scheme != "https" or not issuer.hostname or issuer.username or issuer.password:
        raise ValueError("Invalid authorisation service")
    endpoints = {}
    for name in ("registration_endpoint", "authorization_endpoint", "token_endpoint"):
        value = metadata[name]
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != issuer.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid authorisation endpoint")
        endpoints[name] = value
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        raise ValueError("Authorisation service must support PKCE S256")
    return endpoints


def sign_in():
    metadata = request_json(SERVICE + "/.well-known/oauth-authorization-server")
    endpoints = oauth_endpoints(metadata)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # The callback URL contains a one-time authorisation code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = (
                parsed.path == "/callback"
                and query.get("state") == [state]
                and (len(query.get("code", [])) == 1 or "error" in query)
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                b"Return to the terminal to finish setup." if valid else b"Invalid callback."
            )
            if valid:
                result.update(code=query.get("code", [None])[0], denied="error" in query)

    with ThreadingHTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        client = request_json(
            endpoints["registration_endpoint"],
            method="POST",
            body={
                "client_name": "Pensieve conversation capture setup",
                "redirect_uris": [redirect],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        url = (
            endpoints["authorization_endpoint"]
            + "?"
            + urlencode(
                {
                    "client_id": client["client_id"],
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": "openid email profile",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        if not webbrowser.open(url):
            raise RuntimeError(
                "Could not open a browser; run setup in an interactive desktop terminal"
            )
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if not result or result.get("denied") or not result.get("code"):
            raise RuntimeError("Sign-in was cancelled or timed out")
        tokens = request_json(
            endpoints["token_endpoint"],
            method="POST",
            form=True,
            body={
                "client_id": client["client_id"],
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": redirect,
                "code_verifier": verifier,
            },
        )
        return tokens["access_token"]


def save_config(path, value):
    private_directory(path.parent)
    raw = encoded(value)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Capture config exceeds its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enable(path, label):
    # Validate any existing file before starting sign-in or issuing credentials.
    existing = profiles(path)
    token = sign_in()
    issued = request_json(SERVICE + KEYS, method="POST", body={"label": label}, token=token)
    key_id = str(uuid.UUID(issued["key"]["id"]))
    try:
        owner = str(uuid.UUID(issued["key"]["user_id"]))
        if owner in existing:
            raise ValueError("Capture is already enabled for this account; disable it first")
        values = (
            json.loads(private_file(path, MAX_CONFIG_BYTES))
            if path.exists()
            else {"version": 1, "profiles": []}
        )
        values["profiles"].append({"user_id": owner, "upload_key": issued["upload_key"]})
        save_config(path, values)
    except Exception:
        request_json(SERVICE + KEYS + "/" + key_id, method="DELETE", token=token)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(os.environ.get("PENSIEVE_CAPTURE_CONFIG", CONFIG_PATH))
    )
    commands = parser.add_subparsers(dest="command", required=True)
    enable_parser = commands.add_parser("enable", help="Sign in and opt in on this computer")
    enable_parser.add_argument("--label", default=socket.gethostname())
    commands.add_parser("status", help="Show whether this computer has capture enabled")
    commands.add_parser("disable", help="Stop this computer uploading; saved work remains")
    commands.add_parser("keys", help="Sign in and list your active upload keys")
    revoke = commands.add_parser(
        "revoke", help="Sign in and revoke an upload key, including a lost computer"
    )
    revoke.add_argument("key_id", type=uuid.UUID)
    args = parser.parse_args()
    try:
        if args.command == "enable":
            print(
                "Capture saves new visible work to the selected context, shared with all its members.\n"
                "No selected context means no upload. Saved portions expire after 90 days.\n"
                "There is no private mode or conversation delete action."
            )
            if (
                input("Enable capture for the account you sign in with? [y/N] ").strip().lower()
                != "y"
            ):
                print("Capture unchanged.")
                return
            enable(args.config, args.label)
            print("Capture enabled. Start or resume a work session with the Pensieve plugin.")
        elif args.command == "status":
            print(f"Capture enabled for {len(profiles(args.config))} account(s).")
        elif args.command == "disable":
            profiles(args.config)  # Refuse malformed or unsafe files before replacing them.
            save_config(args.config, {"version": 1, "profiles": []})
            print(
                "Capture disabled on this computer. Saved work remains; use keys/revoke to revoke credentials."
            )
        else:
            token = sign_in()
            if args.command == "revoke":
                request_json(SERVICE + KEYS + "/" + str(args.key_id), method="DELETE", token=token)
                print("Capture key revoked.")
            else:
                for key in request_json(SERVICE + KEYS, token=token)["keys"]:
                    print(
                        f"{key['id']}  {key['label']}  last upload: {key['last_used_at'] or 'never'}"
                    )
    except (OSError, ValueError, RuntimeError, KeyError, EOFError):
        # In particular, never print OAuth response bodies, URLs or exception reprs.
        print(
            "Capture setup failed. Check sign-in, connectivity and private config permissions.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
