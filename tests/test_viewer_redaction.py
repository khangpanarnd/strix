"""Tests for viewer secret redaction (BR-30, NFR-5)."""

from __future__ import annotations

from strix.interface.viewer.redaction import REPLACEMENT, _secret_values, redact


def test_redacts_secret_named_env_value() -> None:
    env = {"OPENAI_API_KEY": "supersecretvalue123"}
    out = redact("the key is supersecretvalue123 here", env=env)

    assert "supersecretvalue123" not in out
    assert REPLACEMENT in out


def test_ignores_short_env_values() -> None:
    env = {"MY_TOKEN": "ab12"}  # < 6 chars
    text = "value ab12 stays"
    assert redact(text, env=env) == text


def test_ignores_non_secret_env_names() -> None:
    env = {"HOME": "longenoughvalue"}
    text = "HOME is longenoughvalue"
    assert redact(text, env=env) == text


def test_redacts_multi_line_value_whole_and_per_line() -> None:
    secret = "line-one-secret\nline-two-secret"
    env = {"SERVICE_SECRET": secret}

    # Whole value present in output.
    whole = redact(f"before {secret} after", env=env)
    assert "line-one-secret" not in whole
    assert "line-two-secret" not in whole

    # Value split across the log: each >=6-char line is still caught.
    split = redact("prefix line-one-secret middle line-two-secret suffix", env=env)
    assert "line-one-secret" not in split
    assert "line-two-secret" not in split


def test_redacts_sk_pattern() -> None:
    text = "token sk-abcdefghijklmnopqrstuvwxyz0123 end"
    out = redact(text, env={})
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in out
    assert REPLACEMENT in out


def test_redacts_bearer_pattern() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1Ni234abc"
    out = redact(text, env={})
    assert "eyJhbGciOiJIUzI1Ni234abc" not in out
    assert REPLACEMENT in out


def test_clean_text_unchanged() -> None:
    text = "nothing secret here, just plain output lines"
    assert redact(text, env={}) == text


def test_empty_text_unchanged() -> None:
    assert redact("", env={}) == ""


def test_sk_pattern_too_short_not_redacted() -> None:
    # Fewer than 20 trailing chars does not match the sk- pattern.
    text = "value sk-short123 ok"
    assert redact(text, env={}) == text


def test_pathological_multiline_secret_is_capped() -> None:
    # 2,000 distinct long lines must not turn every redaction into 2,000 scans.
    secret = "\n".join(f"secret-line-number-{i:05d}" for i in range(2000))
    env = {"BIG_SECRET": secret}

    assert len(_secret_values(env)) <= 100
    # The longest fragment (the whole value) is still masked.
    assert redact(secret, env=env).count(REPLACEMENT) >= 1
