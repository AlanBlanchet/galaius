"""Write after review (`interact.place_reviews.PlaceReviews`): writes into a `write_on_review`
folder land in a staging copy; the owner accepts one exact diff by its digest, and the PC applies
exactly that diff — or nothing, when the folder or the copy changed since, or the digest differs."""

from pathlib import Path
from uuid import uuid4

import pytest

from interact.place_reviews import PlaceReviews

pytestmark = pytest.mark.usefixtures("directory_backend")


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    place = tmp_path / "home" / "notes"
    (place / "drafts").mkdir(parents=True)
    (place / "a.txt").write_text("alpha")
    (place / "drafts" / "b.txt").write_text("beta")
    (place / "old.txt").write_text("old")
    (place / ".git").mkdir()
    (place / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return place


@pytest.fixture
def reviews(tmp_path: Path) -> PlaceReviews:
    return PlaceReviews(root=tmp_path / "state" / "reviews")


def _agent_edits(tree: Path) -> None:
    (tree / "a.txt").write_text("alpha, edited")
    (tree / "drafts" / "c.txt").write_text("gamma")
    (tree / "old.txt").unlink()
    (tree / ".git" / "HEAD").write_text("ref: refs/heads/other\n")


def test_an_accepted_digest_applies_exactly_the_staged_diff(folder: Path, reviews: PlaceReviews) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    _agent_edits(tree)
    [review] = reviews.list()
    assert review.state == "ready"
    assert {(item.path, item.change) for item in review.files} == {("a.txt", "changed"), ("drafts/c.txt", "added"), ("old.txt", "deleted")}
    assert (folder / "a.txt").read_text() == "alpha"  # nothing reached the folder yet
    reviews.accept(review_id, review.digest, folder)
    assert (folder / "a.txt").read_text() == "alpha, edited" and (folder / "drafts" / "c.txt").read_text() == "gamma" and not (folder / "old.txt").exists()
    assert (folder / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"  # .git internals are never applied
    assert reviews.list() == ()


def test_a_wrong_digest_applies_nothing(folder: Path, reviews: PlaceReviews) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    _agent_edits(tree)
    with pytest.raises(PermissionError, match="digest"):
        reviews.accept(review_id, "0" * 64, folder)
    assert (folder / "a.txt").read_text() == "alpha" and (folder / "old.txt").exists()


def test_a_copy_changed_after_the_owner_looked_is_refused(folder: Path, reviews: PlaceReviews) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    _agent_edits(tree)
    [seen] = reviews.list()
    (tree / "drafts" / "d.txt").write_text("slipped in after the owner read the diff")
    with pytest.raises(PermissionError, match="digest"):
        reviews.accept(review_id, seen.digest, folder)
    assert not (folder / "drafts" / "d.txt").exists()


def test_a_folder_changed_since_staging_is_refused_whole(folder: Path, reviews: PlaceReviews) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    _agent_edits(tree)
    [seen] = reviews.list()
    (folder / "a.txt").write_text("the owner edited it meanwhile")
    with pytest.raises(PermissionError, match="changed since"):
        reviews.accept(review_id, seen.digest, folder)
    assert (folder / "old.txt").exists() and not (folder / "drafts" / "c.txt").exists()


def test_a_link_in_the_copy_blocks_the_review(folder: Path, reviews: PlaceReviews, tmp_path: Path) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    (tree / "escape").symlink_to(tmp_path)
    [review] = reviews.list()
    assert review.state == "blocked" and "link" in review.reason
    with pytest.raises(PermissionError):
        reviews.accept(review_id, review.digest or "0" * 64, folder)


def test_workflow_writes_gather_in_one_review_per_run_and_never_delete(folder: Path, reviews: PlaceReviews) -> None:
    run_id = uuid4()
    reviews.stage_file("notes", folder, ("drafts", "b.txt"), b"beta 2", origin="workflow", run_id=run_id)
    reviews.stage_file("notes", folder, ("new", "e.txt"), b"epsilon", origin="workflow", run_id=run_id)
    [review] = reviews.list()
    assert {(item.path, item.change) for item in review.files} == {("drafts/b.txt", "changed"), ("new/e.txt", "added")}
    reviews.accept(review.id, review.digest, folder)
    assert (folder / "drafts" / "b.txt").read_text() == "beta 2" and (folder / "new" / "e.txt").read_text() == "epsilon"
    assert (folder / "a.txt").exists() and (folder / "old.txt").exists()


def test_discarding_drops_the_copy_and_touches_nothing(folder: Path, reviews: PlaceReviews) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    _agent_edits(tree)
    reviews.discard(review_id)
    assert reviews.list() == () and not tree.exists() and (folder / "a.txt").read_text() == "alpha"


def test_a_review_without_changes_is_not_listed(folder: Path, reviews: PlaceReviews) -> None:
    reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    assert reviews.list() == ()


def test_a_folder_too_large_to_copy_is_refused(folder: Path, reviews: PlaceReviews) -> None:
    small = reviews.model_copy(update={"max_files": 2})
    with pytest.raises(PermissionError, match="too large"):
        small.stage_copy("notes", folder, origin="agent", run_id=None)
    assert not any(small.root.glob("*/tree"))


def test_a_new_steering_file_is_held_for_review_and_gone_from_the_folder(folder: Path, reviews: PlaceReviews, tmp_path: Path) -> None:
    """What a fenced agent creates at the top of a writable folder that an editor, git or Claude runs
    later (here `.claude/settings.json`) leaves the folder at once and waits for the owner's review."""
    (folder / ".claude").mkdir()
    (folder / ".claude" / "settings.json").write_text('{"hooks": {"Stop": [{"command": "sh -c evil"}]}}')
    (folder / ".claude" / "escape").symlink_to(tmp_path)
    reviews.hold("notes", folder, folder / ".claude")
    assert not (folder / ".claude").exists() and tmp_path.exists()
    [review] = reviews.list()
    assert [(item.path, item.change) for item in review.files] == [(".claude/settings.json", "added")]


def test_accept_writes_the_bytes_it_checked_even_if_the_copy_changes_meanwhile(folder: Path, reviews: PlaceReviews, monkeypatch: pytest.MonkeyPatch) -> None:
    """The staging copy may still be writable by its agent; what reaches the folder is what the
    digest covered, never bytes swapped in after the check."""
    import interact.place_reviews as module
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    (tree / "a.txt").write_text("reviewed text")
    [seen] = reviews.list()
    real = module._live

    def swapped(where, key):
        (tree / "a.txt").write_text("swapped after the check")  # between the digest check and the write
        return real(where, key)
    monkeypatch.setattr(module, "_live", swapped)
    reviews.accept(review_id, seen.digest, folder)
    assert (folder / "a.txt").read_text() == "reviewed text"


def test_a_review_lists_what_runs_later_first_and_says_what_it_drops(folder: Path, reviews: PlaceReviews) -> None:
    review_id, tree = reviews.stage_copy("notes", folder, origin="agent", run_id=None)
    (tree / "a.txt").write_text("changed")
    (tree / ".githooks").mkdir()
    (tree / ".githooks" / "pre-commit").write_text("#!/bin/sh\nevil\n")
    (tree / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
    [review] = reviews.list()
    assert review.files[0].path == ".githooks/pre-commit" and "1 change inside .git" in review.reason
    _, lines = reviews.read(review_id, folder, limit=None)
    assert "+evil" in lines
