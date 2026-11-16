from io import BytesIO

import pytest
from PIL import Image

from app.media.thumbnail import THUMBNAIL_SIZE, generate_thumbnail


def make_image_bytes(size, color, image_format="PNG"):
    image = Image.new("RGBA", size, color)
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def test_thumbnail_generator_composes_vertical_jpeg_with_title_and_logo():
    background = make_image_bytes((720, 1280), (24, 60, 120, 255))
    logo = make_image_bytes((256, 256), (220, 80, 40, 255))

    result = generate_thumbnail(
        title="The hidden science behind lightning",
        background_image=background,
        channel_logo=logo,
    )

    with Image.open(BytesIO(result)) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.size == THUMBNAIL_SIZE
        assert thumbnail.mode == "RGB"
    assert len(result) <= 8_000_000


def test_thumbnail_generator_rejects_invalid_title_and_image_inputs():
    with pytest.raises(ValueError, match="title"):
        generate_thumbnail(
            title=" ",
            background_image=b"not-an-image",
            channel_logo=b"not-an-image",
        )
    with pytest.raises(ValueError, match="Background image"):
        generate_thumbnail(
            title="Valid title",
            background_image=b"not-an-image",
            channel_logo=make_image_bytes((32, 32), (255, 0, 0, 255)),
        )


def test_thumbnail_generator_uses_brand_mark_when_channel_logo_is_not_configured():
    result = generate_thumbnail(
        title="A short story without a channel logo",
        background_image=make_image_bytes((720, 1280), (24, 60, 120, 255)),
    )
    with Image.open(BytesIO(result)) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.size == THUMBNAIL_SIZE
