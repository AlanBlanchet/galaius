"""The owner's levels on this PC as the web and the PC's own CLI change them.

From the web (a request signed by the server, `galaius_core.PlaceLevelRequest` and siblings): a
level applies at once, either way; whatever the server signs, a widening never opens the home folder
itself, anything outside it, a hidden name or a credential store (`PlaceMap.web_refusal`): those
open only here (`galaius machine places`). Browsing from the web lists folder names inside the home
folder, within a page budget; file names and the rest only while the owner switched it on here
(`galaius machine browse on`). A review of staged writes can be read or dropped from the web; only
the PC accepts one.

Every change, refusal, browse page and review outcome lands in the PC's own append-only
`places.log` with its digest; the server's audit keeps the digest only."""

import hashlib
import json
import sys
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from galaius_core import (
    MachineAgentAnswer, MachineAgentRequest, MachineFence, MachinePlaceReview, MachinePlacesView, PlaceBrowseRequest, PlaceCancelRequest,
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
        return MachinePlacesView(places=config.place_map().entries(), browse=config.browse,
                                 fence=MachineFence(platform=sys.platform, available=fence, on=config.fence_agents, reason=reason),
                                 suggested_sandbox="" if SUGGESTED_SANDBOX in config.places else SUGGESTED_SANDBOX)

    def request(self, path: str, level: PlaceLevel, initiator: UUID | None) -> str:
        """A level asked from the web, applied at once (a widening only where `web_refusal` allows
        it); the change's digest."""
        outcome: list[tuple[str, PlaceLevel, bool]] = []

        def change(config: "MachineConfig") -> "MachineConfig":
            places, parts = config.place_map(), split(path)
            previous, widens = places.level(parts), places.widens(path, level)
            if widens and (said := places.web_refusal(parts)) is not None:
                raise PermissionError(said)
            outcome.append((self.digest(config, uuid4(), path, level, previous), previous, widens))
            return config.with_place(path, level)

        try:
            self.runner.update(change)
        except PermissionError as refusal:
            self._log("level", path=path, level=level, method="web-refused", reason=str(refusal), initiator_account=initiator)
            raise
        digest, previous, widens = outcome[0]
        self._log("level", path=path, level=level, previous=previous, method="web-widen" if widens else "web-narrow", initiator_account=initiator, digest=digest)
        if not widens:
            self._stop_fenced()
        return digest

    def _stop_fenced(self) -> None:
        """After a narrowing: every fenced agent turn running now stops (a turn keeps the view it
        started with); its next turn is built from the narrowed levels."""
        stopped = [run.run_id for run in reg.running_runs() if run.fence is not None and reg.stop(run.run_id)]
        if stopped:
            self._log("stopped", runs=stopped, reason="a folder level was narrowed")

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
                digest = self.request(request.path, request.level, initiator)
                return MachineAgentAnswer(request_id=request.id, places=self.view(self.runner.load()), detail="applied", digest=digest)
            case PlaceCancelRequest():
                raise PermissionError("no change waits on this PC: a level asked from the web applies at once")
            case PlaceBrowseRequest():
                self.runner.browse_budget.take()
                entries, cursor = config.place_map().browse(request.path, request.cursor, everywhere=config.browse)
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
