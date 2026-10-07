"""The owner's levels on this PC as the web and the PC's own CLI change them.

From the web (a request signed by the server, `galaius_core.PlaceLevelRequest` and siblings):
an earlier level applies at once; a later one only waits in `MachineConfig.pending_places` until
the owner confirms it HERE (`galaius machine approve`): the server holds the key that signs
requests, so its signature alone never opens a folder further (there is no passkey path). Whole-PC
browsing answers only while the owner switched it on here, within a page budget. A
review of staged writes can be read or dropped from the web; only the PC accepts one.

Every change, confirm, refusal, browse page and review outcome lands in the PC's own append-only
`places.log` with its digest; the server's audit keeps the digest only."""

import hashlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from galaius_core import (
    MachineAgentAnswer, MachineAgentRequest, MachineFence, MachinePlaceChange, MachinePlaceReview, MachinePlacesView, PlaceBrowseRequest, PlaceCancelRequest,
    PlaceDiscardRequest, PlaceLevel, PlaceLevelRequest, PlaceReviewRequest, PlaceReviewsRequest, PlacesRequest,
)

from galaius.agents import registry as reg
from galaius.fence import available
from galaius.places import SUGGESTED_SANDBOX, split

if TYPE_CHECKING:
    from galaius.machines import MachineConfig, MachineRunner

LOG = "places.log"


