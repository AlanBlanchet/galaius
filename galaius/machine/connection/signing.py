"""What the server signs its messages with, and what a machine checks before it acts on one."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from galaius_core import MachineAgentRequest, MachineAgentSettingsUpdate, MachineCommand, MachineDataRequest, MachineFileQuery, MachineTokenSwap
from galaius.machine.connection.config import MachineConfig


def signature(token: str, message: MachineCommand | MachineFileQuery | MachineDataRequest | MachineAgentRequest | MachineAgentSettingsUpdate | MachineTokenSwap) -> str:
    """What the server signs `message` with for the machine holding `token`: HMAC-SHA256 of its
    canonical JSON (sorted keys, no spaces, `signature` left out) keyed by SHA-256(token)."""
    unsigned = message.model_dump(mode="json", exclude={"signature"})
    return hmac.new(hashlib.sha256(token.encode()).digest(), json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def verify(config: MachineConfig, message: MachineCommand | MachineFileQuery | MachineDataRequest | MachineAgentRequest, what: str) -> None:
    """Not expired, and signed by the server with this machine's key: the server stores
    SHA-256(token), so both sides sign with the same derived key."""
    if message.expires_at.tzinfo is None or message.expires_at <= datetime.now(UTC):
        raise PermissionError(f"{what} expired")
    if not hmac.compare_digest(signature(config.token.get_secret_value(), message), message.signature):
        raise PermissionError(f"{what} signature is invalid")
