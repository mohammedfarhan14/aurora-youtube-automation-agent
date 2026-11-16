"""Real FFmpeg integration coverage for a complete portrait MP4 render."""

import io
import math
import shutil
import subprocess
import sys
import wave
from array import array

import pytest

from app.media.ffmpeg import FFmpegVideoRenderer
from app.media.providers import AudioAsset, RenderRequest
from app.media.visuals import LocalAbstractVisualProvider


def make_test_audio(*, duration_seconds: int = 30, sample_rate_hz: int = 16_000) -> bytes:
    samples = array(
        "h",
        (
            int(4_000 * math.sin(2 * math.pi * 440 * index / sample_rate_hz))
            for index in range(duration_seconds * sample_rate_hz)
        ),
    )
    if sys.byteorder != "little":
        samples.byteswap()

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate_hz)
        audio.writeframes(samples.tobytes())
    return buffer.getvalue()


@pytest.mark.anyio
async def test_real_ffmpeg_renders_portrait_video_with_audio_and_subtitles():
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        pytest.skip("FFmpeg and ffprobe are required for the real-render integration test.")

    encoders = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-encoders"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    if "libx264" not in encoders:
        pytest.skip("The installed FFmpeg build does not include the libx264 encoder.")

    visuals = LocalAbstractVisualProvider(width=360, height=640)
    first_visual = await visuals.generate(
        visual_direction="A bright abstract opening scene",
        narration_segment="A short opening sentence.",
        scene_index=0,
    )
    second_visual = await visuals.generate(
        visual_direction="A contrasting abstract conclusion scene",
        narration_segment="A concise concluding sentence.",
        scene_index=1,
    )
    narration = AudioAsset(
        content=make_test_audio(),
        mime_type="audio/wav",
        duration_ms=30_000,
        sample_rate_hz=16_000,
        channels=1,
        provider_id="integration-test",
        model_id="sine-wave-fixture-v1",
        voice_id="test-tone",
    )
    request = RenderRequest(
        visual_assets=(first_visual, second_visual),
        audio_asset=narration,
        captions_vtt=(
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:15.000\nA short opening sentence.\n\n"
            "00:00:15.000 --> 00:00:30.000\nA concise concluding sentence.\n"
        ),
        scene_durations_seconds=(15, 15),
        target_duration_seconds=30,
        width=360,
        height=640,
        frame_rate=24,
    )
    renderer = FFmpegVideoRenderer(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        video_encoder="libx264",
        video_preset="ultrafast",
        video_crf=30,
        timeout_seconds=120,
        max_output_bytes=20_000_000,
    )

    video = await renderer.render(request)
    report = await renderer.inspect(video.content)

    assert video.content[4:8] == b"ftyp"
    assert video.mime_type == "video/mp4"
    assert video.width == 360
    assert video.height == 640
    assert video.video_codec == "h264"
    assert video.audio_codec == "aac"
    assert abs(video.duration_ms - 30_000) <= 750
    assert report.is_valid is True
    assert report.has_audio is True
    assert report.has_captions is True
    assert report.video_codec == "h264"
    assert report.audio_codec == "aac"
    assert report.integrated_loudness_lufs is not None
    assert -17.0 <= report.integrated_loudness_lufs <= -11.0
    assert report.true_peak_dbfs is not None
    assert report.true_peak_dbfs <= -0.5
    assert report.black_frame_segments == ()
