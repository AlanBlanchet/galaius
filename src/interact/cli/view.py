"""Renderer-agnostic UI description.

The problem this solves: the dashboard exists today only as a VS Code webview, so
nothing else can show it and it's hard to size for different media. Instead, domain
objects describe *what* to show as plain typed data — :class:`View` — and each
surface (CLI text, a browser page, the VS Code webview) renders it however fits.
No widgets or HTML live in the core; renderers are thin per-surface adapters.

Build a view from domain state with :meth:`View.dashboard`; render it with a
surface adapter (e.g. ``interact.cli.render.CliRenderer``, or an HTTP endpoint that
returns ``view.model_dump_json()`` for a web renderer).
"""

from pydantic import BaseModel


class Metric(BaseModel):
    """A single labelled value (e.g. ``configured: openai, gemini``)."""

    label: str
    value: str


class Column(BaseModel):
    key: str
    label: str


class Table(BaseModel):
    """Columns + row dicts keyed by ``Column.key``. Renderers size to medium."""

    columns: list[Column]
    rows: list[dict[str, str]]


class Section(BaseModel):
    """One titled block: a few metrics and/or a table, with an optional note."""

    title: str
    metrics: list[Metric] = []
    table: Table | None = None
    note: str = ""


class View(BaseModel):
    """An ordered set of sections — the whole declarative UI for one surface."""

    title: str
    sections: list[Section] = []

    @classmethod
    def dashboard(cls, config) -> "View":
        """Assemble the status dashboard from the model registry + config.

        Pure data — no API calls, no VLM. Reads provider availability (env-key
        based), the configured model per role, and the ready grounding models.
        """
        from interact.models import Model  # heavy: pulls litellm

        providers = Model.available_providers()
        grounding = Model.recommended_grounding()
        return cls(
            title="interact",
            sections=[
                Section(
                    title="Providers",
                    metrics=[
                        Metric(label="configured", value=", ".join(providers) or "none")
                    ],
                    note=""
                    if providers
                    else "No API keys found — `interact config set OPENAI_API_KEY …`.",
                ),
                Section(
                    title="Models",
                    table=Table(
                        columns=[
                            Column(key="role", label="Role"),
                            Column(key="model", label="Configured"),
                        ],
                        rows=[
                            {"role": "image", "model": config.image_model or "(recommended)"},
                            {"role": "component", "model": config.component_model or "(falls back to image)"},
                            {"role": "video", "model": config.video_model or "(recommended)"},
                        ],
                    ),
                ),
                Section(
                    title="Grounding models ready",
                    table=Table(
                        columns=[
                            Column(key="model", label="Model"),
                            Column(key="cost", label="$/Mtok"),
                        ],
                        rows=[
                            {"model": m.id, "cost": f"{m.cost_score:.2f}"}
                            for m in grounding[:8]
                        ],
                    ),
                    note=f"{len(grounding)} available"
                    if grounding
                    else "none — check API keys (`interact doctor`).",
                ),
            ],
        )
