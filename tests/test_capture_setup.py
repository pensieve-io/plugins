"""Device setup imports one scoped key without authentication or local consent flags."""

import json
import stat

import capture_setup as setup
import pytest
from conversation_capture import profiles

OWNER = "353e0b53-8178-4a3c-8d40-a07414144741"
KEY = "synthetic-upload-only-key"


def download(tmp_path, client="codex", key=KEY):
    path = tmp_path / "download.json"
    path.write_text(
        json.dumps(
            {"version": 2, "profiles": [{"user_id": OWNER, "client": client, "upload_key": key}]}
        )
    )
    return path


def test_import_installs_private_scoped_key_and_preserves_other_client(tmp_path):
    destination = tmp_path / "private" / "capture.json"
    setup.install(download(tmp_path), destination)
    assert profiles(destination, "codex") == {OWNER: KEY}
    assert profiles(destination, "claude") == {}
    setup.install(download(tmp_path, "claude"), destination)
    setup.install(download(tmp_path, key=KEY + "-new"), destination)
    assert profiles(destination, "codex") == {OWNER: KEY + "-new"}
    assert profiles(destination, "claude") == {OWNER: KEY}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert "enabled" not in destination.read_text()


@pytest.mark.parametrize(
    "invalid",
    [
        {"version": 1, "profiles": []},
        {"version": 2, "profiles": []},
        {"version": 2, "profiles": [{"user_id": OWNER, "client": "codex", "upload_key": "short"}]},
    ],
)
def test_invalid_download_never_replaces_working_config(tmp_path, invalid):
    destination = tmp_path / "private" / "capture.json"
    source = download(tmp_path)
    setup.install(source, destination)
    original = destination.read_bytes()
    source.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        setup.install(source, destination)
    assert destination.read_bytes() == original


def test_symlink_or_insecure_existing_config_is_rejected(tmp_path):
    source = download(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(source)
    destination = tmp_path / "private" / "capture.json"
    with pytest.raises(OSError):
        setup.install(link, destination)
    setup.install(source, destination)
    destination.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        setup.install(source, destination)


def test_cli_never_prints_setup_contents(tmp_path, monkeypatch, capsys):
    source = download(tmp_path)
    destination = tmp_path / "private" / "capture.json"
    monkeypatch.setattr("sys.argv", ["setup", str(source), "--config", str(destination)])
    setup.main()
    assert KEY not in capsys.readouterr().out
    assert profiles(destination, "codex") == {OWNER: KEY}
