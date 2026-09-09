"""Tests for `load_settings` — the one place `env` is read.

`load_settings` had no tests before the protocol split, and the split gives it
real work: two enums to parse, a base URL to shape-check, and a warning to emit.
Every failure here must be a `RuntimeError` (the type the error-handling spec
assigns to configuration failures, and the only one `run_sync` catches around this
call) and must never echo a configuration value.

`env` is a `SimpleNamespace` because the real Worker env is attribute-access only:
`env.LLM_BASE_URL` works and `env["LLM_BASE_URL"]` raises. A dict would test the
wrong contract.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from linuxdo_oss.config import MAX_CONCURRENCY_CEILING, load_settings
from linuxdo_oss.llm import LLMProtocol, SchemaMode

API_KEY = "sk-secret-must-never-be-logged-0123456789"

REQUIRED = {
    "TAG_FEED_URL": "https://forum.example/tag/oss/42.rss",
    "LLM_BASE_URL": "https://api.example.com/v1",
    "LLM_MODEL": "gpt-test",
    "LLM_API_KEY": API_KEY,
}


def make_env(**overrides: Any) -> SimpleNamespace:
    values = dict(REQUIRED)
    values.update(overrides)
    return SimpleNamespace(**{k: v for k, v in values.items() if v is not None})


# ----------------------------------------------------------------------
# Defaults preserve the pre-split deployment
# ----------------------------------------------------------------------


def test_absent_protocol_and_schema_mode_keep_the_original_behaviour():
    """A deployment that predates both variables must not change what it does."""
    settings = load_settings(make_env())

    assert settings.llm_protocol is LLMProtocol.RESPONSES
    assert settings.llm_schema_mode is SchemaMode.STRICT


def test_blank_values_fall_back_to_the_defaults_rather_than_failing():
    """`vars` in wrangler.jsonc can legitimately hold an empty string."""
    settings = load_settings(make_env(LLM_PROTOCOL="", LLM_SCHEMA_MODE=""))

    assert settings.llm_protocol is LLMProtocol.RESPONSES
    assert settings.llm_schema_mode is SchemaMode.STRICT


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_every_protocol_round_trips_through_configuration(protocol):
    # Anthropic's root must not carry `/v1`; the other two conventionally do.
    base = (
        "https://api.anthropic.com"
        if protocol is LLMProtocol.ANTHROPIC
        else "https://api.example.com/v1"
    )

    settings = load_settings(make_env(LLM_PROTOCOL=protocol.value, LLM_BASE_URL=base))

    assert settings.llm_protocol is protocol


@pytest.mark.parametrize("mode", list(SchemaMode))
def test_every_schema_mode_round_trips_through_configuration(mode):
    settings = load_settings(make_env(LLM_SCHEMA_MODE=mode.value))

    assert settings.llm_schema_mode is mode


@pytest.mark.parametrize("spelling", ["RESPONSES", "  responses  ", "Chat_Completions"])
def test_case_and_surrounding_space_are_tolerated(spelling):
    """Leniency about typography only. The set of accepted values is unchanged."""
    settings = load_settings(make_env(LLM_PROTOCOL=spelling))

    assert settings.llm_protocol.value == spelling.strip().lower()


# ----------------------------------------------------------------------
# An unrecognised value is never guessed
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LLM_PROTOCOL", "openai"),
        ("LLM_PROTOCOL", "chat"),
        ("LLM_PROTOCOL", "claude"),
        ("LLM_SCHEMA_MODE", "json"),
        ("LLM_SCHEMA_MODE", "off"),
        ("LLM_SCHEMA_MODE", "true"),
    ],
)
def test_an_unrecognised_value_aborts_instead_of_defaulting(variable, value):
    """Defaulting a typo to `responses` / `strict` is the failure being removed.

    The operator would see 404s from an endpoint they believed they had selected,
    or a weakened guard they never asked for.
    """
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(**{variable: value}))

    message = str(caught.value)
    assert variable in message
    # The rejected spelling IS named: it is what the operator typed, it is not a
    # secret, and a message that only said "invalid" would send them hunting.
    assert value in message


# ----------------------------------------------------------------------
# Base URL shape
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("protocol", "base_url", "root"),
    [
        (LLMProtocol.RESPONSES, "https://api.openai.com/v1/responses", "https://api.openai.com/v1"),
        (
            LLMProtocol.CHAT_COMPLETIONS,
            "https://api.openai.com/v1/chat/completions",
            "https://api.openai.com/v1",
        ),
        (
            LLMProtocol.ANTHROPIC,
            "https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com",
        ),
        # The paste-o that only anthropic suffers: its suffix already carries /v1.
        # It used to abort; ADR 0006 strips it back instead.
        (LLMProtocol.ANTHROPIC, "https://api.anthropic.com/v1", "https://api.anthropic.com"),
    ],
)
def test_a_paste_of_the_protocols_own_endpoint_resolves_to_the_root(protocol, base_url, root):
    settings = load_settings(make_env(LLM_PROTOCOL=protocol.value, LLM_BASE_URL=base_url))

    assert settings.llm_base_url == root


def test_a_stripped_suffix_warns_with_the_suffix_and_the_protocol_only(caplog):
    """Tolerated, never silent — and the WARNING obeys the same rule as the
    rejections: the suffix and the protocol name, never the URL."""
    with caplog.at_level(logging.WARNING, logger="linuxdo_oss.config"):
        load_settings(
            make_env(
                LLM_PROTOCOL="chat_completions",
                LLM_BASE_URL="https://gw.example.com/v1/chat/completions",
            )
        )

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1

    message = records[0].getMessage()
    assert "/chat/completions" in message
    assert "chat_completions" in message
    assert "gw.example.com" not in message
    assert API_KEY not in message


def test_a_root_that_needs_no_strip_stays_silent(caplog):
    """The common case must not add a line to every cron run's log."""
    with caplog.at_level(logging.WARNING, logger="linuxdo_oss.config"):
        settings = load_settings(make_env(LLM_BASE_URL="https://api.openai.com/v1"))

    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    ("protocol", "base_url", "owner"),
    [
        (LLMProtocol.RESPONSES, "https://gw.example.com/v1/chat/completions", "chat_completions"),
        (LLMProtocol.RESPONSES, "https://gw.example.com/v1/messages", "anthropic"),
        (LLMProtocol.CHAT_COMPLETIONS, "https://gw.example.com/v1/responses", "responses"),
        (LLMProtocol.ANTHROPIC, "https://gw.example.com/v1/responses", "responses"),
    ],
)
def test_another_protocols_endpoint_path_still_aborts_at_startup(protocol, base_url, owner):
    """Class B. Stripping this would hide a wrong LLM_PROTOCOL behind a 404 raised
    five minutes later, from a cron run."""
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(LLM_PROTOCOL=protocol.value, LLM_BASE_URL=base_url))

    message = str(caught.value)
    assert "LLM_BASE_URL" in message
    assert owner in message
    assert "gw.example.com" not in message


