import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from app.media.ffmpeg import FFmpegVideoRenderer, MediaRenderError, _vtt_to_ass
from app.media.providers import AudioAsset, BackgroundMusicAsset, RenderRequest, VisualAsset


def create_executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)


def make_png(width, height):
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


def make_jpeg(width, height):
    return (
        bytes.fromhex("ffd8ffc0")
        + bytes.fromhex("0011")
        + bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([3])
        + bytes.fromhex("011100021100031100")
        + bytes.fromhex("ffd9")
    )


def make_request(*, target_duration=30):
    return RenderRequest(
        visual_assets=(
            VisualAsset(
                content=make_png(180, 320),
                mime_type="image/png",
                width=180,
                height=320,
                provider_id="local-test",
                rights_basis="generated_in_application",
            ),
            VisualAsset(
                content=make_png(180, 320),
                mime_type="image/png",
                width=180,
                height=320,
                provider_id="local-test",
                rights_basis="generated_in_application",
            ),
        ),
        audio_asset=AudioAsset(
            content=b"RIFF-test-wav",
            mime_type="audio/wav",
            duration_ms=30_000,
            sample_rate_hz=16_000,
            channels=1,
            provider_id="mock-tone",
            model_id="mock",
            voice_id="mock",
        ),
        captions_vtt=(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:15.000\nFirst scene\n\n"
            "00:00:15.000 --> 00:00:30.000\nSecond scene\n"
        ),
        scene_durations_seconds=(15, 15),
        target_duration_seconds=target_duration,
        width=360,
        height=640,
        frame_rate=30,
    )


def make_renderer(tmp_path, *, probe_payload=None, max_output_bytes=1000):
    ffmpeg = tmp_path / "fake-ffmpeg"
    ffprobe = tmp_path / "fake-ffprobe"
    create_executable(
        ffmpeg,
        "import pathlib, sys\n"
        "index = sys.argv.index('-fs')\n"
        "assert int(sys.argv[index + 1]) == 1000\n"
        "pathlib.Path(sys.argv[-1]).write_bytes(b'fake-mp4-content')\n",
    )
    payload = probe_payload or {
        "format": {"duration": "30.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 360, "height": 640},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ],
    }
    create_executable(
        ffprobe,
        f"import json\nprint({json.dumps(json.dumps(payload))})\n",
    )
    return FFmpegVideoRenderer(
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe),
        video_encoder="libx264",
        max_output_bytes=max_output_bytes,
    )


def test_vtt_to_ass_positions_readable_captions_above_shorts_controls():
    result = _vtt_to_ass(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.345\nA &amp; B {story}\n",
        width=1080,
        height=1920,
    )

    assert "PlayResX: 1080" in result
    assert "PlayResY: 1920" in result
    assert "WrapStyle: 0" in result
    assert "Style: Default,DejaVu Sans,52," in result
    assert ",0,1,4,1.2,2,70,70,288,1" in result
    assert "Default,,0,0,0,,A & B \\{story\\}" in result
    assert "Dialogue: 0,0:00:00.00,0:00:02.34" in result

    small_frame = _vtt_to_ass(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA short line\n",
        width=360,
        height=640,
    )
    assert ",0,1,2,1.2,2,24,24,96,1" in small_frame


def test_vtt_to_ass_supports_classic_centered_large_caption_style():
    result = _vtt_to_ass(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nA caption\n",
        width=1080,
        height=1920,
        caption_style="classic",
        caption_position="center",
        caption_size="large",
    )

    assert "Style: Default,DejaVu Sans,63," in result
    assert ",0,3,1.5,0,5,70,70,0,1" in result


def test_vtt_to_ass_rejects_unknown_caption_preferences():
    with pytest.raises(ValueError, match="Caption style or size"):
        _vtt_to_ass(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCaption\n",
            width=1080,
            height=1920,
            caption_style="unknown",
        )


def test_vtt_to_ass_rejects_malformed_cue_timestamps():
    with pytest.raises(ValueError, match="invalid minute or second"):
        _vtt_to_ass(
            "WEBVTT\n\n00:00:99.000 --> 00:00:02.000\nBad time\n",
            width=360,
            height=640,
        )


@pytest.mark.parametrize("width,height", [(1080, 1920), (1920, 1080), (1080, 1080)])
def test_ffmpeg_renderer_accepts_supported_video_shapes(tmp_path, width, height):
    renderer = make_renderer(tmp_path)
    request = replace(make_request(), width=width, height=height)

    renderer._validate_request(request)


