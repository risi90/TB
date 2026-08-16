"""Tests for the dashboard's optional password gate (via Streamlit AppTest)."""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from pathlib import Path  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

_APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


def _run_app() -> AppTest:
    at = AppTest.from_file(_APP, default_timeout=60)
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    return at


def test_no_password_configured_leaves_dashboard_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    at = _run_app()
    assert len(at.tabs) == 7  # full dashboard rendered, no login form


def test_password_locks_dashboard_until_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    at = _run_app()
    assert len(at.tabs) == 0  # nothing but the login form
    assert len(at.text_input) == 1

    # Wrong password: error shown, still locked.
    at.text_input[0].set_value("wrong")
    at.button[0].click()
    at.run()
    assert any("Incorrect" in str(e.value) for e in at.error)
    assert len(at.tabs) == 0

    # Correct password: session unlocks and the dashboard renders.
    at.text_input[0].set_value("s3cret")
    at.button[0].click()
    at.run()
    assert not at.exception, [e.message for e in at.exception]
    assert at.session_state["auth_ok"] is True
    assert len(at.tabs) == 7
