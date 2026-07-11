"""VLM vision + analysis, grouped into a package.

``core`` is the VLM client (media items, ``analyze_media`` / ``analyze_screenshot`` /
``transcribe_audio``, frame sampling); ``critique`` builds the review/verify prompts + schemas;
``measure`` is the deterministic (no-VLM) WCAG contrast / colour measurement; ``detect`` is
VLM-driven desktop element detection. This ``__init__`` re-exports the core VLM surface so
``from interact.vision import analyze_media`` keeps resolving; the analysis submodules are
imported by their own path (``interact.vision.critique`` etc.).
"""

from interact.vision.core import (  # noqa: F401
    _UNSET,
    MediaItem,
    VLMResult,
    _Unset,
    _audio_content,
    _build_media_content,
    _extract_frames,
    analyze_media,
    analyze_screenshot,
    evenly_sampled,
    transcribe_audio,
)
