"""Pillow-based generator for branded 9:16 YouTube Shorts thumbnails."""

from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

THUMBNAIL_SIZE = (1080, 1920)
MAX_INPUT_BYTES = 25_000_000
MAX_OUTPUT_BYTES = 50_000_000
MAX_IMAGE_DIMENSION = 10_000


def _open_image(content: bytes, *, label: str) -> Image.Image:
    if not isinstance(content, bytes) or not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} image must be non-empty and under 25 MB.")
    try:
        with Image.open(BytesIO(content)) as source:
            if (
                source.width < 1
                or source.height < 1
                or source.width > MAX_IMAGE_DIMENSION
                or source.height > MAX_IMAGE_DIMENSION
            ):
                raise ValueError(f"{label} image dimensions are outside supported limits.")
            source.load()
            return source.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"{label} image is not a supported image file.") from exc


def _wrap_title(title: str, *, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> list[str]:
    words = title.split()
    lines: list[str] = []
    current = ""
    max_width = THUMBNAIL_SIZE[0] - 144
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        if draw.textlength(word, font=font) > max_width:
            raise ValueError("A title word is too wide for the thumbnail layout.")
        current = word
    if current:
        lines.append(current)
    if len(lines) > 3:
        lines = lines[:3]
        last = lines[-1]
        while last and draw.textlength(f"{last}…", font=font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = f"{last}…"
    return lines


def generate_thumbnail(
    *,
    title: str,
    background_image: bytes,
    channel_logo: bytes | None = None,
    font_path: str | None = None,
) -> bytes:
    """Compose a 1080x1920 JPEG from a background, channel logo and title.

    Supply a font path for scripts not covered by Pillow's built-in default font.
    The function performs no file or network I/O.
    """
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError("Thumbnail title must contain 1 to 160 characters.")
    background = _open_image(background_image, label="Background")
    logo = _open_image(channel_logo, label="Channel logo") if channel_logo else None

    canvas = ImageOps.fit(
        background.convert("RGB"),
        THUMBNAIL_SIZE,
        method=Image.Resampling.LANCZOS,
    ).convert("RGBA")
    overlay = Image.new("RGBA", THUMBNAIL_SIZE, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle((0, 0, THUMBNAIL_SIZE[0], THUMBNAIL_SIZE[1]), fill=(0, 0, 0, 28))
    overlay_draw.rectangle(
        (0, 1120, THUMBNAIL_SIZE[0], THUMBNAIL_SIZE[1]),
        fill=(0, 0, 0, 168),
    )
    canvas = Image.alpha_composite(canvas, overlay)

    logo_backing = Image.new("RGBA", (196, 196), (255, 255, 255, 240))
    backing_mask = Image.new("L", (196, 196), 0)
    ImageDraw.Draw(backing_mask).ellipse((0, 0, 195, 195), fill=255)
    logo_backing.putalpha(backing_mask)
    canvas.alpha_composite(logo_backing, (64, 64))
    if logo is not None:
        logo = ImageOps.contain(logo, (176, 176), method=Image.Resampling.LANCZOS)
        logo_mask = Image.new("L", logo.size, 0)
        ImageDraw.Draw(logo_mask).ellipse((0, 0, logo.width - 1, logo.height - 1), fill=255)
        logo.putalpha(ImageChops.multiply(logo.getchannel("A"), logo_mask))
        canvas.alpha_composite(logo, (74 + (176 - logo.width) // 2, 74 + (176 - logo.height) // 2))

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(font_path, 86) if font_path else ImageFont.load_default(size=86)
        brand_font = (
            ImageFont.truetype(font_path, 96)
            if font_path
            else ImageFont.load_default(size=96)
        )
    except OSError as exc:
        raise ValueError("Configured thumbnail font could not be loaded.") from exc
    if logo is None:
        draw.text((114, 106), "A", font=brand_font, fill=(25, 30, 45, 255), anchor="mm")
    lines = _wrap_title(title.strip(), draw=draw, font=font)
    draw.multiline_text(
        (72, 1280),
        "\n".join(lines),
        font=font,
        fill=(255, 255, 255, 255),
        spacing=28,
        stroke_width=3,
        stroke_fill=(0, 0, 0, 210),
    )

    for quality in (90, 84, 78, 72, 66, 60, 54, 48):
        output = BytesIO()
        canvas.convert("RGB").save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        content = output.getvalue()
        if len(content) <= MAX_OUTPUT_BYTES:
            return content
    raise ValueError("Generated thumbnail exceeds YouTube's 50 MB thumbnail limit.")
