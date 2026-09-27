"""Secure bounded prompt-service token file access."""

import os
from pathlib import Path
import stat


def read_prompt_token(path: Path) -> str:
    """Read one token through an owned regular descriptor without following a leaf link."""
    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is not None:
        flags |= no_follow
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("prompt token file cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        owner = getattr(os, "getuid", None)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("prompt token file must be a regular file")
        if owner is not None and metadata.st_uid != owner():
            raise ValueError("prompt token file must be owned by the current user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("prompt token file must have mode 0600")
        if metadata.st_size < 1 or metadata.st_size > 4096:
            raise ValueError("prompt token file has an invalid size")
        content = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(content) > 4096:
        raise ValueError("prompt token file has an invalid size")
    token = content.decode("utf-8").strip()
    if not token:
        raise ValueError("prompt token file is empty")
    return token
