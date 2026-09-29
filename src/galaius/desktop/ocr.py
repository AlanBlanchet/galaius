"""Read the text on a captured frame — what ``wait_for text=`` polls on a desktop target.

One interface, one engine per OS, each the platform's own where it has one:

* Windows → :class:`WindowsOcr`: ``Windows.Media.Ocr`` (built into Windows 10+, via WinRT).
* macOS → :class:`VisionOcr`: Vision ``VNRecognizeTextRequest`` (via pyobjc).
* Linux, or any OS as fallback → :class:`TesseractOcr`: the ``tesseract`` binary.

Why OCR and not pixel differences: a blinking caret changes pixels but is not text, so it can
neither satisfy nor block a text wait (the failed ``screen="changes"`` design, host_desktop.md)."""

import asyncio
import functools
import io
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod

from PIL import Image, ImageOps


class NoTextReader(RuntimeError):
    """No OCR engine can run here; the message says what to install."""


class TextReader(ABC):
    """PNG in, the text on it out (lines joined by newlines, reading order as the engine gives)."""

    @abstractmethod
    def read(self, png: bytes) -> str: ...


def _grey_dark_on_light(png: bytes, min_height: int) -> Image.Image:
    """Grey, dark text on a light ground, upscaled until ``min_height`` — the input tesseract reads
    best; screen text is small and launchers are often light-on-dark."""
    img = Image.open(io.BytesIO(png)).convert("L")
    if sum(i * n for i, n in enumerate(img.histogram())) / (img.width * img.height or 1) < 110:
        img = ImageOps.invert(img)
    if img.height < min_height:
        scale = min(3, -(-min_height // img.height))
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)
    return img


class TesseractOcr(TextReader):
    """The ``tesseract`` binary, two page-segmentation passes: ``--psm 11`` (sparse text, scattered
    labels) and ``--psm 6`` (one uniform block). Measured on a launcher over a dark or coloured
    desktop, psm 11 alone dropped the result row that psm 6 read; the passes come back form-feed
    separated so words of different passes never join into one match."""

    _PAGE_MODES = ("11", "6")

    _MIN_HEIGHT = 1200  # ~2x a typical screen region; small UI text below this reads poorly

    def __init__(self):
        self.binary = shutil.which("tesseract")
        if self.binary is None:
            raise NoTextReader(
                "no OCR engine: install tesseract (Linux: `apt install tesseract-ocr`, macOS: "
                "`brew install tesseract`, Windows: the UB-Mannheim installer) for wait_for text= "
                "on a desktop target"
            )

    def read(self, png: bytes) -> str:
        buf = io.BytesIO()
        _grey_dark_on_light(png, self._MIN_HEIGHT).save(buf, format="PNG")
        return "\f".join(
            subprocess.run(
                [self.binary, "stdin", "stdout", "--psm", mode, "-l", "eng"],
                input=buf.getvalue(), capture_output=True, timeout=60, check=True,
            ).stdout.decode("utf-8", "replace")
            for mode in self._PAGE_MODES
        )


class WindowsOcr(TextReader):
    """``Windows.Media.Ocr`` in the user's profile languages — no install, no model download."""

    def __init__(self):
        try:
            from winrt.windows.media.ocr import OcrEngine  # noqa: PLC0415 — Windows-only binding
        except ImportError as exc:
            raise NoTextReader(f"Windows OCR binding missing ({exc})") from exc
        self._engine = OcrEngine.try_create_from_user_profile_languages()
        if self._engine is None:
            raise NoTextReader("Windows OCR has no language pack for this user's profile languages")
        self._max_side = int(OcrEngine.max_image_dimension)

    def read(self, png: bytes) -> str:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if max(img.size) > self._max_side:  # the engine refuses larger images outright
            img.thumbnail((self._max_side, self._max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return asyncio.run(self._recognize(buf.getvalue()))

    async def _recognize(self, png: bytes) -> str:
        from winrt.windows.graphics.imaging import BitmapDecoder  # noqa: PLC0415 — Windows-only
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream  # noqa: PLC0415

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png)
        await writer.store_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await self._engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)


class VisionOcr(TextReader):
    """macOS Vision text recognition, accurate level, no language correction (UI labels are not
    prose, and correction "fixes" app names)."""

    def __init__(self):
        try:
            import Vision  # noqa: PLC0415 — macOS-only binding
            from Foundation import NSData  # noqa: PLC0415
        except ImportError as exc:
            raise NoTextReader(f"macOS Vision binding missing ({exc})") from exc
        self._vision, self._nsdata = Vision, NSData

    def read(self, png: bytes) -> str:
        vision = self._vision
        handler = vision.VNImageRequestHandler.alloc().initWithData_options_(
            self._nsdata.dataWithBytes_length_(png, len(png)), None
        )
        request = vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(f"macOS Vision OCR failed: {error}")
        lines = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if candidates:
                lines.append(str(candidates[0].string()))
        return "\n".join(lines)


_NATIVE: dict[str, type[TextReader]] = {"win32": WindowsOcr, "darwin": VisionOcr}


@functools.cache
def text_reader() -> TextReader:
    """This OS's engine, else tesseract, else :class:`NoTextReader` naming what to install."""
    native = _NATIVE.get(sys.platform)
    if native is not None:
        try:
            return native()
        except NoTextReader as exc:
            if shutil.which("tesseract") is None:
                raise NoTextReader(f"{exc}; and no tesseract binary to fall back to") from exc
    return TesseractOcr()
