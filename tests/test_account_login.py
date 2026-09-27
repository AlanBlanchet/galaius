"""Which server `interact login` signs in at, from what this computer remembered."""

import httpx
import pytest

from interact.account_login import AccountLogin

PUBLIC = "https://interact.example.org"
TUNNEL = "http://127.0.0.1:8817"


@pytest.mark.parametrize("remembered, answering, expected", [
    (TUNNEL, {TUNNEL, PUBLIC}, PUBLIC),  # tunnel up: the public address it names is used
    (TUNNEL, {TUNNEL}, TUNNEL),          # public unreachable from here: the tunnel still works
    (TUNNEL, set(), None),               # tunnel down: never the target, asked instead
    (PUBLIC, set(), PUBLIC),             # a public address is kept as remembered
])
def test_remembered_server(monkeypatch: pytest.MonkeyPatch, remembered: str, answering: set[str], expected: str | None) -> None:
    def client(self: AccountLogin) -> httpx.Client:
        def answer(request: httpx.Request) -> httpx.Response:
            if self.server not in answering:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"server": PUBLIC} if request.url.path == "/v1/install" else {})
        return httpx.Client(base_url=self.server, transport=httpx.MockTransport(answer))

    monkeypatch.setattr(AccountLogin, "client", client)
    chosen = AccountLogin.parsed(remembered).public()
    assert (chosen.server if chosen else None) == expected
