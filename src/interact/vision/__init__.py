"""VLM vision + analysis, grouped into a package.

``core`` is the VLM client (``analyze_media`` / ``analyze_screenshot`` /
``transcribe_audio``); ``critique`` builds the review/verify prompts + schemas; ``measure`` is
the deterministic (no-VLM) WCAG contrast / colour measurement; ``detect`` is VLM-driven
desktop element detection. This initializer exposes only dependency-light media types.
Analysis callers import ``interact.vision.core`` explicitly so importing a media record never
initializes the model client.
"""

from interact.vision.types import (  # noqa: F401
    MediaAnalysis as MediaAnalysis,
    MediaItem as MediaItem,
    RecordingCapture as RecordingCapture,
    RecordingResult as RecordingResult,
    VLMResult as VLMResult,
    evenly_sampled as evenly_sampled,
)
