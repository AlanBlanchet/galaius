"""Frame-difference motion detection for recordings — decides whether a clip actually moved
and whether a captured surface is a uniform (GL-unrendered) blank."""

import io
import logging

from PIL import Image, ImageChops

_log = logging.getLogger("interact")
_MOTION_FRACTION = 0.001
_MOTION_DELTA = 10


class Motion:
    """Video motion detection helpers."""

    @staticmethod
    def is_blank(video_bytes: bytes) -> bool:
        """True if the recording's first frame is a single uniform colour — a GPU surface that
        ffmpeg x11grab couldn't read (distinct from a static-but-real frame)."""
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                vp = Path(tmpdir) / "in.mp4"
                vp.write_bytes(video_bytes)
                out = Path(tmpdir) / "f.png"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(vp), "-vframes", "1", str(out)],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                if not out.exists():
                    return False
                lo, hi = Image.open(out).convert("L").getextrema()
                return lo == hi
        except Exception:
            return False

    @staticmethod
    def detect(
        video_bytes: bytes,
        pixel_delta: int = _MOTION_DELTA,
        changed_fraction: float = _MOTION_FRACTION,
    ) -> bool:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = Path(tmpdir) / "input.mp4"
                video_path.write_bytes(video_bytes)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(video_path),
                        "-vf",
                        "select='eq(n\\,0)+eq(n\\,5)+eq(n\\,10)+eq(n\\,15)+eq(n\\,20)+eq(n\\,25)'",
                        "-vsync",
                        "vfr",
                        str(Path(tmpdir) / "frame_%02d.png"),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                frames = sorted(Path(tmpdir).glob("frame_*.png"))
                if len(frames) < 2:
                    return False
                images = [Image.open(f).convert("L") for f in frames]
                for a, b in zip(images, images[1:]):
                    diff = ImageChops.difference(a, b)
                    total_pixels = a.size[0] * a.size[1]
                    changed = sum(
                        count
                        for value, count in enumerate(diff.histogram())
                        if value > pixel_delta
                    )
                    if changed > total_pixels * changed_fraction:
                        return True
                return False
        except Exception:
            return True
