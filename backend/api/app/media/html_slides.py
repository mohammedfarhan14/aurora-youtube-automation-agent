"""Offline Chromium composition for visual-only scene plates.

Only application-owned HTML is rendered. Narration stays out of the slide so
FFmpeg's synchronized caption track is the only transcript shown on screen.
Visual bytes are embedded as a data URI, and Chromium has no network route.
The output remains a rights-provenanced still for Aurora's FFmpeg renderer;
this is not an AI motion-video provider.
"""

import asyncio
import base64
import os
import tempfile
from pathlib import Path

from PIL import Image

from app.media.providers import VisualAsset, VisualProvider


class HTMLSlideRenderError(RuntimeError):
    """An offline Chromium scene composition failed."""


def _scene_html(
    *,
    visual: VisualAsset,
    scene_index: int,
    width: int,
    height: int,
    lighting: str,
) -> str:
    mime = "image/jpeg" if visual.mime_type == "image/jpeg" else "image/png"
    encoded_visual = base64.b64encode(visual.content).decode("ascii")
    photo_uri = f"data:{mime};base64,{encoded_visual}"
    photo_filter = {
        "natural": "saturate(1.02) contrast(1.01)",
        "cinematic": "saturate(1.12) contrast(1.04)",
        "studio": "saturate(1.05) contrast(1.08) brightness(1.04)",
        "dramatic": "saturate(1.18) contrast(1.16) brightness(.91)",
    }.get(lighting, "saturate(1.12) contrast(1.04)")
    css = f"""
* {{ box-sizing: border-box; }}
html, body {{
  width: {width}px; height: {height}px; margin: 0; overflow: hidden;
  background: #10131b;
}}
body {{ font-family: Inter, 'DejaVu Sans', Arial, sans-serif; color: #fff; }}
.frame {{
  position: relative; width: 100%; height: 100%; overflow: hidden;
  background: #10131b;
}}
.photo {{
  position: absolute; inset: -2%; width: 104%; height: 104%; object-fit: cover;
  filter: {photo_filter}; transform: scale(1.015);
}}
.shade {{
  position: absolute; inset: 0;
  background:
    linear-gradient(180deg, rgba(7,10,19,.08) 0%, rgba(7,10,19,.03) 38%,
      rgba(7,10,19,.18) 57%, rgba(7,10,19,.76) 100%),
    linear-gradient(90deg, rgba(7,10,19,.22), transparent 84%);
}}
.top {{
  position: absolute; top: 6.2%; left: 7.5%; right: 7.5%; display: flex;
  justify-content: flex-end; font-size: 15px; font-weight: 700;
  letter-spacing: .18em; text-transform: uppercase; color: rgba(255,255,255,.78);
}}
.count {{
  padding: 9px 13px; border: 1px solid rgba(255,255,255,.25);
  border-radius: 20px; background: rgba(10,14,24,.24);
}}
"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width={width}, initial-scale=1, maximum-scale=1">
<style>{css}</style></head>
<body><main class="frame">
<img class="photo" src="{photo_uri}" alt="">
<div class="shade"></div>
</main></body></html>"""


class ChromiumHTMLSlideVisualProvider:
    """Apply local HTML art direction to an existing visual source."""

    def __init__(
        self,
        source: VisualProvider,
        *,
        chromium_path: str,
        width: int = 720,
        height: int = 1280,
        timeout_seconds: float = 30,
        max_output_bytes: int = 12_000_000,
    ) -> None:
        if not chromium_path.strip():
            raise ValueError("Chromium executable path is required.")
        if width < 360 or height < 640 or width > 1080 or height > 1920:
            raise ValueError("HTML slide dimensions are outside supported portrait limits.")
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("HTML slide timeout and output limit must be positive.")
        self._source = source
        self._chromium_path = chromium_path
        self._width = width
        self._height = height
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    async def generate(
        self,
        *,
        visual_direction: str,
        narration_segment: str,
        scene_index: int,
        visual_style: str = "",
        lighting: str = "cinematic",
    ) -> VisualAsset:
        source = await self._source.generate(
            visual_direction=visual_direction,
            narration_segment=narration_segment,
            scene_index=scene_index,
            visual_style=visual_style,
            lighting=lighting,
        )
        with tempfile.TemporaryDirectory(prefix="aurora-html-slide-") as temporary:
            directory = Path(temporary)
            page = directory / "scene.html"
            screenshot = directory / "scene.png"
            profile = directory / "chromium-profile"
            config_dir = directory / "chromium-config"
            cache_dir = directory / "chromium-cache"
            home_dir = directory / "home"
            for writable_dir in (profile, config_dir, cache_dir, home_dir):
                writable_dir.mkdir()
            page.write_text(
                _scene_html(
                    visual=source,
                    scene_index=scene_index,
                    width=self._width,
                    height=self._height,
                    lighting=lighting,
                ),
                encoding="utf-8",
            )
            command = [
                self._chromium_path,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-extensions",
                "--disable-dns-prefetch",
                "--disable-crash-reporter",
                "--disable-breakpad",
                "--renderer-process-limit=1",
                "--num-raster-threads=1",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--hide-scrollbars",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=250",
                f"--window-size={self._width},{self._height}",
                f"--screenshot={screenshot}",
                page.as_uri(),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={
                        **os.environ,
                        "HOME": str(home_dir),
                        "XDG_CONFIG_HOME": str(config_dir),
                        "XDG_CACHE_HOME": str(cache_dir),
                    },
                )
                await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise HTMLSlideRenderError(
                    "Chromium exceeded its scene-render time limit."
                ) from exc
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            except OSError as exc:
                raise HTMLSlideRenderError(
                    "Configured Chromium executable could not start."
                ) from exc
            if process.returncode != 0 or not screenshot.is_file():
                raise HTMLSlideRenderError("Chromium did not produce a scene image.")
            if (
                screenshot.stat().st_size <= 0
                or screenshot.stat().st_size > self._max_output_bytes
            ):
                raise HTMLSlideRenderError(
                    "Chromium scene image is empty or exceeds its size limit."
                )
            content = screenshot.read_bytes()
            try:
                with Image.open(screenshot) as rendered:
                    if rendered.size != (self._width, self._height):
                        raise HTMLSlideRenderError("Chromium returned unexpected scene dimensions.")
                    rendered.verify()
            except (OSError, ValueError) as exc:
                raise HTMLSlideRenderError(
                    "Chromium returned an invalid PNG scene image."
                ) from exc

        return VisualAsset(
            content=content,
            mime_type="image/png",
            width=self._width,
            height=self._height,
            provider_id=f"{source.provider_id}+chromium_html_v1",
            rights_basis=source.rights_basis,
            attribution=source.attribution,
            source_uri=source.source_uri,
            license_id=source.license_id,
            license_uri=source.license_uri,
            contains_synthetic_media=source.contains_synthetic_media,
        )

    async def aclose(self) -> None:
        close = getattr(self._source, "aclose", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
