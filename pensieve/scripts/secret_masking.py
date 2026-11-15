"""Mask recognised credentials in captured text before it is queued or uploaded.

Adapted from OpenCrawl's capture redaction (Apache-2.0), whose credential formats
come from Gitleaks v8.30.0 (MIT); see THIRD_PARTY_NOTICES.md. A value is masked
when its format is a known credential, or when a credential-named field, header
or URL holds it. There is deliberately no entropy or length-only rule, so UUIDs,
hashes, trace IDs and commit SHAs stay readable. Masking is pattern recognition,
not a guarantee: an unknown format in free prose can still get through.
"""

from __future__ import annotations

import json
import re

MASK = "[REDACTED_SECRET]"
# Encoded tool results can nest JSON inside JSON strings. Past this depth the
# whole value is withheld rather than sent unmasked.
MAX_DEPTH = 256

CREDENTIALS = [
    re.compile(
        r"-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----.*?"
        r"(?:-----END[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----|\Z)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,255}|github_pat_[A-Za-z0-9_]{82})"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,255}"),
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),
    # OpenAI, Anthropic and OpenRouter keys all start `sk-`.
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:sk|rk)_(?:live|test|prod)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{20,}"),
    re.compile(r"\bxapp-\d-[A-Za-z0-9-]{20,}"),
    re.compile(
        r"(?:https?://)?hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/+_-]{20,}"
    ),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}(?![A-Za-z0-9])"),
    re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    re.compile(r"\bkey-[a-f0-9]{32}\b"),
    re.compile(r"\b(?:hvs|hvb)\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:dop|doo|dor)_v1_[a-f0-9]{64}\b"),
    re.compile(
        r"\b(?:hf_[A-Za-z0-9]{34}|AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58})\b"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}={0,2}"),
    # Formats in Pensieve's own stack: capture upload keys, Doppler, Supabase, PostHog.
    re.compile(r"\bpcap_[A-Za-z0-9_-]{32,}"),
    re.compile(r"\bdp\.(?:pt|st|ct|sa|scim|audit)\.[A-Za-z0-9_.-]{20,}"),
    re.compile(r"\b(?:sbp_[a-f0-9]{40}\b|sb_secret_[A-Za-z0-9_-]{20,})"),
    re.compile(r"\bphx_[A-Za-z0-9]{30,}"),
]
AUTH_SCHEME = re.compile(r"(\b(?:Bearer|Basic)[ \t]+)([A-Za-z0-9._~+/-]{8,}=*)", re.IGNORECASE)
URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]{0,31}://[^\s/@:]+:)[^\s/@]+(@)", re.IGNORECASE)
# JSON, env, shell and query-string assignments. Values stop at delimiters,
# never at the next line, and a name is bounded so a long run cannot backtrack.
ASSIGNMENT = re.compile(
    r"""(\b([A-Za-z][A-Za-z0-9_-]{0,63})["']?[ \t]*[=:][ \t]*)"""
    r"""(?:"((?:\\.|[^"\\\r\n])*)"|'((?:\\.|[^'\\\r\n])*)'|([^\s"'`,;&<>\[\]{}]+))"""
)
SECRET_NAME = re.compile(
    r"(?:[a-z][a-z0-9]*[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token"
    r"|secret(?:[_-]?(?:key|access[_-]?key))?|client[_-]?secret|password|passwd|token"
    r"|upload[_-]?key)|authorization|x-api-key|x-amz-(?:signature|security-token)",
    re.IGNORECASE,
)
# Shell, template and environment references name a secret without holding it.
REFERENCE = re.compile(
    r"^(?:\$|%[A-Z_]+%|\{\{|<|process\.env\.|(?:os\.)?environ\b|import\.meta\.env\.)"
    r"|^(?:true|false|null|none|undefined)$",
    re.IGNORECASE,
)


class TooDeep(ValueError):
    pass


def is_literal(value: str) -> bool:
    return len(value) >= 4 and value != MASK and not REFERENCE.search(value)


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
            if isinstance(entry, str) and SECRET_NAME.fullmatch(key) and is_literal(entry)
            else mask_value(entry, depth + 1)
            for key, entry in value.items()
        }
    return value


def mask_patterns(text: str) -> str:
    for pattern in CREDENTIALS:
        text = pattern.sub(MASK, text)
    text = AUTH_SCHEME.sub(
        lambda m: m[1] + (MASK if is_literal(m[2]) else m[2]),
        text,
    )
    text = URL_PASSWORD.sub(lambda m: m[1] + MASK + m[2], text)
    return ASSIGNMENT.sub(mask_assignment, text)


def mask_assignment(match: re.Match) -> str:
    prefix, name, double, single, bare = match.groups()
    value = next(v for v in (double, single, bare) if v is not None)
    # An unquoted call such as `token = get_token()` says where a secret comes
    # from without holding it; coding transcripts are full of them.
    if (
        not SECRET_NAME.fullmatch(name)
        or not is_literal(value)
        or value.lower() in {"bearer", "basic"}
        or (bare is not None and "(" in bare)
    ):
        return match[0]
    quote = '"' if double is not None else "'" if single is not None else ""
    return prefix + quote + MASK + quote
