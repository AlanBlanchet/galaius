"""The shared cache file behind the live model catalog and benchmark board."""

import json
import os

import pytest

from interact.ttl_cache import TTLCache, age_of


def test_a_write_leaves_exactly_one_file_and_no_temp():
    """A surviving temp is a leak; a temp that IS the target means the write was not atomic."""
    cache = TTLCache("thing.json")
    cache.write({"a": 1})
    assert json.loads(cache.path.read_text()) == {"a": 1}
    assert [p.name for p in cache.path.parent.iterdir()] == ["thing.json"]


def test_a_second_write_replaces_the_first_whole():
    cache = TTLCache("thing.json")
    cache.write({"n": 1})
    cache.write({"n": 2})
    assert cache.read() == {"n": 2}


def test_a_corrupt_file_reads_as_missing_rather_than_raising():
    """Both front ends read this file directly; a parse error must degrade, never crash a panel."""
    cache = TTLCache("thing.json")
    cache.path.parent.mkdir(parents=True, exist_ok=True)
    cache.path.write_text("{ half a fi")
    assert cache.read() is None


def test_a_missing_file_reads_as_missing():
    assert TTLCache("never-written.json").read() is None


def test_a_write_to_an_unwritable_place_never_raises(monkeypatch):
    cache = TTLCache("thing.json")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    cache.write({"a": 1})  # must not raise
    assert not cache.path.exists()


def test_a_never_fetched_thing_is_infinitely_old():
    assert age_of(0) == float("inf")


def test_age_counts_forward_from_the_fetch():
    import time

    assert 0 <= age_of(time.time()) < 5