def test_ffmpeg_renderer_rejects_unsupported_video_shape(tmp_path):
    renderer = make_renderer(tmp_path)
    request = replace(make_request(), width=1280, height=960)

    with pytest.raises(ValueError, match="supported 9:16, 16:9, or 1:1"):
        renderer._validate_request(request)


@pytest.mark.anyio
async def test_ffmpeg_renderer_builds_video_and_checks_streams(tmp_path):
    renderer = make_renderer(tmp_path)

    result = await renderer.render(make_request())

    assert result.content == b"fake-mp4-content"
    assert result.mime_type == "video/mp4"
    assert result.duration_ms == 30_000
    assert result.width == 360
    assert result.height == 640
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert result.provider_id == "ffmpeg"


@pytest.mark.anyio
async def test_ffmpeg_renderer_accepts_jpeg_scene_assets(tmp_path):
    renderer = make_renderer(tmp_path)
    request = make_request()
    jpeg_assets = tuple(
        VisualAsset(
            content=make_jpeg(180, 320),
            mime_type="image/jpeg",
            width=180,
            height=320,
            provider_id="commons-test",
            rights_basis="cc_by",
            attribution="Creator — File — Source — CC BY 4.0",
            source_uri="https://commons.wikimedia.org/wiki/File:Example.jpg",
            license_id="CC BY 4.0",
            license_uri="https://creativecommons.org/licenses/by/4.0/",
        )
        for _ in request.visual_assets
    )
    request = replace(request, visual_assets=jpeg_assets)

    result = await renderer.render(request)

    assert result.mime_type == "video/mp4"
    assert result.width == 360
    assert result.height == 640


