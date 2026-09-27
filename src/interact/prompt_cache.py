"""Verified local cache for immutable prompt revisions and channel pointers."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

from interact_core import (
    PromptCatalogPage,
    PromptChannelEntry,
    PromptExecutionRef,
    PromptKey,
    PromptRevision,
)


class _PromptCache:
    """Persist verified prompt revisions and resolve only an exact published digest."""

    def __init__(self, path: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            owner = getattr(os, "getuid", None)
            if not stat.S_ISREG(metadata.st_mode) or (
                owner is not None and metadata.st_uid != owner()
            ):
                raise OSError("prompt cache must be an owned regular file")
            os.fchmod(descriptor, 0o600)
            database = sqlite3.connect(path)
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                database.close()
                raise OSError("prompt cache changed before SQLite opened it")
        finally:
            os.close(descriptor)
        self._database = database
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS revisions (account TEXT NOT NULL, digest TEXT NOT NULL, namespace TEXT NOT NULL, slug TEXT NOT NULL, revision TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(account, namespace, slug, digest))"
        )
        primary_key = tuple(
            row[1] for row in sorted(
                self._database.execute("PRAGMA table_info(revisions)"), key=lambda row: row[5]
            ) if row[5]
        )
        if primary_key != ("account", "namespace", "slug", "digest"):
            with self._database:
                self._database.execute(
                    "CREATE TABLE revisions_keyed (account TEXT NOT NULL, digest TEXT NOT NULL, namespace TEXT NOT NULL, slug TEXT NOT NULL, revision TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(account, namespace, slug, digest))"
                )
                self._database.execute(
                    "INSERT INTO revisions_keyed SELECT account, digest, namespace, slug, revision, content FROM revisions"
                )
                self._database.execute("DROP TABLE revisions")
                self._database.execute("ALTER TABLE revisions_keyed RENAME TO revisions")
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS channels (account TEXT NOT NULL, namespace TEXT NOT NULL, slug TEXT NOT NULL, channel TEXT NOT NULL, revision TEXT NOT NULL, digest TEXT NOT NULL, lock_version INTEGER NOT NULL, PRIMARY KEY(account, namespace, slug, channel))"
        )
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS catalogs (account TEXT PRIMARY KEY, server_timestamp TEXT NOT NULL, cursor TEXT)"
        )

    def close(self) -> None:
        self._database.close()

    def apply(
        self, account: str, page: PromptCatalogPage, revisions: tuple[PromptRevision, ...]
    ) -> None:
        by_identity = {
            (revision.key.namespace, revision.key.slug, revision.digest): revision
            for revision in revisions
        }
        for entry in page.entries:
            revision = by_identity.get(
                (entry.key.namespace, entry.key.slug, entry.digest)
            )
            if (
                revision is None
                or revision.key != entry.key
                or revision.revision != entry.revision
            ):
                raise ValueError("catalog entry has no matching verified revision")
        with self._database:
            self._database.execute(
                "DELETE FROM channels WHERE account=?", (account,)
            )
            for entry in page.entries:
                revision = by_identity[
                    (entry.key.namespace, entry.key.slug, entry.digest)
                ]
                self._database.execute(
                    "INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?, ?, ?)",
                    (account, revision.digest, revision.key.namespace, revision.key.slug, str(revision.revision), revision.content),
                )
                self._database.execute(
                    "INSERT INTO channels VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account, namespace, slug, channel) DO UPDATE SET revision=excluded.revision, digest=excluded.digest, lock_version=excluded.lock_version WHERE excluded.lock_version > channels.lock_version",
                    (account, entry.key.namespace, entry.key.slug, entry.channel, str(entry.revision), entry.digest, entry.lock_version),
                )
            self._database.execute(
                "INSERT INTO catalogs VALUES (?, ?, ?) ON CONFLICT(account) DO UPDATE SET server_timestamp=excluded.server_timestamp, cursor=excluded.cursor",
                (account, page.server_timestamp.isoformat(), page.cursor),
            )

    def catalog(self, account: str) -> PromptCatalogPage:
        timestamp = self._database.execute(
            "SELECT server_timestamp, cursor FROM catalogs WHERE account=?", (account,)
        ).fetchone()
        if timestamp is None:
            raise LookupError("prompt catalog is not cached")
        rows = self._database.execute(
            "SELECT namespace, slug, channel, revision, digest, lock_version FROM channels WHERE account=? ORDER BY namespace, slug, channel",
            (account,),
        )
        return PromptCatalogPage(
            entries=tuple(
                PromptChannelEntry(
                    key=PromptKey(namespace=row[0], slug=row[1]), channel=row[2],
                    revision=row[3], digest=row[4], lock_version=row[5],
                )
                for row in rows
            ),
            cursor=timestamp[1],
            server_timestamp=timestamp[0],
        )

    def resolve(self, account: str, key: PromptKey, channel: str, digest: str) -> str:
        row = self._database.execute(
            "SELECT revisions.content, channels.digest FROM channels JOIN revisions ON revisions.account=channels.account AND revisions.namespace=channels.namespace AND revisions.slug=channels.slug AND revisions.digest=channels.digest WHERE channels.account=? AND channels.namespace=? AND channels.slug=? AND channels.channel=?",
            (account, key.namespace, key.slug, channel),
        ).fetchone()
        if row is None or row[1] != digest:
            raise LookupError("exact prompt revision is not cached")
        return str(row[0])

    def resolve_execution(
        self, account: str, key: PromptKey, channel: str, digest: str
    ) -> tuple[str, PromptExecutionRef]:
        content = self.resolve(account, key, channel, digest)
        entry = next(
            (item for item in self.catalog(account).entries
             if item.key == key and item.channel == channel and item.digest == digest),
            None,
        )
        if entry is None:
            raise LookupError("exact prompt revision is not cached")
        return content, PromptExecutionRef(
            key=entry.key,
            channel=entry.channel,
            revision=entry.revision,
            digest=entry.digest,
        )
