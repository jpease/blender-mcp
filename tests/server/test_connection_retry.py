"""
Coverage for retrying the initial connection to Blender.

Blender is frequently not listening at the exact moment the MCP server first
reaches for it - the addon is still enabling, or a container has started but
not yet bound the port. Retrying the *initial* connect turns that race into a
short wait instead of a hard failure.

The retry deliberately wraps `BlenderConnection.connect` rather than living
inside it: every tool call reaches `connect` through `get_blender_connection`,
so changing its per-call semantics would alter behaviour far beyond startup.
"""

import pytest

from blender_mcp.server import connection as connection_module


class _Connection:
    """Fails `fail_times` connect attempts, then succeeds."""

    def __init__(self, fail_times):
        self.attempts = 0
        self._fail_times = fail_times

    def connect(self):
        self.attempts += 1
        return self.attempts > self._fail_times


class _Sleeper:
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def test_succeeds_on_the_first_attempt_without_sleeping() -> None:
    blender = _Connection(fail_times=0)
    sleeper = _Sleeper()

    assert connection_module.connect_with_retry(blender, attempts=3, delay=0.5, sleep=sleeper) is True
    assert blender.attempts == 1
    assert sleeper.delays == []


def test_retries_until_the_connection_succeeds() -> None:
    blender = _Connection(fail_times=2)
    sleeper = _Sleeper()

    assert connection_module.connect_with_retry(blender, attempts=3, delay=0.5, sleep=sleeper) is True
    assert blender.attempts == 3
    assert sleeper.delays == [0.5, 0.5]


def test_gives_up_after_the_configured_attempts_without_a_trailing_sleep() -> None:
    blender = _Connection(fail_times=99)
    sleeper = _Sleeper()

    assert connection_module.connect_with_retry(blender, attempts=3, delay=0.5, sleep=sleeper) is False
    assert blender.attempts == 3
    assert sleeper.delays == [0.5, 0.5]


def test_a_single_attempt_never_sleeps() -> None:
    blender = _Connection(fail_times=99)
    sleeper = _Sleeper()

    assert connection_module.connect_with_retry(blender, attempts=1, delay=0.5, sleep=sleeper) is False
    assert blender.attempts == 1
    assert sleeper.delays == []


def test_attempts_setting_defaults_and_reads_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("BLENDER_CONNECT_ATTEMPTS", raising=False)
    assert connection_module.connect_attempts() == connection_module.DEFAULT_CONNECT_ATTEMPTS

    monkeypatch.setenv("BLENDER_CONNECT_ATTEMPTS", "7")
    assert connection_module.connect_attempts() == 7


def test_attempts_setting_is_floored_at_one(monkeypatch) -> None:
    monkeypatch.setenv("BLENDER_CONNECT_ATTEMPTS", "0")
    assert connection_module.connect_attempts() == 1


def test_unparseable_attempts_setting_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("BLENDER_CONNECT_ATTEMPTS", "soon")
    assert connection_module.connect_attempts() == connection_module.DEFAULT_CONNECT_ATTEMPTS


def test_retry_delay_setting_defaults_and_reads_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("BLENDER_CONNECT_RETRY_DELAY", raising=False)
    assert connection_module.connect_retry_delay() == connection_module.DEFAULT_CONNECT_RETRY_DELAY

    monkeypatch.setenv("BLENDER_CONNECT_RETRY_DELAY", "1.5")
    assert connection_module.connect_retry_delay() == pytest.approx(1.5)


def test_unparseable_retry_delay_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("BLENDER_CONNECT_RETRY_DELAY", "later")
    assert connection_module.connect_retry_delay() == connection_module.DEFAULT_CONNECT_RETRY_DELAY


def test_get_blender_connection_retries_a_late_starting_blender(monkeypatch) -> None:
    blender = _Connection(fail_times=2)
    sleeper = _Sleeper()

    monkeypatch.setattr(connection_module, "_blender_connection", None)
    monkeypatch.setattr(connection_module, "BlenderConnection", lambda host, port: blender)
    monkeypatch.setattr(connection_module, "_maybe_handshake_addon", lambda _c: None)
    monkeypatch.setattr(connection_module.time, "sleep", sleeper)
    monkeypatch.setenv("BLENDER_CONNECT_ATTEMPTS", "3")
    monkeypatch.setenv("BLENDER_CONNECT_RETRY_DELAY", "0.25")

    try:
        assert connection_module.get_blender_connection() is blender
        assert blender.attempts == 3
        assert sleeper.delays == [0.25, 0.25]
    finally:
        connection_module._blender_connection = None


def test_get_blender_connection_still_raises_when_every_attempt_fails(monkeypatch) -> None:
    blender = _Connection(fail_times=99)

    monkeypatch.setattr(connection_module, "_blender_connection", None)
    monkeypatch.setattr(connection_module, "BlenderConnection", lambda host, port: blender)
    monkeypatch.setattr(connection_module.time, "sleep", lambda _d: None)
    monkeypatch.setenv("BLENDER_CONNECT_ATTEMPTS", "2")

    try:
        with pytest.raises(Exception, match="Could not connect to Blender"):
            connection_module.get_blender_connection()
        assert blender.attempts == 2
    finally:
        connection_module._blender_connection = None
