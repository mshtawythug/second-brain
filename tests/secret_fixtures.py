"""Synthetic credential fixtures for the ingest secret-guard suite (F4).

**Every value in this module is fake, non-resolving, and safe to commit.** They
are shaped to satisfy the regexes in :mod:`brain.secret_patterns` and nothing
else: the AWS pair are Amazon's own published documentation examples, and the
rest spell ``EXAMPLE``/``NOT``/``A``/``REAL`` in the entropy positions where a
genuine credential would carry secret bytes. No value authenticates against any
service. CLAUDE.md rule 15 (no PII / no real secrets in checked-in code) is
satisfied by construction, not by redaction.

Centralized in one module so the repo's own ``scripts/hooks/pre-commit`` gate
needs exactly one allowlist entry per token (``.pii-allowlist.txt``) rather than
one per test file, and so a future pattern addition has a single obvious place
to grow.
"""
from __future__ import annotations

# Canonical positive per pattern kind. Keys MUST stay in lockstep with
# ``brain.secret_patterns.SECRET_PATTERNS`` — ``tests/test_secret_patterns.py``
# asserts the two key sets are equal, so adding a pattern without adding a
# fixture (or vice versa) turns that test red.
SYNTHETIC_POSITIVES: dict[str, str] = {
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "aws_temp_access_key_id": "ASIAIOSFODNN7EXAMPLE",
    # NOT a real key type. The pattern is a ``BEGIN <kind> PRIVATE KEY`` banner
    # with the kind left open, so a synthetic kind satisfies it just as well as
    # RSA / EC / OPENSSH / ENCRYPTED — and unlike those, this string cannot be
    # the first line of anybody's actual key. That distinction is load-bearing.
    # This fixture is allowlisted in ``.pii-allowlist.txt`` so the repo can
    # commit itself, and the allowlist subtracts matched text VERBATIM. While
    # this value was the real RSA banner, the allowlist exempted the
    # byte-identical first line of every genuine PEM RSA key — and on a pasted
    # key that banner is the ONLY text matching any pattern, since the base64
    # body matches nothing. Subtracting it emptied the hit list and a real key
    # committed silently. Keep this value un-real. See
    # ``test_stage_one_blocks_a_genuine_rsa_private_key_header``.
    #
    # (The real banner is deliberately not spelled out anywhere above: the gate
    # now blocks it on sight, including inside a comment. It caught this very
    # docstring on the first run after the fix.)
    "private_key_header": "-----BEGIN EXAMPLE NOT A REAL PRIVATE KEY-----",
    "slack_token": "xoxb-EXAMPLE-NOT-A-REAL-TOKEN-000000",
    "openai_key": "sk-EXAMPLENOTAREALKEY0000000000",
    "openai_project_key": "sk-proj-EXAMPLE-NOT-A-REAL-KEY-000000",
    "stripe_secret_key_live": "sk_live_EXAMPLENOTAREAL000000",
    "stripe_restricted_key_live": "rk_live_EXAMPLENOTAREAL000000",
    "github_pat": "ghp_EXAMPLENOTAREALTOKEN0000000000000000",
    "github_pat_fine_grained": "github_pat_EXAMPLE_NOT_A_REAL_TOKEN_0000",
    "gitlab_pat": "glpat-EXAMPLE-NOT-REAL-0000",
    "google_api_key": "AIzaEXAMPLENOTAREALGOOGLEAPIKEY00000000",
}

# Near-misses: same prefix, but too short / wrong shape. Each MUST NOT match its
# own pattern, and the suite additionally asserts none matches ANY pattern —
# a negative that trips a sibling regex would silently weaken the test.
SYNTHETIC_NEGATIVES: dict[str, str] = {
    "aws_access_key_id": "AKIASHORT",
    "aws_temp_access_key_id": "ASIASHORT",
    "private_key_header": "-----BEGIN CERTIFICATE-----",
    "slack_token": "xoxb-short",
    "openai_key": "sk-tooshort",
    "openai_project_key": "sk-proj-short",
    "stripe_secret_key_live": "sk_live_short",
    "stripe_restricted_key_live": "rk_live_short",
    "github_pat": "ghp_tooshort",
    "github_pat_fine_grained": "github_pat_short",
    "gitlab_pat": "glpat-short",
    "google_api_key": "AIzaShort",
}

# The single value most tests reach for when they just need "a credential".
SYNTHETIC_AWS_KEY = SYNTHETIC_POSITIVES["aws_access_key_id"]

# Prose that must NOT trip the guard — the false-positive floor. Deliberately
# includes credential *vocabulary* without credential *shapes*.
CLEAN_PROSE = (
    "Rotate the deploy key every quarter.\n\n"
    "The runbook lives in the ops wiki; ask the platform team for access.\n"
)
