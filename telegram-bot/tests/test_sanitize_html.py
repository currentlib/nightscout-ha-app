"""Unit tests for app/bot/handlers/common.py's sanitize_telegram_html.

Pure function, no DB, no event loop. Unexecuted in this environment (no Python
interpreter available in this sandbox) — run with `pytest` from the repo root
once a real environment is available.
"""
from app.bot.handlers import sanitize_telegram_html


def test_literal_lt_gets_escaped():
    assert sanitize_telegram_html("BG < 3.9") == "BG &lt; 3.9"


def test_allowed_tag_passes_through():
    assert sanitize_telegram_html("<b>bold</b>") == "<b>bold</b>"


def test_allowed_closing_tag_passes_through():
    assert sanitize_telegram_html("<i>text</i> and <a href='x'>link</a>") == \
        "<i>text</i> and <a href='x'>link</a>"


def test_disallowed_tag_gets_escaped():
    assert sanitize_telegram_html("<script>alert(1)</script>") == "&lt;script>alert(1)&lt;/script>"


def test_br_tag_gets_escaped():
    assert sanitize_telegram_html("line1<br>line2") == "line1&lt;br>line2"


def test_empty_string_returns_as_is():
    assert sanitize_telegram_html("") == ""


def test_none_returns_as_is():
    assert sanitize_telegram_html(None) is None
