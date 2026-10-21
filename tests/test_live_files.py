"""A live run file is written where it is, never through a link (2026-10-21: a scratch link let a shell redirect empty a
live file, 769 rows)."""

import pytest

from galaius.agents import registry as reg


def test_a_live_file_that_is_a_link_is_refused_and_its_target_untouched(tmp_path):
    elsewhere = tmp_path / "scratch-copy.json"
    elsewhere.write_text('{"rows": [1, 2, 3]}')
    live = reg.agents_dir() / "outbox-receipts.json"
    reg._ensure_registry_directory()
    live.symlink_to(elsewhere)
    with pytest.raises(OSError, match="is a link"):
        reg._replace_private(live, b"{}")
    assert live.is_symlink() and elsewhere.read_text() == '{"rows": [1, 2, 3]}'