@pytest.mark.anyio
async def test_ffmpeg_renderer_rejects_missing_caption_stream(tmp_path):
    payload = {
        "format": {"duration": "30.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 360, "height": 640},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    renderer = make_renderer(tmp_path, probe_payload=payload)

    with pytest.raises(MediaRenderError, match="quality checks"):
        await renderer.render(make_request())


@pytest.mark.parametrize("audio_duration_ms", [23_999, 36_001])
@pytest.mark.anyio
async def test_ffmpeg_renderer_rejects_audio_outside_supported_tempo_range(
    tmp_path,
    audio_duration_ms,
):
    renderer = make_renderer(tmp_path)
    request = make_request()
    request = replace(
        request,
        audio_asset=AudioAsset(
            content=request.audio_asset.content,
            mime_type=request.audio_asset.mime_type,
            duration_ms=audio_duration_ms,
            sample_rate_hz=request.audio_asset.sample_rate_hz,
            channels=request.audio_asset.channels,
            provider_id=request.audio_asset.provider_id,
            model_id=request.audio_asset.model_id,
            voice_id=request.audio_asset.voice_id,
        ),
    )

    with pytest.raises(ValueError, match="between 80% and 120%"):
        await renderer.render(request)


def test_ffmpeg_renderer_accepts_eighty_percent_narration_length(tmp_path):
    renderer = make_renderer(tmp_path)
    request = replace(
        make_request(),
        audio_asset=replace(make_request().audio_asset, duration_ms=24_000),
    )

    renderer._validate_request(request)
    arguments = renderer._ffmpeg_arguments(request, tmp_path)
    filter_graph = arguments[arguments.index("-filter_complex") + 1]

    assert "atempo=0.8" in filter_graph


def test_ffmpeg_renderer_accepts_qwen_audio_slightly_longer_than_target(tmp_path):
    renderer = make_renderer(tmp_path)
    request = replace(
        make_request(),
        audio_asset=replace(make_request().audio_asset, duration_ms=33_840),
    )

    renderer._validate_request(request)
    arguments = renderer._ffmpeg_arguments(request, tmp_path)
    filter_graph = arguments[arguments.index("-filter_complex") + 1]

    assert "atempo=1.128000" in filter_graph


def test_ffmpeg_renderer_accepts_slightly_longer_synthesized_audio(tmp_path):
    renderer = make_renderer(tmp_path)
    request = replace(
        make_request(),
        audio_asset=replace(make_request().audio_asset, duration_ms=32_560),
    )

    renderer._validate_request(request)
    arguments = renderer._ffmpeg_arguments(request, tmp_path)
    filter_graph = arguments[arguments.index("-filter_complex") + 1]

    assert "atempo=1.085333" in filter_graph


def test_ffmpeg_renderer_stretches_short_narration_to_target_duration(tmp_path):
    renderer = make_renderer(tmp_path)
    request = replace(
        make_request(),
        audio_asset=replace(make_request().audio_asset, duration_ms=27_920),
    )

    arguments = renderer._ffmpeg_arguments(request, tmp_path)
    filter_graph = arguments[arguments.index("-filter_complex") + 1]

    assert "atempo=0.930667" in filter_graph
    assert "apad,atrim=duration=30" in filter_graph
    assert "ass='" in filter_graph
    assert "subtitles='" not in filter_graph
    assert arguments[arguments.index("-map") + 1] == "[vburn]"


@pytest.mark.anyio
async def test_ffmpeg_renderer_rejects_duration_mismatch_before_execution(tmp_path):
    renderer = make_renderer(tmp_path)

    with pytest.raises(ValueError, match="sum to the target"):
        await renderer.render(make_request(target_duration=25))


@pytest.mark.anyio
async def test_ffmpeg_renderer_enforces_output_size_limit(tmp_path):
    ffmpeg = tmp_path / "large-ffmpeg"
    ffprobe = tmp_path / "fake-ffprobe"
    create_executable(
        ffmpeg,
        "import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(b'x' * 2048)\n",
    )
    payload = {
        "format": {"duration": "30.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 360, "height": 640},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ],
    }
    create_executable(
        ffprobe,
        f"import json\nprint({json.dumps(json.dumps(payload))})\n",
    )
    renderer = FFmpegVideoRenderer(
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe),
        video_encoder="libx264",
        max_output_bytes=1024,
    )

    with pytest.raises(MediaRenderError, match="output-size"):
        await renderer.render(make_request())


@pytest.mark.anyio
async def test_ffmpeg_renderer_inspect_reports_missing_audio(tmp_path):
    payload = {
        "format": {"duration": "30.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 360, "height": 640},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ],
    }
    renderer = make_renderer(tmp_path, probe_payload=payload)

    report = await renderer.inspect(b"fake-mp4")

    assert report.is_valid is False
    assert report.has_audio is False
    assert "missing_audio_stream" in report.issues


def make_diagnostic_renderer(tmp_path, diagnostics):
    ffmpeg = tmp_path / "diagnostic-ffmpeg"
    ffprobe = tmp_path / "diagnostic-ffprobe"
    create_executable(
        ffmpeg,
        "import sys\n" + f"sys.stderr.write({json.dumps(diagnostics)})\n",
    )
    payload = {
        "format": {"duration": "30.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 360, "height": 640},
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ],
    }
    create_executable(
        ffprobe,
        f"import json\nprint({json.dumps(json.dumps(payload))})\n",
    )
    return FFmpegVideoRenderer(
        ffmpeg_path=str(ffmpeg),
        ffprobe_path=str(ffprobe),
        video_encoder="libx264",
    )


@pytest.mark.anyio
async def test_ffmpeg_renderer_inspect_reports_normalized_audio_and_no_black_frames(tmp_path):
    diagnostics = "Integrated loudness:\n  I: -14.0 LUFS\nTrue peak:\n  Peak: -1.0 dBFS\n"
    renderer = make_diagnostic_renderer(tmp_path, diagnostics)

    report = await renderer.inspect(b"fake-mp4")

    assert report.is_valid is True
    assert report.integrated_loudness_lufs == -14.0
    assert report.true_peak_dbfs == -1.0
    assert report.black_frame_segments == ()


@pytest.mark.anyio
async def test_ffmpeg_renderer_rejects_hot_audio_and_detected_black_frames(tmp_path):
    diagnostics = (
        "Integrated loudness:\n"
        "  I: -8.0 LUFS\n"
        "True peak:\n"
        "  Peak: -0.1 dBFS\n"
        "black_start:1.000 black_end:2.000 black_duration:1.000\n"
    )
    renderer = make_diagnostic_renderer(tmp_path, diagnostics)

    report = await renderer.inspect(b"fake-mp4")

    assert report.is_valid is False
    assert "audio_loudness_out_of_range" in report.issues
    assert "audio_true_peak_exceeded" in report.issues
    assert "black_frames_detected" in report.issues
    assert report.black_frame_segments == ((1.0, 2.0),)


def test_ffmpeg_renderer_normalizes_audio_to_streaming_loudness(tmp_path):
    renderer = make_renderer(tmp_path)

    arguments = renderer._ffmpeg_arguments(make_request(), tmp_path)
    filter_complex = arguments[arguments.index("-filter_complex") + 1]

    assert "loudnorm=I=-14:TP=-1.0:LRA=11" in filter_complex
    assert arguments[arguments.index("-ar") + 1] == "48000"


def test_ffmpeg_renderer_adds_subtle_alternating_motion_to_still_scenes(tmp_path):
    renderer = make_renderer(tmp_path)
    arguments = renderer._ffmpeg_arguments(make_request(), tmp_path)
    filter_complex = arguments[arguments.index("-filter_complex") + 1]

    assert "scale=402:716:force_original_aspect_ratio=increase" in filter_complex
    assert "crop=360:640" in filter_complex
    assert "cos(PI*n/450)" in filter_complex
    assert "x='(iw-ow)*0.5*(1-cos" in filter_complex
    assert "x='(iw-ow)*0.5*(1+cos" in filter_complex
    assert "eval=frame" not in filter_complex
    assert "cos(PI*n/450))',setsar=1" in filter_complex


def test_ffmpeg_renderer_can_keep_stills_static(tmp_path):
    renderer = FFmpegVideoRenderer(
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        video_encoder="libx264",
        motion_style="static",
    )
    arguments = renderer._ffmpeg_arguments(make_request(), tmp_path)
    filter_complex = arguments[arguments.index("-filter_complex") + 1]

    assert "scale=360:640:force_original_aspect_ratio=increase" in filter_complex
    assert "cos(PI*n/" not in filter_complex


def make_music_asset(*, rights_status="cleared", content=b"RIFF-test-music"):
    metadata = {
        "schema_version": "1.0.0",
        "asset_id": "test-track-001",
        "title": "Test background track",
        "creator": "Test composer",
        "source_url": "https://music.example.test/tracks/test-track-001",
        "license_id": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_text": "Test composer — Test background track — CC BY 4.0",
        "rights_status": rights_status,
        "usage_scope": ["youtube", "commercial"],
        "commercial_use": True,
        "derivative_works": True,
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    return BackgroundMusicAsset(
        content=content,
        mime_type="audio/wav",
        duration_ms=30_000,
        sample_rate_hz=48_000,
        channels=2,
        metadata=metadata,
    )


def test_ffmpeg_renderer_ducks_rights_cleared_background_music_beneath_narration(tmp_path):
    renderer = make_renderer(tmp_path)
    request = replace(make_request(), background_music_asset=make_music_asset())

    arguments = renderer._ffmpeg_arguments(request, tmp_path)
    filter_complex = arguments[arguments.index("-filter_complex") + 1]

    assert arguments[arguments.index("-stream_loop") + 1] == "-1"
    assert str(tmp_path / "background-music.wav") in arguments
    assert "[3:a]" in filter_complex
    assert "sidechaincompress=threshold=0.025:ratio=8:attack=20:release=500" in filter_complex
    assert "amix=inputs=2:duration=first" in filter_complex
    map_indices = [index for index, value in enumerate(arguments) if value == "-map"]
    assert arguments[map_indices[-1] + 1] == "4:s:0?"


@pytest.mark.parametrize("rights_status", ["review_required", "rejected"])
def test_ffmpeg_renderer_rejects_background_music_without_cleared_rights(tmp_path, rights_status):
    renderer = make_renderer(tmp_path)
    request = replace(
        make_request(),
        background_music_asset=make_music_asset(rights_status=rights_status),
    )

    with pytest.raises(ValueError, match="cleared licensing metadata"):
        renderer._validate_request(request)


def test_ffmpeg_renderer_rejects_background_music_content_hash_mismatch(tmp_path):
    renderer = make_renderer(tmp_path)
    music = make_music_asset()
    metadata = dict(music.metadata)
    metadata["content_sha256"] = "0" * 64
    request = replace(
        make_request(),
        background_music_asset=replace(music, metadata=metadata),
    )

    with pytest.raises(ValueError, match="cleared licensing metadata"):
        renderer._validate_request(request)


def test_ffmpeg_renderer_rejects_background_music_without_commercial_youtube_scope(tmp_path):
    renderer = make_renderer(tmp_path)
    music = make_music_asset()
    metadata = dict(music.metadata)
    metadata["usage_scope"] = ["personal"]
    request = replace(
        make_request(),
        background_music_asset=replace(music, metadata=metadata),
    )

    with pytest.raises(ValueError, match="cleared licensing metadata"):
        renderer._validate_request(request)
