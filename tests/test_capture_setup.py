"""Local setup exercises PKCE without real accounts, tokens or network services."""

import base64
import hashlib
import socket
import stat
import threading
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import capture_setup as setup
import pytest
from conversation_capture import profiles

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
KEY = "synthetic-upload-only-key"


def test_setup_uses_own_pkce_flow_and_never_persists_oauth(tmp_path, monkeypatch, capsys):
    path = tmp_path / "capture" / "capture.json"
    requests = []
    callbacks = []
    metadata = {
        "issuer": "https://auth.example.test/auth/v1",
        "code_challenge_methods_supported": ["S256"],
        **{
            name: "https://auth.example.test/" + name
            for name in ("registration_endpoint", "authorization_endpoint", "token_endpoint")
        },
    }
    issued = {"key": {"id": str(uuid.uuid4()), "user_id": OWNER}, "upload_key": KEY}

    def request(url, **kwargs):
        requests.append((url, kwargs))
        if "well-known" in url:
            return metadata
        if url == metadata["registration_endpoint"]:
            assert kwargs["body"]["token_endpoint_auth_method"] == "none"
            assert kwargs["body"]["grant_types"] == ["authorization_code"]
            return {"client_id": "synthetic-client"}
        if url == metadata["token_endpoint"]:
            verifier = kwargs["body"]["code_verifier"]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            assert callbacks[0]["code_challenge"] == [challenge]
            assert kwargs["body"]["code"] == "fixture-code"
            return {"access_token": "synthetic-ephemeral-oauth", "refresh_token": "discard-me"}
        assert kwargs["token"] == "synthetic-ephemeral-oauth"
        return issued

    def open_browser(url):
        query = parse_qs(urlsplit(url).query)
        callbacks.append(query)
        assert query["scope"] == ["openid email profile"]
        target = query["redirect_uri"][0]
        idle = socket.create_connection(("127.0.0.1", urlsplit(target).port))

        def callback():
            target = query["redirect_uri"][0]
            # A wrong state must not terminate the local receiver or exchange a code.
            from urllib.error import HTTPError

            with pytest.raises(HTTPError):
                urlopen(target + "?" + urlencode({"state": "wrong", "code": "intruder"})).close()
            urlopen(
                target + "?" + urlencode({"state": query["state"][0], "code": "fixture-code"}),
                timeout=3,
            ).close()
            idle.close()

        thread = threading.Thread(target=callback)
        thread.start()
        return True

    monkeypatch.setattr(setup, "request_json", request)
    monkeypatch.setattr(setup.webbrowser, "open", open_browser)
    setup.enable(path, "Laptop")
    assert profiles(path) == {OWNER: KEY}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "oauth" not in path.read_text() and "discard-me" not in path.read_text()
    assert KEY not in capsys.readouterr().out


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://auth.example.test/token",
        "https://evil.example.test/token",
        "https://user:password@auth.example.test/token",
    ],
)
def test_setup_rejects_untrusted_oauth_endpoints(endpoint):
    metadata = {"issuer": "https://auth.example.test", "registration_endpoint": endpoint}
    with pytest.raises(ValueError):
        setup.oauth_endpoints(metadata)


def test_cancelled_enable_never_authenticates_or_changes_config(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["setup", "--config", str(tmp_path / "capture.json"), "enable"])
    monkeypatch.setattr("builtins.input", lambda _: "n")
    monkeypatch.setattr(setup, "sign_in", lambda: pytest.fail("No sign-in before opt-in"))
    setup.main()
    assert not (tmp_path / "capture.json").exists()


def test_duplicate_enable_revokes_only_new_unused_key(tmp_path, monkeypatch):
    path = tmp_path / "private" / "capture.json"
    setup.save_config(path, {"version": 1, "profiles": [{"user_id": OWNER, "upload_key": KEY}]})
    monkeypatch.setattr(setup, "sign_in", lambda: "synthetic-oauth")
    calls = []
    key_id = str(uuid.uuid4())

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return {"key": {"id": key_id, "user_id": OWNER}, "upload_key": "new-unused-key"}

    monkeypatch.setattr(setup, "request_json", request)
    with pytest.raises(ValueError, match="already enabled"):
        setup.enable(path, "Laptop")
    assert calls[-1][0].endswith(key_id) and calls[-1][1]["method"] == "DELETE"
    assert profiles(path) == {OWNER: KEY}


def test_disable_removes_credentials_without_sign_in(tmp_path, monkeypatch):
    path = tmp_path / "private" / "capture.json"
    setup.save_config(path, {"version": 1, "profiles": [{"user_id": OWNER, "upload_key": KEY}]})
    monkeypatch.setattr("sys.argv", ["setup", "--config", str(path), "disable"])
    monkeypatch.setattr(setup, "sign_in", lambda: pytest.fail("Local disable needs no sign-in"))
    setup.main()
    assert profiles(path) == {}
