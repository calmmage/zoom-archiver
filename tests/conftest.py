"""Every suite is offline and insulated from developer credentials/config."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    for name in ('ZOOM_ACCOUNT_ID', 'ZOOM_CLIENT_ID', 'ZOOM_CLIENT_SECRET',
                 'ZOOM_USER_ID', 'ZOOM_ARCHIVER_ROOT', 'ZOOM_ARCHIVER_KEY_RESOLVER'):
        monkeypatch.delenv(name, raising=False)
    def reject(*args, **kwargs):
        raise AssertionError('real network forbidden in Zoom tests')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', reject)
