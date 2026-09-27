import difflib
import logging
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict

from interact.desktop import DesktopElement
from interact.parsing import Parse

_log = logging.getLogger(__name__)

#: The names models give a bounding box, in the order we try them after the format's own key.
_BOX_KEYS = ("box_2d", "bbox_2d", "box", "bbox")


class BoxOrder(StrEnum):
    """How bounding box values are sequenced in model output."""

    XYXY = "xyxy"
    YXYX = "yxyx"
    XYWH = "xywh"


class CoordFormat(BaseModel):
    """Typed coordinate format spec for a VLM model's grounding output.

    A ``prefix`` of ``""`` denotes the implicit default (pixel coords).
    Lookup uses :meth:`for_model`; registry is populated by
    :meth:`load_from_config` from the extension's ``coordFormats`` block.
    """

    model_config = ConfigDict(frozen=True)

    prefix: str = ""
    normalized: bool = False
    box_order: BoxOrder = BoxOrder.XYWH
    box_key: str = ""
    divisor: int = 1000
    prompt_template: str = ""

    # Per-class registry. We deliberately don't inherit RegistryMixin
    # (its `id` lookup doesn't fit prefix-based matching) but mirror the
    # same shape so callers feel uniform.
    _registry: ClassVar[list["CoordFormat"]] = []

    @classmethod
    def registry(cls) -> list["CoordFormat"]:
        return cls._registry

    @classmethod
    def _register(cls, item: "CoordFormat") -> None:
        for i, ex in enumerate(cls._registry):
            if ex.prefix == item.prefix:
                cls._registry[i] = item
                return
        cls._registry.append(item)

    @classmethod
    def _reset(cls) -> None:
        cls._registry.clear()

    @classmethod
    def for_model(cls, model_id: str) -> Self:
        """Return the registered format whose prefix matches ``model_id``,
        or a default :class:`CoordFormat` instance when none match."""
        m = model_id.lower()
        for fmt in cls._registry:
            if fmt.prefix and m.startswith(fmt.prefix.lower()):
                return fmt  # type: ignore[return-value]
        _log.debug("No coord format registered for model %r", model_id)
        return cls()  # type: ignore[return-value]

    @classmethod
    def load_from_config(cls, config_map: dict[str, dict]) -> None:
        """Replace the registry with formats built from ``{prefix: spec}``."""
        cls._reset()
        for prefix, spec in config_map.items():
            if not isinstance(spec, dict):
                continue
            cls._register(cls(prefix=prefix, **spec))

    @staticmethod
    def extract_json_list(response: str) -> list | None:
        """Extract a JSON list of elements from a VLM response string."""
        parsed = Parse.try_json(response)
        raw_list: list | None
        if isinstance(parsed, dict) and isinstance(parsed.get("elements"), list):
            raw_list = parsed["elements"]
        elif isinstance(parsed, list):
            raw_list = parsed
        else:
            raw_list = None
        if raw_list is None:
            raw_list = Parse.extract_json_array(response)
        return raw_list

    def prompt(self, img_w: int, img_h: int) -> str:
        if self.prompt_template:
            return self.prompt_template.replace("{w}", str(img_w)).replace(
                "{h}", str(img_h)
            )
        return (
            f"Return pixel coordinates where (0,0)=top-left, x ranges 0-{img_w}, y ranges 0-{img_h}. "
            f"A full-width element has x\u22480, w\u2248{img_w}. "
            '[{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":30}]'
        )

    def meta(self) -> dict:
        """Spec for artifact dumps. The divisor only applies to normalized coords,
        so it's dropped for pixel formats (e.g. qwen's absolute ``bbox_2d``) where
        showing ``divisor: 1000`` is misleading."""
        data = self.model_dump()
        if not self.normalized:
            data.pop("divisor", None)
        return data

    def _box_of(self, entry: dict) -> list | None:
        """The entry's box: under the key we asked for, a known alias, or — last — a key the model
        MISSPELLED (`box_2dd`, #122). A four-number list under a name one edit away from a box key
        is a box — dropping it silently once turned one typo into "0 elements"; it's logged, and
        never guessed from any random four-list (a `color: [r, g, b, a]` stays what it is)."""
        asked = (self.box_key,) if self.box_key else ()
        for key in asked + _BOX_KEYS:
            box = entry.get(key)
            if box:
                return box
        known = set(asked) | set(_BOX_KEYS)
        for key, value in entry.items():
            if not (isinstance(value, list) and len(value) >= 4):
                continue
            if difflib.get_close_matches(str(key).lower(), known, n=1, cutoff=0.8):
                _log.warning("VLM wrote a box under %r — read as a box (#122)", key)
                return value
        return None

    def parse(
        self, response: str, img_w: int, img_h: int
    ) -> list[DesktopElement] | None:
        """Parse VLM response into elements in pixel coords of the VLM image."""
        raw_list = CoordFormat.extract_json_list(response)
        if raw_list is None:
            fallback = DesktopElement.parse_vlm(response)
            if not fallback:
                return None
            raw_list = [element.model_dump() for element in fallback]

        raw_entries: list[tuple[int, int, int, int, str, str]] = []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            try:
                role = str(entry.get("role", entry.get("label", "element")))
                name = str(entry.get("name", entry.get("label", "")))

                box = self._box_of(entry)

                if box and isinstance(box, list) and len(box) >= 4:
                    vals = [int(v) for v in box[:4]]
                    match self.box_order:
                        case BoxOrder.YXYX:
                            ymin, xmin, ymax, xmax = vals
                        case BoxOrder.XYXY:
                            xmin, ymin, xmax, ymax = vals
                        case BoxOrder.XYWH:
                            raw_entries.append(
                                (vals[0], vals[1], vals[2], vals[3], role, name)
                            )
                            continue
                    raw_entries.append(
                        (xmin, ymin, xmax - xmin, ymax - ymin, role, name)
                    )
                else:
                    element = DesktopElement.from_vlm_dict(entry, 0)
                    raw_entries.append(
                        (
                            element.x,
                            element.y,
                            element.w,
                            element.h,
                            role,
                            name,
                        )
                    )
            except (ValueError, TypeError):
                continue

        if not raw_entries:
            return None

        sx = img_w / self.divisor if self.normalized else 1.0
        sy = img_h / self.divisor if self.normalized else 1.0

        elements: list[DesktopElement] = []
        for i, (x, y, w, h, role, name) in enumerate(raw_entries):
            px, py = int(x * sx), int(y * sy)
            pw, ph = max(1, int(w * sx)), max(1, int(h * sy))
            if pw <= 0 or ph <= 0 or px < 0 or py < 0:
                continue
            elements.append(
                DesktopElement(
                    index=i + 1, x=px, y=py, w=pw, h=ph, role=role, name=name
                )
            )
        return elements or None