class PlaceDesk:
    """One runner's levels: read and changed under its machine-file lock (`MachineRunner.update`)."""

    def __init__(self, runner: "MachineRunner") -> None:
        self.runner = runner

    def _log(self, op: str, **entry: object) -> None:
        self.runner.audit(LOG, {"op": op, **{key: str(value) if isinstance(value, UUID) else value for key, value in entry.items()}})

    @staticmethod
    def digest(config: "MachineConfig", change_id: UUID, path: str, level: PlaceLevel, previous: PlaceLevel) -> str:
        """sha256 of the canonical change: what the PC's log and the server's audit both keep."""
        canonical = {"machine_id": str(config.machine_id), "id": str(change_id), "path": path, "level": level, "previous": previous}
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def view(self, config: "MachineConfig") -> MachinePlacesView:
        fence, reason = available()
        return MachinePlacesView(places=config.place_map().entries(), pending=config.pending_places, browse=config.browse,
                                 fence=MachineFence(platform=sys.platform, available=fence, on=config.fence_agents, reason=reason),
                                 suggested_sandbox="" if SUGGESTED_SANDBOX in config.places else SUGGESTED_SANDBOX)

    def request(self, path: str, level: PlaceLevel, initiator: UUID | None) -> tuple[MachinePlaceChange | None, str]:
        """A level asked from the web: applied when it narrows (None), else the change now waiting
        here; with the change's digest."""
        outcome: list[tuple[MachinePlaceChange | None, str, PlaceLevel]] = []

        def change(config: "MachineConfig") -> "MachineConfig":
            places, change_id = config.place_map(), uuid4()
            previous = places.level(split(path))
            digest = self.digest(config, change_id, path, level, previous)
            if not places.widens(path, level):
                outcome.append((None, digest, previous))
                return config.with_place(path, level)
            config.with_place(path, level)  # refused now (credential store, link, overlap) -> never queued
            waiting = MachinePlaceChange(id=change_id, path=path, level=level, previous=previous, asked_at=datetime.now(UTC), digest=digest)
            outcome.append((waiting, digest, previous))
            return config.model_copy(update={"pending_places": (*(item for item in config.pending_places if item.path != path), waiting)[-64:]})

        self.runner.update(change)
        waiting, digest, previous = outcome[0]
        self._log("level", path=path, level=level, previous=previous, method="web-request" if waiting else "web-narrow", initiator_account=initiator, digest=digest)
        if waiting is None:
            self._stop_fenced()
        return waiting, digest

    def _stop_fenced(self) -> None:
        """After a narrowing: every fenced agent turn running now stops (a turn keeps the view it
        started with); its next turn is built from the narrowed levels."""
        stopped = [run.run_id for run in reg.running_runs() if run.fence is not None and reg.stop(run.run_id)]
        if stopped:
            self._log("stopped", runs=stopped, reason="a folder level was narrowed")

    def cancel(self, change_id: UUID, initiator: UUID | None) -> str:
        found: list[MachinePlaceChange] = []

        def drop(config: "MachineConfig") -> "MachineConfig":
            found.extend(item for item in config.pending_places if item.id == change_id)
            return config.model_copy(update={"pending_places": tuple(item for item in config.pending_places if item.id != change_id)})

        self.runner.update(drop)
        if not found:
            raise PermissionError("no such widening waits on this PC (already confirmed or withdrawn)")
        self._log("cancel", path=found[0].path, level=found[0].level, method="web-cancel" if initiator else "pc", initiator_account=initiator, digest=found[0].digest)
        return found[0].digest

    def approve(self, prefix: str | None, confirm: Callable[[MachinePlaceChange], bool]) -> list[MachinePlaceChange]:
        """The owner, on this PC, confirms the waiting widenings (all, or the one whose id starts
        with `prefix`), each re-checked against the folder as it is now; `confirm` asks him."""
        applied = []
        for waiting in self.runner.load().pending_places:
            if prefix and not str(waiting.id).startswith(prefix):
                continue
            if not confirm(waiting):
                continue
            try:
                def confirmed(config: "MachineConfig", waiting: MachinePlaceChange = waiting) -> "MachineConfig":
                    if waiting not in config.pending_places:
                        raise PermissionError("this widening was withdrawn meanwhile")
                    return config.with_place(waiting.path, waiting.level)
                self.runner.update(confirmed)
            except PermissionError as error:
                self.runner.update(lambda config, waiting=waiting: config.model_copy(update={"pending_places": tuple(item for item in config.pending_places if item.id != waiting.id)}))
                self._log("level", path=waiting.path, level=waiting.level, method="pc-refused", reason=str(error), digest=waiting.digest)
                continue
            self._log("level", path=waiting.path, level=waiting.level, previous=waiting.previous, method="pc-confirm", digest=waiting.digest)
            applied.append(waiting)
        return applied

    def set_here(self, path: str, level: PlaceLevel) -> "MachineConfig":
        """The owner sets a level on this PC itself: applied at once, whichever way it goes."""
        places = self.runner.load().place_map()
        before, widens = places.level(split(path)), places.widens(path, level)
        config = self.runner.update(lambda current: current.with_place(path, level))
        self._log("level", path=path, level=level, previous=before, method="pc", digest=self.digest(config, uuid4(), path, level, before))
        if not widens:
            self._stop_fenced()
        return config

    def accept(self, review_id: UUID, digest: str) -> MachinePlaceReview:
        """The owner, on this PC, applies one review whose digest he read (its folder must still be write-after-review)."""
        config = self.runner.load()
        place = self.runner.reviews.place(review_id)
        if config.place_map().reach(split(place)) != "write_on_review":
            raise PermissionError(f"{place} is no longer write-after-review here: discard this review")
        review = self.runner.reviews.accept(review_id, digest, config.place_map().base.joinpath(*split(place)))
        self._log("review", review_id=review_id, place=place, outcome="accepted", method="pc-confirm", files=len(review.files), digest=review.digest)
        return review

    def answer(self, request: MachineAgentRequest) -> MachineAgentAnswer:
        """One web request about places (already checked: this machine, signed, not expired)."""
        config, reviews = self.runner.load(), self.runner.reviews
        initiator = getattr(request, "initiator_account", None)
        match request:
            case PlacesRequest():
                return MachineAgentAnswer(request_id=request.id, places=self.view(config))
            case PlaceLevelRequest():
                waiting, digest = self.request(request.path, request.level, initiator)
                detail = "waiting on the PC: its owner confirms it there with `galaius machine approve`" if waiting else "applied"
                return MachineAgentAnswer(request_id=request.id, places=self.view(self.runner.load()), change=waiting, detail=detail, digest=digest)
            case PlaceCancelRequest():
                digest = self.cancel(request.change_id, initiator)
                return MachineAgentAnswer(request_id=request.id, places=self.view(self.runner.load()), detail="withdrawn", digest=digest)
            case PlaceBrowseRequest():
                if not config.browse:
                    raise PermissionError("browsing this PC from the web is off; its owner turns it on there with `galaius machine browse on`")
                self.runner.browse_budget.take()
                entries, cursor = config.place_map().browse(request.path, request.cursor)
                self._log("browse", path=request.path, cursor=request.cursor, initiator_account=initiator)
                return MachineAgentAnswer(request_id=request.id, browse=entries, cursor=cursor, truncated=cursor is not None)
            case PlaceReviewsRequest():
                return MachineAgentAnswer(request_id=request.id, reviews=reviews.list()[-100:])
            case PlaceReviewRequest():
                place = reviews.place(request.review_id)
                review, lines = reviews.read(request.review_id, config.place_map().base.joinpath(*split(place)))
                return MachineAgentAnswer(request_id=request.id, reviews=(review,), lines=lines)
            case PlaceDiscardRequest():
                review = reviews.discard(request.review_id)
                self._log("review", review_id=request.review_id, place=review.place, outcome="discarded", method="web", initiator_account=initiator, digest=review.digest)
                return MachineAgentAnswer(request_id=request.id, detail="discarded", digest=review.digest)
        raise PermissionError(f"{request.op} is not a place request")
