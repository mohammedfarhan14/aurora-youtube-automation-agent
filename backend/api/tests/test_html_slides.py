import json
import sys
from pathlib import Path

import pytest

from app.media.html_slides import ChromiumHTMLSlideVisualProvider
from app.media.providers import VisualAsset


class _SourceProvider:
    async def generate(self, **_: object) -> VisualAsset:
        return VisualAsset(
            content=b"source-image",
            mime_type="image/png",
            width=1,
            height=1,
            provider_id="test-source",
            rights_basis="generated_in_application",
            contains_synthetic_media=True,
        )


@pytest.mark.anyio
async def test_chromium_composer_uses_restricted_container_compatible_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "args.json"
    fake_chromium = tmp_path / "fake-chromium"
    fake_chromium.write_text(
        "#!"
        + sys.executable
        + "\nimport json, os, sys\n"
        + "from PIL import Image\n"
        + "args = sys.argv[1:]\n"
        + "open(os.environ['AURORA_TEST_CAPTURE'], 'w').write(json.dumps(args))\n"
        + "out = next(a.split('=', 1)[1] for a in args if a.startswith('--screenshot='))\n"
        + "Image.new('RGB', (720, 1280), (16, 19, 27)).save(out)\n",
        encoding="utf-8",
    )
    fake_chromium.chmod(0o755)
    monkeypatch.setenv("AURORA_TEST_CAPTURE", str(capture))
    provider = ChromiumHTMLSlideVisualProvider(
        _SourceProvider(), chromium_path=str(fake_chromium)
    )

    result = await provider.generate(
        visual_direction="A quiet orbital view of the Moon",
        narration_segment="The station travels in lunar orbit.",
        scene_index=0,
    )

    args = json.loads(capture.read_text(encoding="utf-8"))
    assert "--disable-crash-reporter" in args
    assert "--disable-breakpad" in args
    assert "--renderer-process-limit=1" in args
    assert "--num-raster-threads=1" in args
    assert result.provider_id == "test-source+chromium_html_v1"
    assert (result.width, result.height) == (720, 1280)
