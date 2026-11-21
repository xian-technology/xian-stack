"""The native PostGraphile test waits for its isolated services to accept requests.

This runs without the PostGraphile image, unlike the service tests themselves.
"""

from __future__ import annotations

import test_postgraphile_native as native


def test_wait_until_retries_a_connection_reset_while_the_server_starts(monkeypatch):
    """Docker's port proxy accepts a connection before the server listens and then resets it."""
    monkeypatch.setattr(native.time, "sleep", lambda _seconds: None)
    attempts = []

    def read():
        attempts.append(None)
        if len(attempts) == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return "ready"

    assert native.wait_until(read) == "ready"
    assert len(attempts) == 2