def test_a_strip_that_would_eat_the_host_aborts_as_a_runtime_error():
    """`"https://v1".endswith("/v1")` is True. The failure the strip introduces must
    still leave config.py as RuntimeError, not ValueError."""
    with pytest.raises(RuntimeError, match="no host"):
        load_settings(make_env(LLM_PROTOCOL="anthropic", LLM_BASE_URL="https://v1"))


@pytest.mark.parametrize("character", ["?", "#"])
def test_a_base_url_with_a_query_string_or_fragment_is_refused(character):
    """The suffix is a PATH. Appending it after `?k=1` requests a path inside the
    query string, so the shape is refused rather than silently mangled."""
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(LLM_BASE_URL=f"https://gw.example.com/v1{character}token={API_KEY}"))

    message = str(caught.value)
    assert "LLM_BASE_URL" in message
    # This is the case that makes echoing the URL unacceptable: the key is in it.
    assert API_KEY not in message
    assert "gw.example.com" not in message


@pytest.mark.parametrize("trailing", ["", "/", "///"])
def test_trailing_slashes_do_not_defeat_the_strip(trailing):
    settings = load_settings(
        make_env(LLM_BASE_URL=f"https://api.openai.com/v1/responses{trailing}")
    )

    assert settings.llm_base_url == "https://api.openai.com/v1"


def test_a_v1_root_is_correct_and_untouched_for_the_openai_style_protocols():
    """The rule is per-protocol: `/v1` is a root here and a strippable prefix for
    anthropic. Stripping it here would break a working deployment."""
    for protocol in (LLMProtocol.RESPONSES, LLMProtocol.CHAT_COMPLETIONS):
        settings = load_settings(
            make_env(LLM_PROTOCOL=protocol.value, LLM_BASE_URL="https://api.openai.com/v1")
        )
        assert settings.llm_base_url == "https://api.openai.com/v1"


@pytest.mark.parametrize("base_url", ["http://api.example.com/v1", "api.example.com", "ftp://x"])
def test_a_non_https_base_url_is_still_refused(base_url):
    """Pre-existing rule, re-asserted because the validation around it changed."""
    with pytest.raises(RuntimeError):
        load_settings(make_env(LLM_BASE_URL=base_url))


# ----------------------------------------------------------------------
# Degrading is loud
# ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", [SchemaMode.JSON_OBJECT, SchemaMode.NONE])
def test_a_degraded_schema_mode_warns_on_every_run(mode, caplog):
    """ADR 0005 forbids SILENT degradation, not configured degradation."""
    with caplog.at_level(logging.WARNING, logger="linuxdo_oss.config"):
        load_settings(make_env(LLM_SCHEMA_MODE=mode.value))

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1

    message = records[0].getMessage()
    assert mode.value in message
    # Only the mode name. Never a URL, a model or a key.
    assert API_KEY not in message
    assert "api.example.com" not in message
    assert "gpt-test" not in message


