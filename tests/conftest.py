import pytest


@pytest.fixture(autouse=True)
def no_live_onboarding(monkeypatch):
    # Fixture native markers must never start a real browser pairing.
    monkeypatch.setattr("capture_onboarding.start", lambda *a, **kw: {"status": "unavailable"})
