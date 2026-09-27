import re

import pytest

from interact.models import Model
from interact.probe import ArtifactRun
from tests.support import catalog_json

# Two gemini grounding models (priced high vs free) + an unconfigured chatgpt model.
# Only "good-grounder" is a curated component recommendation. chatgpt has no configured env key
# (e.g. a subscription auth), so it is never "configured" even though it is in the catalog.
SAMPLE = catalog_json(
    ("gemini/good-grounder", 5.0, 10.0), "gemini/free-junk", "chatgpt/x",
    env_keys={"gemini": ["GEMINI_API_KEY"]},
    recommend={"component": ["gemini/good-grounder", "chatgpt/x"]},
    coord_formats={"gemini/": {"normalized": True, "box_order": "yxyx"}},
)


@pytest.fixture(autouse=True)
def _registry(reset_model_registry):
    pass


class TestGroundingModelRanking:
    def test_prefers_component_recs_over_cost_and_excludes_unconfigured(self, monkeypatch):
        """Regression: the detect default must pick a curated grounding model.

        Cheapest-first surfaced a free general/image-gen VLM that mislocates boxes.
        Ranking must put the curated ``recommendations.component`` model first even
        though it is pricier, and must never offer a provider whose key isn't set.
        """
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        Model.load_registry(SAMPLE)

        ranked = [m.id for m in Model.recommended_grounding()]

        assert ranked, "expected at least one grounding model"
        assert ranked[0] == "gemini/good-grounder"  # component rec wins over the free model
        assert "gemini/free-junk" in ranked
        assert ranked.index("gemini/good-grounder") < ranked.index("gemini/free-junk")
        assert "chatgpt/x" not in ranked  # empty envKeys ⇒ not configured ⇒ excluded

    def test_no_models_when_no_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        Model.load_registry(SAMPLE)
        assert Model.recommended_grounding() == []


class TestArtifactLayout:
    def test_dir_for_slug(self, tmp_path):
        """One dated run folder; each step is {session}_{action}_{provider}_{model}."""
        Model.load_registry(SAMPLE)
        model = Model.by_id("gemini/good-grounder")
        run = ArtifactRun(out_root=tmp_path / "20260101_120000", session_ts="20260101_120000")
        directory = run.dir_for("detect", model)
        # Stamped at call time (sequential); step index orders multi-action runs.
        assert re.fullmatch(r"\d{8}_\d{6}_detect_gemini_good-grounder", directory.name)
        assert directory.is_dir()
        stepped = run.dir_for("click", model, step=2)
        assert re.fullmatch(r"\d{8}_\d{6}_02_click_gemini_good-grounder", stepped.name)
