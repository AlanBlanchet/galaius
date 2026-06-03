import json

import pytest

from interact.config import Config
from interact.models import Model
from interact.view import View

SAMPLE = json.dumps(
    {
        "providers": {
            "gemini": {
                "envKeys": ["GEMINI_API_KEY"],
                "models": {
                    "gemini/g1": {
                        "input_cost_per_million": 1.0,
                        "output_cost_per_million": 2.0,
                    }
                },
            }
        },
        "recommendations": {"component": ["gemini/g1"]},
        "coordFormats": {"gemini/": {"normalized": True, "box_order": "yxyx"}},
    }
)


@pytest.fixture(autouse=True)
def _registry():
    Model._reset()
    yield
    Model._reset()


class TestDashboardView:
    def test_sections_reflect_state_and_serialize(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("INTERACT_IMAGE_MODEL", "gemini/g1")
        Model.load_registry(SAMPLE)

        view = View.dashboard(Config())

        assert [s.title for s in view.sections] == [
            "Providers",
            "Models",
            "Grounding models ready",
        ]
        assert "gemini" in view.sections[0].metrics[0].value
        image_row = next(r for r in view.sections[1].table.rows if r["role"] == "image")
        assert image_row["model"] == "gemini/g1"
        assert any(r["model"] == "gemini/g1" for r in view.sections[2].table.rows)

        # Round-trips as JSON — the contract an HTTP endpoint serves to the web renderer.
        assert json.loads(view.model_dump_json())["title"] == "interact"
