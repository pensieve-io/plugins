"""Credentials are masked before capture is queued or uploaded; ordinary IDs survive."""

from __future__ import annotations

import base64
import json
import time

import conversation_capture as capture
import pytest
from secret_masking import MASK, mask_secrets

# Synthetic format fixtures, assembled at runtime so no credential-shaped literal
# sits in the source. None of them is a working credential.
KEY_MARKER = " ".join(["RSA", "PRIVATE", "KEY"])


def jwt_part(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


SAMPLES = [
    "ghp_" + "A" * 36,
    "github_pat_" + "b" * 82,
    "glpat-" + "c" * 20,
    "AKIA" + "A" * 16,
    "ASIA" + "B" * 16,
    "AIza" + "d" * 35,
    "ya29." + "e" * 50,
    "sk-ant-api03-" + "f" * 93 + "AA",
    "sk-proj-" + "g" * 140,
    "sk-or-v1-" + "0" * 64,
    "sk_live_" + "h" * 24,
    "rk_test_" + "i" * 30,
    "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-" + "j" * 24,
    "xapp-1-" + "k" * 40,
    "https://hooks.slack.com/services/T12345678/B12345678/" + "l" * 24,
    "npm_" + "m" * 36,
    "pypi-AgEIcHlwaS5vcmc" + "n" * 60,
    "SG." + "o" * 22 + "." + "p" * 43,
    "key-" + "a" * 32,
    "hvs." + "q" * 30,
    "dop_v1_" + "b" * 64,
    "hf_" + "r" * 34,
    "pcap_" + "s" * 43,
    "dp.st.prd_backend." + "t" * 40,
    "sbp_" + "c" * 40,
    "sb_secret_" + "u" * 30,
    "phx_" + "v" * 43,
    jwt_part({"alg": "HS256"}) + "." + jwt_part({"sub": "fixture"}) + "." + "w" * 43,
    f"-----BEGIN {KEY_MARKER}-----\n" + "fixture" * 20 + f"\n-----END {KEY_MARKER}-----",
]


@pytest.mark.parametrize("value", SAMPLES)
def test_credential_formats_are_masked_deterministically(value):
    assert mask_secrets(f"before {value} after") == f"before {MASK} after"
    assert mask_secrets(mask_secrets(value)) == MASK


def test_ordinary_identifiers_hashes_code_urls_and_references_survive():
    benign = [
        "01a1c94f-33c1-7723-a284-30f5198f0497",
        "550e8400-e29b-41d4-a716-446655440000",
        "a" * 40,
        "b" * 64,
        "session_01M6N70ZNZHJ38FZ7E77KBT4TH",
        "pk_live_" + "q" * 24,
        "phc_" + "x" * 43,
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfixture user@host",
        "https://example.com/items/550e8400-e29b-41d4-a716-446655440000?cursor=abc123def456",
        'const token_count=32000; const key="abcdefghijklm"; const sessionId="1234567890";',
        "API_KEY=$MY_KEY",
        "password=${PASSWORD}",
        "access_token=process.env.TOKEN",
        "api_key: <your-key>",
        f"password: {MASK}",
        "token: true",
        "token=None",
        "token = get_token(scope)",
        "password = prompt_for_password()",
    ]
    for value in benign:
        assert mask_secrets(value) == value
    ids = {"sessionId": benign[0], "trace_id": benign[1], "sha256": benign[3], "api_key": "${KEY}"}
    assert mask_secrets(json.dumps(ids)) == json.dumps(ids)


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "apiKey",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "NANGO_SECRET_KEY",
        "client_secret",
        "password",
        "accessToken",
        "upload_key",
    ],
)
def test_credential_names_mask_opaque_values(name):
    uuid_value = "550e8400-e29b-41d4-a716-446655440000"
    assert mask_secrets(f'{name}="hunter22"') == f'{name}="{MASK}"'
    assert mask_secrets(f"{name}={uuid_value}") == f"{name}={MASK}"
    assert json.loads(mask_secrets(json.dumps({name: uuid_value}))) == {name: MASK}


def test_headers_urls_and_quoted_values_keep_their_structure():
    assert mask_secrets("Authorization: Bearer opaque-token-123") == f"Authorization: Bearer {MASK}"
    assert mask_secrets('password="a long password with spaces" tail') == f'password="{MASK}" tail'
    assert mask_secrets("password='hunter2' tail") == f"password='{MASK}' tail"
    assert mask_secrets("postgresql://app:" + "s3cret-pass" + "@db.internal:5432/main") == (
        f"postgresql://app:{MASK}@db.internal:5432/main"
    )
    assert (
        mask_secrets("https://user:password123@host/db?api_key=opaque-token-123&limit=10")
        == f"https://user:{MASK}@host/db?api_key={MASK}&limit=10"
    )


def test_escaped_json_inside_tool_arguments_is_decoded_and_masked():
    inner = {
        "headers": {"Authorization": "Bearer opaque-token-123"},
        "password": "escaped!secret",
        "traceId": "550e8400-e29b-41d4-a716-446655440000",
    }
    masked = json.loads(mask_secrets(json.dumps({"arguments": json.dumps(inner)})))
    assert json.loads(masked["arguments"]) == {
        "headers": {"Authorization": MASK},
        "password": MASK,
        "traceId": inner["traceId"],
    }
    assert mask_secrets(json.dumps(masked)) == json.dumps(masked)


def test_unfinished_private_key_is_withheld_to_the_end():
    text = f"before -----BEGIN {KEY_MARKER}-----\nunfinished secret"
    assert mask_secrets(text) == f"before {MASK}"


def test_too_deep_nesting_fails_closed_and_long_text_stays_fast():
    nested: object = {"token": SAMPLES[0]}
    for _ in range(300):
        nested = {"nested": nested}
    assert mask_secrets(json.dumps(nested)) == MASK
    text = ("word" * 40 + " uuid=550e8400-e29b-41d4-a716-446655440000\n") * 5000
    started = time.monotonic()
    assert mask_secrets(text) == text
    assert mask_secrets("a-" * 16000) == "a-" * 16000
    assert time.monotonic() - started < 5


def test_queued_events_from_an_older_helper_are_masked_before_upload(tmp_path):
    secret = "sk_live_" + "h" * 24
    db = capture.connect_state(tmp_path, "claude", "session")
    db.execute(
        "INSERT INTO segments(id,owner,context,generation,title) VALUES (?,?,?,?,?)",
        ("segment", "owner", 578, "generation", "Deploy with " + secret),
    )
    event = {
        "event_id": "event",
        "sequence": 1,
        "kind": "user",
        "content": "STRIPE_KEY=" + secret,
        "occurred_at": "2026-11-15T10:00:00+00:00",
        "truncated": False,
    }
    db.execute(
        "INSERT INTO events(id,sequence,segment,body) VALUES (?,?,?,?)",
        ("event", 1, "segment", json.dumps(event)),
    )
    segment = db.execute("SELECT * FROM segments WHERE id='segment'").fetchone()
    batch = capture.next_batch(db, segment, "claude", "session")
    assert secret not in batch["body"].decode()
    sent = json.loads(batch["body"])
    assert sent["events"][0]["content"] == "STRIPE_KEY=" + MASK
    assert sent["title"] == "Deploy with " + MASK
    stored = db.execute("SELECT body FROM events WHERE id='event'").fetchone()["body"]
    assert secret not in stored
