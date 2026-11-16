"""ComfyUI workflow adapter for generating rights-provenanced scene stills."""

import asyncio
import copy
import hashlib
import io
import json
import re
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError

from app.media.providers import VisualAsset


class ComfyUIProviderError(RuntimeError):
    """A ComfyUI workflow failed or returned an invalid scene image."""


def _substitute(value: object, *, prompt: str, negative_prompt: str, seed: int) -> object:
    if isinstance(value, dict):
        return {
            key: _substitute(item, prompt=prompt, negative_prompt=negative_prompt, seed=seed)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _substitute(item, prompt=prompt, negative_prompt=negative_prompt, seed=seed)
            for item in value
        ]
    if isinstance(value, str):
        if value == "{{SEED}}":
            return seed
        replacements = {"PROMPT": prompt, "NEGATIVE_PROMPT": negative_prompt, "SEED": str(seed)}
        return re.sub(
            r"\{\{(PROMPT|NEGATIVE_PROMPT|SEED)\}\}",
            lambda match: replacements[match.group(1)],
            value,
        )
    return value


def _visual_prompt(
    *,
    visual_direction: str,
    narration_segment: str = "",
    visual_style: str,
    lighting: str,
    default_style: str,
    scene_index: int = 0,
) -> str:
    """Build a short, subject-first prompt for the configured SD 1.5 workflow.

    Long prose pushes the named subject beyond SD 1.5's short CLIP context and
    encourages it to satisfy generic style words with unrelated imagery. Scene
    direction therefore comes first; narration is retained only as a short cue.
    """
    shot_types = (
        "wide shot, subject dominates the frame",
        "medium action shot, subject visibly performing the action",
        "close detail of the meaningful object in this moment",
        "reverse angle on the same subject and setting",
        "low-angle shot, strong focal subject",
        "intimate close-up, uncluttered composition",
    )
    # Scene-specific subject words must be first: the local SD 1.5 checkpoint
    # truncates long conditioning and previously returned scenery despite GPU
    # directions. Keep optional style hints short and after the subject.
    sections = [
        f"Literal scene: {visual_direction.strip()[:420].rstrip(' .')}.",
        "exact subject prominent; no unrelated scenery",
        "photorealistic documentary photograph, crisp realistic detail",
        shot_types[scene_index % len(shot_types)],
        "vertical portrait composition, vivid natural color",
        f"{lighting} lighting",
    ]
    style = " ".join(value.strip() for value in (default_style, visual_style) if value.strip())
    if style:
        sections.append(style[:120])
    if narration_segment.strip():
        # Keep only a short tail after the subject so narration cannot dominate.
        sections.append(f"Story context: {narration_segment.strip()[:100]}")
    sections.append("no captions, labels, watermark, or border")
    return "\n".join(sections)


