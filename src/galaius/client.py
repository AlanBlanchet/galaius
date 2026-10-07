"""The script entry point: `Client()` talks to the workspace `galaius agents sync` connected
(loopback session or workspace API key); `AsyncClient()` is the same for asyncio code.

    from galaius.client import Client
    run = Client().workflows.run("Write a report", inputs={"topic": "Q3"})
"""

from galaius.workflows import AsyncWorkflows, ServerBound, Workflows


class Client(ServerBound):
    @property
    def workflows(self) -> Workflows:
        return Workflows(server=self.server, poll_seconds=self.poll_seconds, read_attempts=self.read_attempts)


class AsyncClient(ServerBound):
    @property
    def workflows(self) -> AsyncWorkflows:
        return AsyncWorkflows(workflows=Workflows(server=self.server, poll_seconds=self.poll_seconds, read_attempts=self.read_attempts))
