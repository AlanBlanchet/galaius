"""The machine channel: its address on the server and the websocket opened to it."""

import websockets
from galaius import USER_AGENT
from galaius.account.tls import server_tls
from galaius.machine.connection.config import MachineConfig


def channel_url(server_url: str) -> str:
    return MachineConfig.endpoint_on(server_url, "/v1/machine-channel", socket=True)


def channel(endpoint: str, token: str, open_timeout: float = 10, close_timeout: float = 3, ping_interval: float | None = 20, max_size: int = 1024 * 1024) -> websockets.connect:
    """The machine channel at `endpoint`, trusting what every HTTP call to the server trusts (`server_tls`)."""
    return websockets.connect(endpoint, additional_headers={"Authorization": f"Bearer {token}"}, user_agent_header=USER_AGENT,
                              ssl=server_tls() if endpoint.startswith("wss:") else None, open_timeout=open_timeout,
                              close_timeout=close_timeout, ping_interval=ping_interval, max_size=max_size)
