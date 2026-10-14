"""Startup-order regression tests; the full application dependencies are required."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("polars")

import datashell.server.app as server_app


def test_publication_recovery_runs_before_contract_seeding(monkeypatch):
    events = []

    def recover(root):
        events.append(("recover", root))
        return 0

    def seed(registry):
        events.append(("seed", registry))

    monkeypatch.setattr(server_app, "recover_pending_publications", recover)
    monkeypatch.setattr(server_app, "seed_default_contracts", seed)

    async def scenario():
        async with server_app.lifespan(server_app.app):
            events.append(("ready", None))

    asyncio.run(scenario())
    assert [event[0] for event in events] == ["recover", "seed", "ready"]


def test_recovery_failure_prevents_contract_seeding_and_readiness(monkeypatch):
    events = []

    def fail_recovery(root):
        events.append("recover")
        raise RuntimeError("journal integrity failure")

    def seed(registry):
        events.append("seed")

    monkeypatch.setattr(server_app, "recover_pending_publications", fail_recovery)
    monkeypatch.setattr(server_app, "seed_default_contracts", seed)

    async def scenario():
        async with server_app.lifespan(server_app.app):
            events.append("ready")

    with pytest.raises(RuntimeError, match="journal integrity failure"):
        asyncio.run(scenario())
    assert events == ["recover"]