def test_strict_mode_is_silent(caplog):
    """The default must not add a line to every cron run's log."""
    with caplog.at_level(logging.WARNING, logger="linuxdo_oss.config"):
        load_settings(make_env(LLM_SCHEMA_MODE=SchemaMode.STRICT.value))

    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# ----------------------------------------------------------------------
# The pre-existing contract, unchanged
# ----------------------------------------------------------------------


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_a_missing_required_value_aborts_without_echoing_anything(missing):
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(**{missing: None}))

    message = str(caught.value)
    assert missing in message
    assert API_KEY not in message


def test_concurrency_is_clamped_to_the_connection_ceiling():
    settings = load_settings(make_env(SYNC_CONCURRENCY=99))

    assert settings.max_concurrency == MAX_CONCURRENCY_CEILING


@pytest.mark.parametrize(
    "variable",
    ["SYNC_BATCH_SIZE", "SYNC_CONCURRENCY", "HTTP_TIMEOUT_SECONDS", "MAX_RESPONSE_BYTES"],
)
def test_a_non_numeric_number_is_a_runtime_error_not_a_value_error(variable):
    """`int("twenty")` raises `ValueError`, which `run_sync` does not catch.

    `run_sync` wraps this call in `except RuntimeError` only, so a `ValueError`
    escaping here propagates out of a function that promises never to raise and
    takes the cron invocation with it. `.env` values are always strings and
    `wrangler.jsonc` `vars` are hand-edited JSON, so this is one typo away.
    """
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(**{variable: "twenty"}))

    message = str(caught.value)
    assert variable in message
    # `int`'s own message quotes the input; this one must not, because it reaches
    # the log through `run_sync`'s `logger.exception`.
    assert "twenty" not in message


@pytest.mark.parametrize(
    ("variable", "attribute", "default"),
    [
        ("SYNC_BATCH_SIZE", "sync_batch_size", 20),
        ("HTTP_TIMEOUT_SECONDS", "http_timeout_seconds", 10.0),
        ("MAX_RESPONSE_BYTES", "max_response_bytes", 2 * 1024 * 1024),
    ],
)
def test_a_blank_or_zero_number_keeps_the_pre_existing_default(variable, attribute, default):
    """Falsy meaning "use the default" is the contract the `or default` idiom had.

    Preserved on purpose: `wrangler.jsonc` `vars` can hold an empty string, and
    changing what a bare `0` means would be a silent behaviour change.
    """
    for blank in ("", 0):
        settings = load_settings(make_env(**{variable: blank}))

        assert getattr(settings, attribute) == default


# ----------------------------------------------------------------------
# TAG_FEED_URL — the pipeline's only input
# ----------------------------------------------------------------------


def test_the_tag_feed_url_reaches_settings_verbatim():
    """No normalisation: unlike `LLM_BASE_URL`, nothing is appended to this URL,
    so a query string or a path suffix is the operator's business."""
    url = "https://linux.do/tag/2234-tag/2234.rss"

    assert load_settings(make_env(TAG_FEED_URL=url)).tag_feed_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://forum.example/tag/oss/42.rss",
        "ftp://forum.example/tag/oss/42.rss",
        "//forum.example/tag/oss/42.rss",
        "forum.example/tag/oss/42.rss",
    ],
)
def test_a_tag_feed_url_that_is_not_https_is_refused(url):
    """The feed is the sole input to everything this project publishes; plaintext
    transport would let a network position choose that input."""
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(TAG_FEED_URL=url))

    assert "TAG_FEED_URL" in str(caught.value)


def test_a_tag_feed_url_without_a_host_is_refused():
    """`feeds/tag_feed.py` derives the expected item host from this value, so a
    hostless URL would disable the check that keeps foreign topics out of D1."""
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(TAG_FEED_URL="https:///tag/oss/42.rss"))

    assert "host" in str(caught.value)


def test_a_missing_tag_feed_url_aborts_startup():
    with pytest.raises(RuntimeError) as caught:
        load_settings(make_env(TAG_FEED_URL=None))

    assert "TAG_FEED_URL" in str(caught.value)


def test_the_tag_feed_url_never_appears_in_a_log(caplog):
    """A self-hosted forum's feed URL can carry an access key in its query string."""
    secret = "https://forum.example/tag/oss/42.rss?key=super-secret-token"

    with caplog.at_level(logging.WARNING):
        load_settings(make_env(TAG_FEED_URL=secret, LLM_SCHEMA_MODE="none"))

    assert "super-secret-token" not in caplog.text