class ComfyUIVisualProvider:
    """Run an operator-provided ComfyUI API workflow and select one image output.

    Workflow node/model choices remain in the ComfyUI JSON file. Replace the
    desired text and seed inputs with {{PROMPT}}, {{NEGATIVE_PROMPT}}, and
    {{SEED}} placeholders. This adapter currently accepts still images only.
    """

    def __init__(
        self,
        *,
        base_url: str,
        workflow_path: Path,
        output_node_id: str,
        timeout_seconds: float = 300,
        max_image_bytes: int = 20_000_000,
        negative_prompt: str = "blurry, low quality, illegible text, watermark, distorted anatomy",
        style_prompt: str = "",
        api_key: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        if not output_node_id.strip() or len(output_node_id) > 40:
            raise ValueError("ComfyUI output node ID is invalid.")
        if timeout_seconds <= 0 or max_image_bytes <= 0:
            raise ValueError("ComfyUI limits must be positive.")
        try:
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("ComfyUI workflow file could not be read as JSON.") from exc
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("ComfyUI workflow must be a non-empty API-format object.")
        if output_node_id not in workflow:
            raise ValueError("ComfyUI output node ID is missing from the workflow.")

        headers = {"X-API-Key": api_key.strip()} if api_key and api_key.strip() else {}
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            headers=headers,
            timeout=httpx.Timeout(30),
            trust_env=False,
            follow_redirects=False,
            proxy=proxy_url,
        )
        self._workflow = workflow
        self._output_node_id = output_node_id
        self._timeout_seconds = timeout_seconds
        self._max_image_bytes = max_image_bytes
        self._negative_prompt = negative_prompt
        self._style_prompt = style_prompt.strip()

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("system_stats")
        except httpx.HTTPError:
            return False
        return response.is_success

    async def generate(
        self,
        *,
        visual_direction: str,
        narration_segment: str,
        scene_index: int,
        visual_style: str = "",
        lighting: str = "cinematic",
    ) -> VisualAsset:
        if not visual_direction.strip() or len(visual_direction) > 2_000:
            raise ValueError("ComfyUI scene direction must contain 1 to 2000 characters.")
        if scene_index < 0:
            raise ValueError("Scene index cannot be negative.")

        if lighting not in {"natural", "cinematic", "studio", "dramatic"}:
            raise ValueError("Unsupported scene lighting direction.")
        prompt = _visual_prompt(
            visual_direction=visual_direction,
            narration_segment=narration_segment,
            visual_style=visual_style,
            lighting=lighting,
            default_style=self._style_prompt,
            scene_index=scene_index,
        )
        seed_material = f"{scene_index}:{prompt}".encode()
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
        negative_prompt = self._negative_prompt
        subject_context = f"{visual_direction} {narration_segment}".casefold()
        if any(
            term in subject_context
            for term in (
                "gpu",
                "graphics card",
                "graphics processing",
                "processor",
                "cpu",
                "computer",
                "server",
                "chip",
                "semiconductor",
            )
        ):
            negative_prompt = (
                f"{negative_prompt}, landscape, forest, mountain, ocean, clouds, countryside, "
                "road, unrelated human portrait, close-up eye"
            )
        workflow = _substitute(
            copy.deepcopy(self._workflow),
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
        )
        if not isinstance(workflow, dict):
            raise ComfyUIProviderError("ComfyUI workflow payload is invalid.")

        try:
            queued = await self._client.post(
                "prompt", json={"prompt": workflow, "client_id": str(uuid4())}
            )
            queued.raise_for_status()
            prompt_id = queued.json().get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id or len(prompt_id) > 128:
                raise ComfyUIProviderError("ComfyUI did not return a valid task ID.")

            deadline = asyncio.get_running_loop().time() + self._timeout_seconds
            history: dict | None = None
            while asyncio.get_running_loop().time() < deadline:
                response = await self._client.get(f"history/{prompt_id}")
                response.raise_for_status()
                payload = response.json()
                candidate = payload.get(prompt_id) if isinstance(payload, dict) else None
                if isinstance(candidate, dict) and candidate.get("outputs"):
                    history = candidate
                    break
                await asyncio.sleep(
                    min(1.0, max(0.1, deadline - asyncio.get_running_loop().time()))
                )
            if history is None:
                raise ComfyUIProviderError("ComfyUI timed out while rendering this scene.")
            output = history["outputs"].get(self._output_node_id, {})
            images = output.get("images", []) if isinstance(output, dict) else []
            if not images or not isinstance(images[0], dict):
                raise ComfyUIProviderError("The configured ComfyUI output node returned no image.")
            image_meta = images[0]
            async with self._client.stream(
                "GET",
                "view",
                params={
                    "filename": image_meta.get("filename", ""),
                    "subfolder": image_meta.get("subfolder", ""),
                    "type": image_meta.get("type", "output"),
                },
            ) as image_response:
                image_response.raise_for_status()
                image_chunks = bytearray()
                async for chunk in image_response.aiter_bytes():
                    image_chunks.extend(chunk)
                    if len(image_chunks) > self._max_image_bytes:
                        raise ComfyUIProviderError(
                            "ComfyUI image exceeds the configured byte limit."
                        )
                content = bytes(image_chunks)
        except ComfyUIProviderError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ComfyUIProviderError(
                "ComfyUI request failed or returned invalid workflow data."
            ) from exc

        if not content or len(content) > self._max_image_bytes:
            raise ComfyUIProviderError(
                "ComfyUI image is empty or exceeds the configured byte limit."
            )
        try:
            image = Image.open(io.BytesIO(content))
            image.verify()
            image = Image.open(io.BytesIO(content)).convert("RGB")
        except (UnidentifiedImageError, OSError) as exc:
            raise ComfyUIProviderError("ComfyUI output is not a valid image.") from exc
        width, height = image.size
        if width < 64 or height < 64 or width > 4096 or height > 4096:
            raise ComfyUIProviderError(
                "ComfyUI image dimensions are outside the 64–4096 pixel range."
            )
        encoded = io.BytesIO()
        image.save(encoded, format="PNG", optimize=True)
        png = encoded.getvalue()
        if len(png) > self._max_image_bytes:
            raise ComfyUIProviderError(
                "Normalized ComfyUI image exceeds the configured byte limit."
            )
        return VisualAsset(
            content=png,
            mime_type="image/png",
            width=width,
            height=height,
            provider_id="comfyui_workflow",
            rights_basis="generated_in_application",
            contains_synthetic_media=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
