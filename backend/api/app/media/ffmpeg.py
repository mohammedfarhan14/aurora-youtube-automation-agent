"""Bounded FFmpeg/ffprobe rendering adapter for portrait short-form videos."""

import asyncio
import hashlib
import html
import json
import math
import re
import struct
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from app.media.providers import MediaQualityReport, RenderRequest, VideoAsset

_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}

_VTT_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})$")
_VTT_TAG = re.compile(r"</?[^>]+>")


def _vtt_timestamp_to_ass(value: str) -> str:
    match = _VTT_TIME.fullmatch(value)
    if match is None:
        raise ValueError("Caption timestamps must use the normalized WebVTT format.")
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Caption timestamp contains an invalid minute or second value.")
    return f"{hours}:{minutes:02d}:{seconds:02d}.{milliseconds // 10:02d}"


def _vtt_to_ass(
    captions_vtt: str,
    *,
    width: int,
    height: int,
    caption_style: str = "bold_outline",
    caption_position: str = "lower_safe",
    caption_size: str = "medium",
) -> str:
    """Convert validated WebVTT cues to explicitly positioned ASS captions."""
    blocks = [
        block.strip() for block in re.split(r"\n[ \t]*\n", captions_vtt.strip()) if block.strip()
    ]
    if not blocks or blocks[0] != "WEBVTT":
        raise ValueError("Captions must begin with a WebVTT header.")

    size_scale = {"small": 0.022, "medium": 0.027, "large": 0.033}.get(caption_size)
    if size_scale is None or caption_style not in {"classic", "bold_outline"}:
        raise ValueError("Caption style or size is unsupported.")
    if caption_position not in {"lower_safe", "center"}:
        raise ValueError("Caption position is unsupported.")
    font_size = max(20, round(height * size_scale))
    margin_lr = max(24, round(width * 0.065))
    # Keep burned-in text above YouTube Shorts' bottom controls and clear of
    # important lower-frame subjects. Captions stay centered and bounded by
    # generous side margins rather than covering the whole picture.
    margin_v = max(64, round(height * 0.15)) if caption_position == "lower_safe" else 0
    alignment = 2 if caption_position == "lower_safe" else 5
    border_style, outline, shadow, back_colour = (
        (3, 1.5, 0, "&H80000000")
        if caption_style == "classic"
        else (1, max(2, round(height * 0.002)), 1.2, "&H00000000")
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H99000000,"
            f"{back_colour},1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
            f"{alignment},{margin_lr},{margin_lr},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for block in blocks[1:]:
        cue_lines = block.splitlines()
        timestamp_index = next(
            (index for index, line in enumerate(cue_lines) if " --> " in line), None
        )
        if timestamp_index is None or timestamp_index + 1 >= len(cue_lines):
            raise ValueError("Captions contain a malformed WebVTT cue.")
        start_text, end_text = cue_lines[timestamp_index].split(" --> ", maxsplit=1)
        start = _vtt_timestamp_to_ass(start_text.strip())
        end = _vtt_timestamp_to_ass(end_text.split(maxsplit=1)[0].strip())
        text = " ".join(cue_lines[timestamp_index + 1 :]).strip()
        text = (
            _VTT_TAG.sub("", html.unescape(text))
            .replace("\\", r"\\")
            .replace("{", r"\{")
            .replace("}", r"\}")
        )
        if not text:
            raise ValueError("Captions contain an empty WebVTT cue.")
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def _jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    if len(content) < 4 or not content.startswith(bytes.fromhex("ffd8")):
        return None
    offset = 2
    while offset + 4 <= len(content):
        if content[offset] != 0xFF:
            return None
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            return None
        marker = content[offset]
        offset += 1
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(content):
            return None
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(content):
            return None
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8:
                return None
            component_count = content[offset + 7]
            if component_count < 1 or segment_length < 8 + 3 * component_count:
                return None
            height = int.from_bytes(content[offset + 3 : offset + 5], "big")
            width = int.from_bytes(content[offset + 5 : offset + 7], "big")
            return width, height
        if marker == 0xDA:
            return None
        offset += segment_length
    return None


def _visual_dimensions(visual) -> tuple[int, int] | None:
    if visual.mime_type == "image/png":
        if (
            len(visual.content) < 24
            or not visual.content.startswith(bytes.fromhex("89504e470d0a1a0a"))
            or visual.content[12:16] != b"IHDR"
        ):
            return None
        return struct.unpack(">II", visual.content[16:24])
    if visual.mime_type == "image/jpeg":
        return _jpeg_dimensions(visual.content)
    return None


class MediaRenderError(RuntimeError):
    """A media render or inspection operation failed."""


class FFmpegVideoRenderer:
    """Render with an explicitly configured FFmpeg build and encoder.

    No shell is used. All inputs are staged in a private temporary directory,
    command arguments are generated by this adapter, and process time/size
    limits are enforced. Encoder selection is explicit so deployments can
    make and document their FFmpeg licensing choice.
    """

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        ffprobe_path: str,
        video_encoder: str,
        audio_encoder: str = "aac",
        motion_style: str = "gentle_drift",
        video_preset: str = "veryfast",
        video_crf: int = 23,
        audio_bitrate: str = "128k",
        timeout_seconds: float = 180.0,
        max_output_bytes: int = 100_000_000,
    ) -> None:
        if not ffmpeg_path.strip() or not ffprobe_path.strip():
            raise ValueError("FFmpeg and ffprobe executable paths are required.")
        if not video_encoder.strip() or not audio_encoder.strip():
            raise ValueError("Video and audio encoders must be explicitly configured.")
        if motion_style not in {"static", "gentle_drift"}:
            raise ValueError("Motion style must be 'static' or 'gentle_drift'.")
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("Render timeout and output-size limit must be positive.")
        if not 0 <= video_crf <= 51:
            raise ValueError("Video CRF must be between 0 and 51.")
        if not video_preset.strip() or not audio_bitrate.strip():
            raise ValueError("Video preset and audio bitrate are required.")

        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._video_encoder = video_encoder
        self._audio_encoder = audio_encoder
        self._motion_style = motion_style
        self._video_preset = video_preset
        self._video_crf = video_crf
        self._audio_bitrate = audio_bitrate
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    @staticmethod
    def _validate_request(request: RenderRequest) -> None:
        if request.caption_style not in {"classic", "bold_outline"}:
            raise ValueError("Caption style is unsupported.")
        if request.caption_position not in {"lower_safe", "center"}:
            raise ValueError("Caption position is unsupported.")
        if request.caption_size not in {"small", "medium", "large"}:
            raise ValueError("Caption size is unsupported.")
        if not 2 <= len(request.visual_assets) <= 6:
            raise ValueError("A render requires between two and six scene visuals.")
        if len(request.scene_durations_seconds) != len(request.visual_assets):
            raise ValueError("Every visual scene must have exactly one duration.")
        if any(not 1 <= duration <= 30 for duration in request.scene_durations_seconds):
            raise ValueError("Scene durations must be between one and 30 seconds.")
        if sum(request.scene_durations_seconds) != request.target_duration_seconds:
            raise ValueError("Scene durations must sum to the target video duration.")
        if not 15 <= request.target_duration_seconds <= 45:
            raise ValueError("Target video duration must be between 15 and 45 seconds.")
        if (
            request.width < 360
            or request.height < 640
            or request.width > 2160
            or request.height > 3840
            or request.width % 2
            or request.height % 2
            or not any(
                abs(request.width / request.height - ratio) <= 0.01
                for ratio in (9 / 16, 16 / 9, 1.0)
            )
        ):
            raise ValueError(
                "Video dimensions must use a supported 9:16, 16:9, or 1:1 ratio within limits."
            )
        if not 24 <= request.frame_rate <= 60:
            raise ValueError("Video frame rate must be between 24 and 60.")
        if request.motion_style not in {None, "static", "gentle_drift"}:
            raise ValueError("Motion style must be 'static' or 'gentle_drift'.")
        if request.audio_asset.mime_type != "audio/wav" or not request.audio_asset.content:
            raise ValueError("Renderer requires a non-empty WAV narration asset.")
        if (
            len(request.audio_asset.content) > 25_000_000
            or not 8_000 <= request.audio_asset.sample_rate_hz <= 96_000
            or request.audio_asset.channels not in {1, 2}
            or not 250 <= request.audio_asset.duration_ms <= 90_000
        ):
            raise ValueError("Narration audio exceeds supported media input limits.")

        target_duration_ms = request.target_duration_seconds * 1000
        if (
            not round(target_duration_ms * 0.80)
            <= request.audio_asset.duration_ms
            <= round(target_duration_ms * 1.20)
        ):
            raise ValueError(
                "Narration audio must be between 80% and 120% of the target video "
                "duration so FFmpeg can time-compress it within a natural range."
            )
        if not request.captions_vtt.startswith("WEBVTT"):
            raise ValueError("Captions must be a WebVTT document.")
        if len(request.captions_vtt.encode("utf-8")) > 100_000:
            raise ValueError("Captions exceed the configured size limit.")

        music = request.background_music_asset
        if music is not None:
            if (
                music.mime_type != "audio/wav"
                or not isinstance(music.content, bytes)
                or not music.content
                or len(music.content) > 25_000_000
                or not 8_000 <= music.sample_rate_hz <= 96_000
                or music.channels not in {1, 2}
                or not 250 <= music.duration_ms <= 180_000
            ):
                raise ValueError("Background music audio is outside supported media limits.")
            metadata = music.metadata
            required_text = (
                "asset_id",
                "title",
                "creator",
                "license_id",
                "attribution_text",
            )
            if (
                not isinstance(metadata, dict)
                or metadata.get("schema_version") != "1.0.0"
                or metadata.get("rights_status") != "cleared"
                or any(
                    not isinstance(metadata.get(key), str) or not metadata[key].strip()
                    for key in required_text
                )
                or metadata.get("commercial_use") is not True
                or metadata.get("derivative_works") is not True
                or not isinstance(metadata.get("usage_scope"), list)
                or not all(isinstance(scope, str) for scope in metadata["usage_scope"])
                or not {"youtube", "commercial"}.issubset(metadata["usage_scope"])
                or not isinstance(metadata.get("content_sha256"), str)
                or metadata["content_sha256"] != hashlib.sha256(music.content).hexdigest()
            ):
                raise ValueError("Background music requires verified, cleared licensing metadata.")
            for key in ("source_url", "license_url"):
                value = metadata.get(key)
                try:
                    parsed = urlsplit(value) if isinstance(value, str) else None
                    valid_url = (
                        parsed is not None
                        and parsed.scheme == "https"
                        and parsed.hostname is not None
                        and parsed.username is None
                        and parsed.password is None
                    )
                except ValueError:
                    valid_url = False
                if not valid_url:
                    raise ValueError("Background music source and license URLs must use HTTPS.")

        total_visual_bytes = 0
        for visual in request.visual_assets:
            if (
                visual.mime_type not in {"image/png", "image/jpeg"}
                or not visual.content
                or not visual.rights_basis.strip()
            ):
                raise ValueError("Every scene must be a rights-provenanced PNG or JPEG visual.")
            if not 1 <= visual.width <= 2160 or not 1 <= visual.height <= 3840:
                raise ValueError("Scene visual dimensions are outside the supported range.")
            if len(visual.content) > 25_000_000:
                raise ValueError("Scene visual exceeds the configured input-size limit.")
            if _visual_dimensions(visual) != (visual.width, visual.height):
                raise ValueError("Scene visual dimensions do not match its image header.")
            total_visual_bytes += len(visual.content)
        if total_visual_bytes > 100_000_000:
            raise ValueError("Scene visuals exceed the total input-size limit.")

    async def _run(self, executable: str, arguments: list[str]) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MediaRenderError("Configured media executable could not be started.") from exc

        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise MediaRenderError("Media process exceeded its execution time limit.") from exc
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise

        if process.returncode != 0:
            raise MediaRenderError("Media process failed; detailed output was suppressed.")
        return stdout

    def _ffmpeg_arguments(self, request: RenderRequest, directory: Path) -> list[str]:
        arguments = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
        ]
        for index, duration in enumerate(request.scene_durations_seconds):
            extension = "jpg" if request.visual_assets[index].mime_type == "image/jpeg" else "png"
            image_path = directory / f"scene-{index:02d}.{extension}"
            arguments.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    str(request.frame_rate),
                    "-t",
                    str(duration),
                    "-i",
                    str(image_path),
                ]
            )

        audio_index = len(request.visual_assets)
        arguments.extend(["-i", str(directory / "narration.wav")])
        music_index = None
        if request.background_music_asset is not None:
            music_index = audio_index + 1
            arguments.extend(
                [
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(directory / "background-music.wav"),
                ]
            )
        captions_index = audio_index + (2 if music_index is not None else 1)
        arguments.extend(["-i", str(directory / "captions.vtt")])

        filters = []
        for index, duration in enumerate(request.scene_durations_seconds):
            motion_style = request.motion_style or self._motion_style
            if motion_style == "gentle_drift":
                # 12% overscan gives portrait stills enough room for a visible
                # Ken Burns move without exposing empty edges during the crop.
                overscan_width = request.width + max(2, request.width * 12 // 100 // 2 * 2)
                overscan_height = request.height + max(2, request.height * 12 // 100 // 2 * 2)
                frame_count = duration * request.frame_rate
                x_operator, y_operator = ("-", "+") if index % 2 == 0 else ("+", "-")
                motion = (
                    f"scale={overscan_width}:{overscan_height}:"
                    "force_original_aspect_ratio=increase,"
                    f"crop={request.width}:{request.height}:"
                    f"x='(iw-ow)*0.5*(1{x_operator}cos(PI*n/{frame_count}))':"
                    f"y='(ih-oh)*0.5*(1{y_operator}cos(PI*n/{frame_count}))',"
                )
            else:
                motion = (
                    f"scale={request.width}:{request.height}:force_original_aspect_ratio=increase,"
                    f"crop={request.width}:{request.height},"
                )
            filters.append(
                f"[{index}:v]"
                f"{motion}"
                f"setsar=1,fps={request.frame_rate},format=yuv420p,"
                f"trim=duration={duration},setpts=PTS-STARTPTS[v{index}]"
            )
        video_inputs = "".join(f"[v{index}]" for index in range(len(request.visual_assets)))
        filters.append(f"{video_inputs}concat=n={len(request.visual_assets)}:v=1:a=0[vout]")
        caption_path = (
            str(directory / "captions.ass")
            .replace("\\", "/")
            .replace(":", "\\:")
            .replace("'", "\\'")
        )
        filters.append(f"[vout]ass='{caption_path}'[vburn]")
        audio_tempo = request.audio_asset.duration_ms / (request.target_duration_seconds * 1000)
        narration_filter = (
            f"[{audio_index}:a]atempo={audio_tempo:.6f},aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={request.target_duration_seconds},"
            "loudnorm=I=-14:TP=-1.0:LRA=11,volume=2dB,asetpts=PTS-STARTPTS"
        )
        if music_index is None:
            filters.append(f"{narration_filter},alimiter=limit=0.85:level=false[aout]")
        else:
            filters.append(f"{narration_filter}[voice]")
            filters.append(
                f"[{music_index}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                "volume=0.12,"
                f"atrim=duration={request.target_duration_seconds},"
                "asetpts=PTS-STARTPTS[music]"
            )
            # The narration is the sidechain input: music level is reduced while
            # speech is present, then recovers during pauses.
            filters.append(
                "[music][voice]sidechaincompress="
                "threshold=0.025:ratio=8:attack=20:release=500:makeup=1[ducked]"
            )
            filters.append(
                "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=2,"
                "alimiter=limit=0.85:level=false[aout]"
            )

        output_path = directory / "render.mp4"
        arguments.extend(
            [
                # Keep one filter-complex worker: the media container is
                # intentionally CPU- and PID-limited, and FFmpeg's default
                # per-filter thread fan-out can fail while configuring scale
                # filters for multi-scene renders.
                "-filter_complex_threads",
                "1",
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vburn]",
                "-map",
                "[aout]",
                "-map",
                f"{captions_index}:s:0?",
                "-c:v",
                self._video_encoder,
                "-preset",
                self._video_preset,
                "-crf",
                str(self._video_crf),
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(request.frame_rate),
                "-c:a",
                self._audio_encoder,
                "-b:a",
                self._audio_bitrate,
                "-ar",
                "48000",
                "-c:s",
                "mov_text",
                "-t",
                str(request.target_duration_seconds),
                "-fs",
                str(self._max_output_bytes),
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        return arguments

    async def _run_diagnostics(self, path: Path) -> str:
        arguments = [
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.02:pic_th=0.999",
            "-af",
            "ebur128=peak=true:framelog=quiet",
            "-f",
            "null",
            "-",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                self._ffmpeg_path,
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise MediaRenderError("FFmpeg quality analysis could not be started.") from exc

        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise MediaRenderError("FFmpeg quality analysis exceeded its time limit.") from exc
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise

        if process.returncode != 0:
            raise MediaRenderError("FFmpeg quality analysis failed.")
        if len(stderr) > 65_536:
            raise MediaRenderError("FFmpeg quality analysis output exceeded its size limit.")
        return stderr.decode("utf-8", errors="replace")

    async def _probe(
        self,
        path: Path,
        *,
        include_diagnostics: bool = False,
    ) -> MediaQualityReport:
        arguments = [
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ]
        output = await self._run(self._ffprobe_path, arguments)
        try:
            payload = json.loads(output)
            streams = payload.get("streams", [])
            format_duration = float(payload["format"]["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaRenderError("ffprobe returned invalid media metadata.") from exc

        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        captions = next((item for item in streams if item.get("codec_type") == "subtitle"), None)
        issues = []
        integrated_loudness_lufs = None
        true_peak_dbfs = None
        black_frame_segments: tuple[tuple[float, float], ...] = ()
        if video is None:
            issues.append("missing_video_stream")
        if audio is None:
            issues.append("missing_audio_stream")
        if captions is None:
            issues.append("missing_caption_stream")

        width = int(video.get("width", 0)) if video else 0
        height = int(video.get("height", 0)) if video else 0
        video_codec = str(video.get("codec_name", "")) if video else ""
        audio_codec = str(audio.get("codec_name", "")) if audio else ""
        duration_ms = round(format_duration * 1000) if math.isfinite(format_duration) else 0
        if video is not None and (width <= 0 or height <= 0):
            issues.append("invalid_video_dimensions")
        if video is not None and width > 0 and height > 0:
            if not any(abs(width / height - ratio) <= 0.01 for ratio in (9 / 16, 16 / 9, 1.0)):
                issues.append("invalid_video_aspect_ratio")
        if video is not None and not video_codec:
            issues.append("invalid_video_codec")
        if audio is not None and not audio_codec:
            issues.append("invalid_audio_codec")
        if not math.isfinite(format_duration) or not 15_000 <= duration_ms <= 45_000:
            issues.append("invalid_video_duration")

        if include_diagnostics and video is not None and audio is not None:
            diagnostics = await self._run_diagnostics(path)
            loudness_match = re.search(
                r"^\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS\s*$",
                diagnostics,
                re.MULTILINE,
            )
            peak_match = re.search(
                r"^\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS\s*$",
                diagnostics,
                re.MULTILINE,
            )
            if loudness_match is not None:
                try:
                    value = float(loudness_match.group(1))
                    if math.isfinite(value):
                        integrated_loudness_lufs = value
                except ValueError:
                    pass
            if peak_match is not None:
                try:
                    value = float(peak_match.group(1))
                    if math.isfinite(value):
                        true_peak_dbfs = value
                except ValueError:
                    pass

            black_segments = []
            for match in re.finditer(
                r"black_start:(-?\d+(?:\.\d+)?)\s+"
                r"black_end:(-?\d+(?:\.\d+)?)\s+"
                r"black_duration:(-?\d+(?:\.\d+)?)",
                diagnostics,
            ):
                start, end, duration = (float(item) for item in match.groups())
                if math.isfinite(start) and math.isfinite(end) and duration >= 0.5:
                    black_segments.append((start, end))
            black_frame_segments = tuple(black_segments)

            if integrated_loudness_lufs is None:
                issues.append("audio_loudness_unavailable")
            elif not -17.0 <= integrated_loudness_lufs <= -11.0:
                issues.append("audio_loudness_out_of_range")
            if true_peak_dbfs is None:
                issues.append("audio_true_peak_unavailable")
            elif true_peak_dbfs > -0.5:
                issues.append("audio_true_peak_exceeded")
            if black_frame_segments:
                issues.append("black_frames_detected")

        return MediaQualityReport(
            is_valid=not issues,
            duration_ms=duration_ms,
            width=width,
            height=height,
            video_codec=video_codec,
            audio_codec=audio_codec,
            has_audio=audio is not None,
            has_captions=captions is not None,
            issues=tuple(issues),
            integrated_loudness_lufs=integrated_loudness_lufs,
            true_peak_dbfs=true_peak_dbfs,
            black_frame_segments=black_frame_segments,
        )

    async def render(self, request: RenderRequest) -> VideoAsset:
        self._validate_request(request)
        with tempfile.TemporaryDirectory(prefix="aurora-media-render-") as temporary:
            directory = Path(temporary)
            for index, visual in enumerate(request.visual_assets):
                extension = "jpg" if visual.mime_type == "image/jpeg" else "png"
                (directory / f"scene-{index:02d}.{extension}").write_bytes(visual.content)
            (directory / "narration.wav").write_bytes(request.audio_asset.content)
            if request.background_music_asset is not None:
                (directory / "background-music.wav").write_bytes(
                    request.background_music_asset.content
                )
            (directory / "captions.vtt").write_text(request.captions_vtt, encoding="utf-8")
            (directory / "captions.ass").write_text(
                _vtt_to_ass(
                    request.captions_vtt,
                    width=request.width,
                    height=request.height,
                    caption_style=request.caption_style,
                    caption_position=request.caption_position,
                    caption_size=request.caption_size,
                ),
                encoding="utf-8",
            )

            await self._run(
                self._ffmpeg_path,
                self._ffmpeg_arguments(request, directory),
            )
            output_path = directory / "render.mp4"
            if not output_path.is_file():
                raise MediaRenderError("FFmpeg completed without producing a video file.")
            output_size = output_path.stat().st_size
            if output_size <= 0 or output_size > self._max_output_bytes:
                raise MediaRenderError("Rendered video is empty or exceeds the output-size limit.")

            report = await self._probe(output_path)
            if (
                not report.is_valid
                or report.width != request.width
                or report.height != request.height
                or abs(report.duration_ms - request.target_duration_seconds * 1000) > 750
            ):
                raise MediaRenderError(
                    f"Rendered video failed technical quality checks: {','.join(report.issues)}"
                )
            content = output_path.read_bytes()

        return VideoAsset(
            content=content,
            mime_type="video/mp4",
            duration_ms=report.duration_ms,
            width=report.width,
            height=report.height,
            video_codec=report.video_codec,
            audio_codec=report.audio_codec,
            provider_id="ffmpeg",
            metadata={
                "background_music": (
                    dict(request.background_music_asset.metadata)
                    if request.background_music_asset is not None
                    else None
                )
            },
        )

    async def inspect(self, content: bytes) -> MediaQualityReport:
        if not isinstance(content, bytes) or not content:
            raise ValueError("Video content must be non-empty bytes.")
        if len(content) > self._max_output_bytes:
            raise MediaRenderError("Video exceeds the configured inspection-size limit.")
        with tempfile.TemporaryDirectory(prefix="aurora-media-inspect-") as temporary:
            path = Path(temporary) / "inspect.mp4"
            path.write_bytes(content)
            return await self._probe(path, include_diagnostics=True)
