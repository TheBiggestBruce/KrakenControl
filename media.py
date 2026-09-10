from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError


DISPLAY_SIZE = (640, 640)
MAX_GIF_BYTES = 23_000_000
MAX_VIDEO_SECONDS = 30.0
FONT_PATH = Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf")
DEFAULT_OVERLAY_STYLE: dict[str, object] = {
    "size": 1.0,
    "x": 0.5,
    "y": 0.76,
    "text_color": "#f4f7f2",
    "accent_color": "#a855f7",
    "background_color": "#050809",
    "background_opacity": 0.78,
}


class MediaError(RuntimeError):
    pass


def _fit_image(
    image: Image.Image, fit: str, focus_x: float = 0.5, focus_y: float = 0.5
) -> Image.Image:
    image = image.convert("RGB")
    if fit == "cover":
        return ImageOps.fit(
            image,
            DISPLAY_SIZE,
            method=Image.Resampling.LANCZOS,
            centering=(focus_x, focus_y),
        )
    if fit == "stretch":
        return image.resize(DISPLAY_SIZE, Image.Resampling.LANCZOS)
    if fit != "contain":
        raise MediaError("Fit must be contain, cover, or stretch")

    fitted = ImageOps.contain(image, DISPLAY_SIZE, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", DISPLAY_SIZE, "black")
    canvas.paste(fitted, ((640 - fitted.width) // 2, (640 - fitted.height) // 2))
    return canvas


def _is_animated_image(source: Path) -> bool:
    try:
        with Image.open(source) as image:
            return bool(getattr(image, "is_animated", False))
    except UnidentifiedImageError:
        return False


def telemetry_lines(mode: str, values: dict[str, float]) -> list[tuple[str, float]]:
    keys = {
        "liquid": ("LIQUID",),
        "cpu": ("CPU",),
        "gpu": ("GPU",),
        "cpu_gpu": ("CPU", "GPU"),
    }.get(mode, ())
    return [(key, values[key.lower()]) for key in keys if key.lower() in values]


def draw_telemetry(
    image: Image.Image,
    mode: str,
    values: dict[str, float],
    style: dict[str, object] | None = None,
) -> Image.Image:
    lines = telemetry_lines(mode, values)
    if not lines:
        return image
    options = {**DEFAULT_OVERLAY_STYLE, **(style or {})}
    scale = float(options["size"])
    center_x = int(image.width * float(options["x"]))
    center_y = int(image.height * float(options["y"]))
    text_color = str(options["text_color"])
    accent_color = str(options["accent_color"])
    background = ImageColor.getrgb(str(options["background_color"]))
    opacity = int(255 * float(options["background_opacity"]))
    image = image.convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    count = len(lines)
    width = int((390 if count == 2 else 250) * scale)
    height = int(105 * scale)
    left = center_x - width // 2
    top = center_y - height // 2
    draw.rounded_rectangle(
        (left, top, left + width, top + height),
        int(22 * scale),
        fill=(*background, opacity),
    )
    value_font = ImageFont.truetype(str(FONT_PATH), max(18, int(48 * scale)))
    label_font = ImageFont.truetype(str(FONT_PATH), max(9, int(17 * scale)))
    column_width = width / count
    for index, (label, value) in enumerate(lines):
        center = left + column_width * (index + 0.5)
        draw.text(
            (center, top + int(10 * scale)),
            f"{value:.0f}°",
            anchor="ma",
            font=value_font,
            fill=text_color,
        )
        draw.text(
            (center, top + int(76 * scale)),
            label,
            anchor="ma",
            font=label_font,
            fill=accent_color,
        )
    return image


def _prepare_static(
    source: Path,
    output: Path,
    fit: str,
    focus_x: float,
    focus_y: float,
    overlay: str,
    values: dict[str, float],
    overlay_style: dict[str, object],
) -> tuple[Path, str]:
    try:
        with Image.open(source) as image:
            prepared = _fit_image(image, fit, focus_x, focus_y)
            draw_telemetry(prepared, overlay, values, overlay_style).save(
                output, format="PNG", optimize=True
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise MediaError(f"Could not decode image: {exc}") from exc
    return output, "static"


def _video_filter(
    size: int,
    colors: int,
    fps: int,
    fit: str,
    focus_x: float,
    focus_y: float,
    overlay: str,
    values: dict[str, float],
    overlay_style: dict[str, object],
) -> str:
    if fit == "cover":
        geometry = (
            f"scale={size}:{size}:force_original_aspect_ratio=increase,"
            f"crop={size}:{size}:(in_w-{size})*{focus_x}:(in_h-{size})*{focus_y}"
        )
    elif fit == "stretch":
        geometry = f"scale={size}:{size}"
    else:
        geometry = (
            f"scale={size}:{size}:force_original_aspect_ratio=decrease,"
            f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    base = f"fps={fps},{geometry}" if fps else geometry
    lines = telemetry_lines(overlay, values)
    if lines:
        options = {**DEFAULT_OVERLAY_STYLE, **overlay_style}
        scale = float(options["size"])
        count = len(lines)
        box_width = int(size * (0.61 if count == 2 else 0.39) * scale)
        box_height = int(size * 0.165 * scale)
        box_x = int(size * float(options["x"]) - box_width / 2)
        box_y = int(size * float(options["y"]) - box_height / 2)
        background = str(options["background_color"]).replace("#", "0x")
        opacity = float(options["background_opacity"])
        text_color = str(options["text_color"]).replace("#", "0x")
        accent_color = str(options["accent_color"]).replace("#", "0x")
        base += f",drawbox=x={box_x}:y={box_y}:w={box_width}:h={box_height}:color={background}@{opacity}:t=fill"
        column_width = box_width / count
        for index, (label, value) in enumerate(lines):
            center = int(box_x + column_width * (index + 0.5))
            base += (
                f",drawtext=fontfile={FONT_PATH}:text='{value:.0f}°':"
                f"fontcolor={text_color}:fontsize={max(18, int(size * 0.075 * scale))}:x={center}-text_w/2:y={box_y + int(8 * scale)}"
                f",drawtext=fontfile={FONT_PATH}:text='{label}':"
                f"fontcolor={accent_color}:fontsize={max(9, int(size * 0.027 * scale))}:x={center}-text_w/2:y={box_y + int(box_height * 0.72)}"
            )
    return (
        f"{base},split[a][b];"
        f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )


def _prepare_animation(
    source: Path,
    output: Path,
    fit: str,
    fps: int,
    start: float,
    duration: float,
    focus_x: float,
    focus_y: float,
    overlay: str,
    values: dict[str, float],
    overlay_style: dict[str, object],
    palette_colors: int,
) -> tuple[Path, str]:
    if not 0 <= fps <= 30:
        raise MediaError("FPS must be original (0) or between 1 and 30")
    if not 0 <= start:
        raise MediaError("Start time cannot be negative")
    if not 0.1 <= duration <= MAX_VIDEO_SECONDS:
        raise MediaError(f"Duration must be between 0.1 and {MAX_VIDEO_SECONDS:g} seconds")

    attempts = tuple(
        (size, min(colors, palette_colors))
        for size, colors in ((640, 192), (560, 160), (480, 128), (400, 96), (320, 64))
    )
    last_error = ""
    for size, colors in attempts:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if start:
            command += ["-ss", str(start)]
        command += [
            "-i",
            str(source),
            "-t",
            str(duration),
            "-filter_complex",
            _video_filter(
                size, colors, fps, fit, focus_x, focus_y, overlay, values, overlay_style
            ),
            "-loop",
            "0",
            str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode:
            last_error = result.stderr.strip()
            continue
        if output.stat().st_size <= MAX_GIF_BYTES:
            return output, "gif"

    output.unlink(missing_ok=True)
    detail = f": {last_error}" if last_error else ""
    raise MediaError(
        "The animation cannot fit in the Kraken's 23 MB safety limit. "
        f"Choose a shorter clip{detail}"
    )


def prepare_media(
    source: Path,
    output_dir: Path,
    fit: str = "contain",
    fps: int = 24,
    start: float = 0,
    duration: float = 10,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
    overlay: str = "none",
    telemetry: dict[str, float] | None = None,
    overlay_style: dict[str, object] | None = None,
    palette_colors: int = 128,
) -> tuple[Path, str]:
    if not 0 <= focus_x <= 1 or not 0 <= focus_y <= 1:
        raise MediaError("Crop focus must be between 0 and 1")
    if overlay not in {"none", "liquid", "cpu", "gpu", "cpu_gpu"}:
        raise MediaError("Unsupported telemetry overlay")
    if palette_colors not in {64, 128, 192}:
        raise MediaError("GIF palette must use 64, 128, or 192 colors")
    values = telemetry or {}
    style = {**DEFAULT_OVERLAY_STYLE, **(overlay_style or {})}
    try:
        if not 0.5 <= float(style["size"]) <= 1.75:
            raise ValueError
        if not 0 <= float(style["x"]) <= 1 or not 0 <= float(style["y"]) <= 1:
            raise ValueError
        if not 0 <= float(style["background_opacity"]) <= 1:
            raise ValueError
        for key in ("text_color", "accent_color", "background_color"):
            ImageColor.getrgb(str(style[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaError("Invalid telemetry overlay style") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    animated = _is_animated_image(source)
    if not animated:
        try:
            with Image.open(source):
                pass
        except (OSError, UnidentifiedImageError):
            animated = True

    if animated:
        return _prepare_animation(
            source,
            output_dir / "display.gif",
            fit,
            fps,
            start,
            duration,
            focus_x,
            focus_y,
            overlay,
            values,
            style,
            palette_colors,
        )
    return _prepare_static(
        source,
        output_dir / "display.png",
        fit,
        focus_x,
        focus_y,
        overlay,
        values,
        style,
    )


def render_preview(
    source: Path,
    fit: str,
    focus_x: float,
    focus_y: float,
    overlay: str,
    telemetry: dict[str, float],
    overlay_style: dict[str, object],
    start: float = 0,
) -> bytes:
    try:
        with Image.open(source) as image:
            frame = image.convert("RGB")
    except (OSError, UnidentifiedImageError):
        with tempfile.TemporaryDirectory(prefix="kraken-preview-") as directory:
            frame_path = Path(directory) / "frame.png"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            if start:
                command += ["-ss", str(start)]
            command += ["-i", str(source), "-frames:v", "1", str(frame_path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise MediaError(f"Could not render preview: {result.stderr.strip()}")
            with Image.open(frame_path) as image:
                frame = image.convert("RGB")

    rendered = draw_telemetry(
        _fit_image(frame, fit, focus_x, focus_y), overlay, telemetry, overlay_style
    )
    output = io.BytesIO()
    rendered.save(output, "PNG", optimize=True)
    return output.getvalue()
