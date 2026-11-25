"""Mask recognised credentials in captured text before it is queued or uploaded.

Adapted from OpenCrawl's capture redaction (Apache-2.0), whose credential formats
come from Gitleaks v8.30.0 (MIT); see THIRD_PARTY_NOTICES.md. A value is masked
when its format is a known credential, or when a credential-named field, flag,
header or URL holds it. There is deliberately no entropy or length-only rule, so UUIDs,
hashes, trace IDs and commit SHAs stay readable. Masking is pattern recognition,
not a guarantee: an unknown format in free prose can still get through.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MASK = "[REDACTED_SECRET]"
# Encoded tool results can nest JSON inside JSON strings. Past this depth the
# whole value is withheld rather than sent unmasked.
MAX_DEPTH = 256
# ASCII word boundaries, as in OpenCrawl's JavaScript: Python's Unicode `\b`
# would miss a key written straight after accented or CJK text.
A = re.ASCII


def load_formats(path: Path) -> tuple[re.Pattern[str], ...]:
    """Compile the credential formats file: an identifier, a tab, a regex per line."""
    formats = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            formats.append(re.compile(line.partition("\t")[2], A))
    return tuple(formats)


# Formats safe in any text, shared with the Pensieve server's source scrubbing.
FORMATS = load_formats(Path(__file__).with_name("credential_formats.txt"))
# Broader shapes for coding transcripts only, applied before the shared formats:
# an unfinished private key withheld to the end of its body, any `sk-` key, and
# GitHub tokens from 16 characters. In a business document these would misfire.
TRANSCRIPT_FORMATS = (
    re.compile(
        r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"
        r"(?!(?:(?!-----BEGIN)[\s\S]){0,20000}?-----END[ A-Z0-9_-]{0,100}PRIVATE KEY)"
        r"[A-Za-z0-9+/=\s]*",
        A | re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}", A),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,255}", A),
)
AUTH_SCHEME = re.compile(r"(\b(?:Bearer|Basic)[ \t]+)([A-Za-z0-9._~+/-]{8,}=*)", A | re.IGNORECASE)
# Whatever the scheme (`Token`, `Digest`, `SSWS`...), an Authorization header's
# credential follows the scheme word.
AUTH_HEADER = re.compile(
    r"""(\b(?:proxy-)?authorization["']?[ \t]*[=:][ \t]*["']?[A-Za-z][A-Za-z0-9-]{0,31}[ \t]+)"""
    r"""([^\s"',;]+)""",
    A | re.IGNORECASE,
)
# A value: quoted, or bare up to a delimiter, never past the end of its line.
# A bare value stops at `)` so a parameter's type, as in `password: string)`, is
# read as the type.
VALUE = r"""(?:"((?:\\.|[^"\\\r\n])*)"|'((?:\\.|[^'\\\r\n])*)'|([^\s"'`,;&<>\[\]{})]+))"""
# JSON, env, shell and query-string assignments. A name is bounded so a long run
# cannot backtrack.
ASSIGNMENT = re.compile(r"""(\b([A-Za-z][A-Za-z0-9_-]{0,63})["']?[ \t]*[=:][ \t]*)""" + VALUE, A)
# A command-line flag followed by its value, as in `--password hunter2`.
FLAG = re.compile(r"(--([A-Za-z][A-Za-z0-9_-]{0,63})[ \t]+)(?!-)" + VALUE, A)
# A name ending in a credential word: `password`, `DB_PASSWORD`, `PGPASSWORD`.
SECRET_NAME = re.compile(
    r"[a-z0-9_-]*(?:api[_-]?key|(?:private|signing|encryption)[_-]?key|secret(?:[_-]?(?:access[_-]?)?key)?"
    r"|password|passwd|passphrase|token|upload[_-]?key)|authorization|x-amz-signature",
    re.IGNORECASE,
)
# Shell, template, environment and settings references name a secret without
# holding it.
REFERENCE = re.compile(
    r"^(?:\$|%[A-Z_]+%|\{\{|<|(?:os\.)?environ\b|import\.meta\.env\.|process\.env\."
    r"|(?:self|this|cls|settings|config|conf|cfg|env|options|opts|args|ctx|context|request"
    r"|req|params|props|secrets|credentials|creds)\.)",
    A | re.IGNORECASE,
)
# Words that name a type, keyword or auth scheme rather than hold a secret, as
# in `password: string`, `token: Optional[str]` or `Authorization: Token ...`.
NOT_A_VALUE = {
    "true",
    "false",
    "null",
    "none",
    "undefined",
    "string",
    "number",
    "boolean",
    "bigint",
    "object",
    "unknown",
    "never",
    "void",
    "float",
    "bytes",
    "bool",
    "dict",
    "list",
    "optional",
    "secretstr",
    "promise",
    "await",
    "async",
    "bearer",
    "basic",
    "token",
    "digest",
    "ssws",
    "negotiate",
}


class TooDeep(ValueError):
    pass


def is_literal(value: str) -> bool:
    return (
        len(value) >= 4
        and value != MASK
        and value.lower() not in NOT_A_VALUE
        and not REFERENCE.search(value)
    )


def mask_secrets(text: str, depth: int = 0) -> str:
    """Mask credentials; serialised JSON is decoded so escaped values are seen."""
    if depth > MAX_DEPTH:
        raise TooDeep
    if text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            try:
                masked = mask_value(parsed, depth + 1)
            except (TooDeep, RecursionError):
                return MASK
            if masked != parsed:
                return json.dumps(masked, ensure_ascii=False, indent=2)
    return mask_patterns(text)


def mask_value(value, depth: int):
    if depth > MAX_DEPTH:
        raise TooDeep
    if isinstance(value, str):
        return mask_secrets(value, depth)
    if isinstance(value, list):
        return [mask_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            mask_secrets(key, depth + 1): MASK
            if holds_secret(key, entry)
            else mask_value(entry, depth + 1)
            for key, entry in value.items()
        }
    return value


def holds_secret(key: str, entry: object) -> bool:
    """A credential-named field holding a literal, including a numeric PIN."""
    if isinstance(entry, bool) or not isinstance(entry, (str, int, float)):
        return False
    return bool(SECRET_NAME.fullmatch(key)) and is_literal(str(entry))


def mask_format(match: re.Match) -> str:
    """The marker, keeping a format's `keep` and `tail` groups around it."""
    groups = match.groupdict()
    return (groups.get("keep") or "") + MASK + (groups.get("tail") or "")


def mask_patterns(text: str) -> str:
    for pattern in TRANSCRIPT_FORMATS:
        text = pattern.sub(MASK, text)
    for pattern in FORMATS:
        text = pattern.sub(mask_format, text)
    for pattern in (AUTH_SCHEME, AUTH_HEADER):
        text = pattern.sub(lambda m: m[1] + (MASK if is_literal(m[2]) else m[2]), text)
    text = ASSIGNMENT.sub(mask_assignment, text)
    return FLAG.sub(mask_assignment, text)


def mask_assignment(match: re.Match) -> str:
    prefix, name, double, single, bare = match.groups()
    value = next(v for v in (double, single, bare) if v is not None)
    # An unquoted call such as `token = get_token()` says where a secret comes
    # from without holding it; coding transcripts are full of them.
    if (
        not SECRET_NAME.fullmatch(name)
        or not is_literal(value)
        or (bare is not None and "(" in bare)
    ):
        return match[0]
    quote = '"' if double is not None else "'" if single is not None else ""
    return prefix + quote + MASK + quote
