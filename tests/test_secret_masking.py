"""Credentials are masked before capture is queued or uploaded; ordinary IDs survive."""

from __future__ import annotations

import base64
import json
import time

import capture_state
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
        "function login(username: string, password: string): Promise<Token> {",
        "interface Session { accessToken: string; refreshToken?: string | null }",
        "  token: Optional[str] = None",
        "  api_key: SecretStr",
        "const token = await getToken();",
        "api_key = settings.OPENAI_API_KEY",
        "token = self.token",
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


def test_a_private_key_header_in_code_does_not_hide_the_rest():
    code = f"if line.startswith('-----BEGIN {KEY_MARKER}-----'):\n    parse(line)\nreturn result"
    assert mask_secrets(code) == f"if line.startswith('{MASK}'):\n    parse(line)\nreturn result"


@pytest.mark.parametrize("before", ["我的令牌是", "トークン", "é", "密码password="])
def test_credentials_next_to_non_ascii_text_are_masked(before):
    assert mask_secrets(before + SAMPLES[0]) == before + MASK


@pytest.mark.parametrize("scheme", ["Token", "Digest", "SSWS"])
def test_any_authorization_scheme_hides_its_credential(scheme):
    assert mask_secrets(f"Authorization: {scheme} opaqueTOKEN123") == (
        f"Authorization: {scheme} {MASK}"
    )


def test_numeric_credentials_are_masked_beside_other_changes():
    value = {"password": 12345678, "headers": {"Authorization": "Bearer opaque-token-123"}}
    masked = json.loads(mask_secrets(json.dumps(value)))
    assert masked == {"password": MASK, "headers": {"Authorization": MASK}}


def test_too_deep_nesting_fails_closed_and_long_text_stays_fast():
    nested: object = {"token": SAMPLES[0]}
    for _ in range(300):
        nested = {"nested": nested}
    assert mask_secrets(json.dumps(nested)) == MASK
    text = ("word" * 40 + " uuid=550e8400-e29b-41d4-a716-446655440000\n") * 5000
    started = time.monotonic()
    assert mask_secrets(text) == text
    assert mask_secrets("a-" * 16000) == "a-" * 16000
    assert mask_secrets("-eyJ" * 64000) == "-eyJ" * 64000
    assert KEY_MARKER not in mask_secrets(f"-----BEGIN {KEY_MARKER}-----\n" * 30000)
    assert capture_state.clean_content("data:" * 50000, [])[0].startswith("data:data:")
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "text,masked",
    [
        ("mysql --password hunter2secret -h db", f"mysql --password {MASK} -h db"),
        ("deploy --api-key 'opaque value' now", f"deploy --api-key '{MASK}' now"),
        ("PGPASSWORD=hunter2 psql", f"PGPASSWORD={MASK} psql"),
        ("SIGNING_KEY: 9f8e7d6c5b4a", f"SIGNING_KEY: {MASK}"),
        ("passphrase = 'correct horse'", f"passphrase = '{MASK}'"),
    ],
)
def test_flags_and_names_ending_in_a_credential_word_are_masked(text, masked):
    assert mask_secrets(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "psql --password --host db",
        "run --token $TOKEN",
        "max_tokens=4096",
        "--tokens 4096 --limit 10",
    ],
)
def test_flags_without_a_literal_credential_survive(text):
    assert mask_secrets(text) == text
