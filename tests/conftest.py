import pytest


@pytest.fixture(autouse=True)
def no_live_onboarding(monkeypatch):
    # Fixture native markers must never open a real browser page.
    monkeypatch.setattr("capture_onboarding.open_page", lambda url: None)
