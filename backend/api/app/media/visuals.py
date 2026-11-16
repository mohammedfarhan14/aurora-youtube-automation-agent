"""Deterministic, locally generated abstract scene visuals."""

import hashlib
import io
import logging
import struct
import zlib

from PIL import Image, ImageDraw

from app.media.providers import VisualAsset
from app.media.wikimedia import WikimediaVisualProviderError

logger = logging.getLogger(__name__)


class WikimediaFallbackVisualProvider:
    """Keep a production run renderable when Commons has no eligible image."""

    def __init__(self, primary, fallback) -> None:
        self._primary = primary
        self._fallback = fallback

    async def generate(
        self,
        *,
        visual_direction: str,
        narration_segment: str,
        scene_index: int,
        visual_style: str = "",
        lighting: str = "cinematic",
    ) -> VisualAsset:
        try:
            return await self._primary.generate(
                visual_direction=visual_direction,
                narration_segment=narration_segment,
                scene_index=scene_index,
                visual_style=visual_style,
                lighting=lighting,
            )
        except WikimediaVisualProviderError:
            logger.warning(
                "Wikimedia had no eligible visual for scene %s; using original local art.",
                scene_index,
            )
            return await self._fallback.generate(
                visual_direction=visual_direction,
                narration_segment=narration_segment,
                scene_index=scene_index,
                visual_style=visual_style,
                lighting=lighting,
            )

    async def aclose(self) -> None:
        close = getattr(self._primary, "aclose", None)
        if close is not None:
            await close()


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


class LocalAbstractVisualProvider:
    """Generate original, text-free portrait art for the HTML scene composer."""

    def __init__(self, *, width: int = 540, height: int = 960) -> None:
        if width < 180 or height < 320 or width > 1080 or height > 1920:
            raise ValueError("Visual dimensions are outside the supported portrait range.")
        self.width = width
        self.height = height

    async def generate(
        self,
        *,
        visual_direction: str,
        narration_segment: str,
        scene_index: int,
        visual_style: str = "",
        lighting: str = "cinematic",
    ) -> VisualAsset:
        if not visual_direction.strip() or len(visual_direction) > 1000:
            raise ValueError("Visual direction must contain 1 to 1000 characters.")
        if not narration_segment.strip() or len(narration_segment) > 500:
            raise ValueError("Narration segment must contain 1 to 500 characters.")
        if scene_index < 0:
            raise ValueError("Scene index cannot be negative.")

        seed = hashlib.sha256(
            f"{scene_index}:{visual_direction}:{narration_segment}".encode()
        ).digest()
        colors = (
            (24 + seed[0] % 48, 20 + seed[1] % 48, 55 + seed[2] % 70),
            (30 + seed[3] % 70, 55 + seed[4] % 90, 90 + seed[5] % 100),
            (120 + seed[6] % 100, 75 + seed[7] % 80, 100 + seed[8] % 90),
        )
        center_x = self.width * (0.35 + (seed[9] / 255) * 0.3)
        center_y = self.height * (0.25 + (seed[10] / 255) * 0.5)
        radius = min(self.width, self.height) * (0.2 + (seed[11] / 255) * 0.22)

        rows = []
        for y in range(self.height):
            vertical = y / max(1, self.height - 1)
            row = bytearray()
            row.append(0)
            for x in range(self.width):
                horizontal = x / max(1, self.width - 1)
                blend = min(1.0, max(0.0, 0.65 * vertical + 0.35 * horizontal))
                base = tuple(
                    int(colors[0][channel] * (1 - blend) + colors[1][channel] * blend)
                    for channel in range(3)
                )
                dx = x - center_x
                dy = y - center_y
                distance = (dx * dx + dy * dy) ** 0.5
                glow = max(0.0, 1.0 - distance / radius)
                pixel = tuple(
                    min(
                        255,
                        int(
                            base[channel] * (1 - glow * 0.38)
                            + colors[2][channel] * glow * 0.38
                        ),
                    )
                    for channel in range(3)
                )
                row.extend(pixel)
            rows.append(bytes(row))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=6))
            + _png_chunk(b"IEND", b"")
        )
        # Produce a clean image plate only. HTML composition owns all titles and
        # overlays so fallback visuals never duplicate the scene typography.
        image = Image.open(io.BytesIO(png)).convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        description = f"{visual_direction} {narration_segment}".casefold()
        if any(term in description for term in ("moon", "lunar", "horizon")):
            moon_size = round(min(self.width, self.height) * 0.24)
            moon_x = round(self.width * (0.58 + (seed[12] / 255) * 0.16))
            moon_y = round(self.height * (0.36 + (seed[13] / 255) * 0.08))
            moon_bounds = (
                moon_x - moon_size // 2,
                moon_y - moon_size // 2,
                moon_x + moon_size // 2,
                moon_y + moon_size // 2,
            )
            draw.ellipse(moon_bounds, fill=(245, 237, 213, 248))
            crater_color = (174, 177, 190, 115)
            for crater_index in range(6):
                crater_x = moon_x - moon_size // 3 + (seed[14 + crater_index] % (moon_size // 2))
                crater_y = moon_y - moon_size // 3 + (seed[20 + crater_index] % (moon_size // 2))
                crater_radius = max(2, moon_size // (24 + seed[26 + crater_index] % 10))
                draw.ellipse(
                    (
                        crater_x,
                        crater_y,
                        crater_x + crater_radius * 2,
                        crater_y + crater_radius * 2,
                    ),
                    fill=crater_color,
                )
            horizon_y = round(self.height * (0.72 + (scene_index % 2) * 0.035))
            draw.polygon(
                [
                    (0, horizon_y + round(self.height * 0.05)),
                    (round(self.width * 0.22), horizon_y - round(self.height * 0.11)),
                    (round(self.width * 0.48), horizon_y + round(self.height * 0.03)),
                    (round(self.width * 0.77), horizon_y - round(self.height * 0.14)),
                    (self.width, horizon_y + round(self.height * 0.01)),
                    (self.width, self.height),
                    (0, self.height),
                ],
                fill=(12, 19, 36, 238),
            )
        else:
            for ring in range(3):
                inset = round(min(self.width, self.height) * (0.12 + ring * 0.075))
                draw.ellipse(
                    (
                        self.width // 2 - inset,
                        self.height // 2 - inset,
                        self.width // 2 + inset,
                        self.height // 2 + inset,
                    ),
                    outline=(225, 224, 255, 42 - ring * 8),
                    width=max(1, self.width // 270),
                )
        image = Image.alpha_composite(image, overlay).convert("RGB")
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=True)
        return VisualAsset(
            content=encoded.getvalue(),
            mime_type="image/png",
            width=self.width,
            height=self.height,
            provider_id="local_abstract_visual_v1",
            rights_basis="generated_in_application",
            attribution=None,
            source_uri=None,
            contains_synthetic_media=False,
        )
