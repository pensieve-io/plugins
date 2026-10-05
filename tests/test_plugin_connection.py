"""Installation ownership, private header generation and explicit recovery."""

import json
import os
import subprocess
from pathlib import Path

import capture_config as config
import plugin_connection as connection
import plugin_headers
import pytest

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
INSTALLATION = "193e0b53-8178-4a3c-8d40-a07414144741"


def identity(client="codex", **extra):
    return {
        "user_id": OWNER,
        "installation_id": INSTALLATION,
        "client": client,
        "context_id": None,
        **extra,
    }


def test_packaged_helper_bootstraps_before_any_hook(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "pensieve/.mcp.json").read_text())["mcpServers"]["pensieve"]
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_PLUGIN_ROOT": str(root / "pensieve")}
    for client, field in (("codex", "http_headers_helper"), ("claude", "headersHelper")):
        result = subprocess.run(
            manifest[field],
            shell=True,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        path = tmp_path / ".config/pensieve/capture.json"
        headers = json.loads(result.stdout)
        assert headers == plugin_headers.prepare_headers(path, client)
        assert headers[plugin_headers.HEADER].startswith(client + " pcap_")
        assert connection.header_path(path, client).stat().st_mode & 0o777 == 0o600
        assert connection.header_path(path, client).parent.stat().st_mode & 0o777 == 0o700
        assert not path.exists()  # Native auth hasn't authorized an owner yet.


def test_generated_manifest_has_no_drift():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "build_mcp_config", root / "scripts/build_mcp_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert json.loads((root / "pensieve/.mcp.json").read_text()) == module.manifest()


@pytest.mark.parametrize("status", [401, 403, 503, "unavailable"])
def test_failure_never_rotates_proof_or_installs_credentials(tmp_path, status):
    path = tmp_path / "private/capture.json"
    token = connection.installation_token(path, "codex")
    assert (
        connection.connection(
            path, "codex", token, connection.CONNECTION_ENDPOINT, lambda *a, **k: (status, None)
        )
        is None
    )
    assert connection.installation_token(path, "codex") == token
    assert not path.exists()
    assert token not in connection.connect_message("codex")


@pytest.mark.parametrize(
    "extra",
    [{"client": "claude"}, {"user_id": "bad"}, {"installation_id": "bad"}, {"context_id": 1}],
)
def test_identity_response_cannot_reassign_client_or_scope(tmp_path, extra):
    path = tmp_path / "private/capture.json"
    token = connection.installation_token(path, "codex")
    with pytest.raises(ValueError):
        connection.connection(
            path,
            "codex",
            token,
            connection.CONNECTION_ENDPOINT,
            lambda *a, **k: (200, identity(**extra)),
        )
    assert not path.exists()


@pytest.mark.parametrize("version", [2, 3])
def test_authenticated_upgrade_discards_obsolete_profiles(tmp_path, version):
    path = tmp_path / "private/capture.json"
    old = {"user_id": OWNER, "upload_key": "legacy-upload-only-key"}
    if version == 3:
        old.update(client="codex", installation_id=None, runtime="unknown", host_version="")
    config.save_private_json(path, {"version": version, "profiles": [old]})
    token = connection.installation_token(path, "codex")
    profile = connection.connection(
        path, "codex", token, connection.CONNECTION_ENDPOINT, lambda *a, **k: (200, identity())
    )
    assert config.load_config(path, "codex")["profiles"] == [profile]
    assert profile["upload_key"] == token


def test_reset_rotates_proof_and_fences_dormant_spools(tmp_path):
    path = tmp_path / "private/capture.json"
    state = tmp_path / "state"
    token = connection.installation_token(path, "codex")
    profile = connection.connection(
        path, "codex", token, connection.CONNECTION_ENDPOINT, lambda *a, **k: (200, identity())
    )
    other = {**profile, "client": "claude"}
    config.install_profile(path, other)
    config.observe_credentials(state, "codex", {OWNER: token})
    connection.reset_connection(path, "codex", state)
    assert connection.installation_token(path, "codex") != token
    assert config.load_config(path, "codex")["profiles"] == [other]
    assert OWNER in config.credential_revocations(state, "codex")


@pytest.mark.parametrize("unsafe", ["public", "symlink"])
def test_private_proof_refuses_unsafe_storage(tmp_path, unsafe):
    path = tmp_path / "private/capture.json"
    token = connection.installation_token(path, "codex")
    target = connection.header_path(path, "codex")
    if unsafe == "public":
        target.chmod(0o644)
    else:
        target.rename(tmp_path / "target")
        target.symlink_to(tmp_path / "target")
    with pytest.raises((ValueError, OSError)):
        connection.installation_token(path, "codex")
    assert token
