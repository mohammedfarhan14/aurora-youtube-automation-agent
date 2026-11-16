# Media production worker

The media worker is opt-in. It executes the durable production stages after story generation and claim verification:

1. `ai_direction` — creates a deterministic scene plan and WebVTT captions from the verified story.
2. `narration` — calls an explicitly configured OpenAI-compatible TTS endpoint and stores WAV audio.
3. `visual_generation` — choose rights-filtered Wikimedia Commons retrieval, offline local abstract cards, or the opt-in ComfyUI still-image workflow. ComfyUI requires an API-format workflow and a SaveImage output node; see [`config/comfyui/README.md`](../../config/comfyui/README.md). ComfyUI images are marked synthetic and still require review. Aurora's current adapter does not accept generated video clips or motion-transfer outputs.
4. `rendering` — assembles scene images, narration and a subtitle track into a portrait MP4 using FFmpeg.
5. `quality_check` — inspects the MP4 with ffprobe and moves a valid run to `awaiting_review`.

The default `chromium_html` composer uses an offline HTML template to art-direct a still visual with a short line from the verified narration, contrast gradients and restrained scene labeling; FFmpeg still performs portrait motion, audio/caption assembly and QA. Chromium has no network access and is installed only in the media worker image, never in the API or lightweight workers. Set `MEDIA_VISUAL_COMPOSER=pillow` to use the lighter Pillow card renderer. This treatment improves composition but does not create motion footage; realistic source imagery still requires a configured visual provider such as Wikimedia Commons.

The media worker and its single egress proxy share an internal network. The proxy permits public HTTPS for configured provider APIs and assets while rejecting private/reserved destinations; local HTTP exceptions are limited to Qwen at `host.docker.internal:8880` and ComfyUI at `host.docker.internal:8188`. The worker itself has no direct internet route. Hosted environments still need provider-specific egress rules.

A connected YouTube account is not uploaded to by this worker. The owner must still review and approve the completed production run, and approval is rejected if the quality manifest contains unresolved or unsupported factual claims.

## Open-source TTS deployment candidate

For a self-hosted speech runtime, the first candidate to evaluate is [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) served by [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI). The model card identifies the model weights as Apache-2.0; the server repository is also Apache-2.0 and exposes an OpenAI-compatible `POST /v1/audio/speech` endpoint. The upstream release current at this documentation update is `v0.10.0` (8 November 2026). It provides CPU and NVIDIA GPU container variants, so inference can run as a separate service rather than adding model dependencies or GPU requirements to the Aurora API/worker image.

Example local endpoint configuration:

- `MEDIA_TTS_BASE_URL=http://kokoro-tts:8880/v1`
- `MEDIA_TTS_MODEL=kokoro`
- `MEDIA_TTS_VOICE=af_heart` (confirm the selected voice exists in the pinned server/voice-pack release)
- `MEDIA_TTS_RIGHTS_BASIS=licensed`
- `MEDIA_TTS_LICENSE_ID=Apache-2.0`
- `MEDIA_TTS_LICENSE_URI=https://www.apache.org/licenses/LICENSE-2.0`
- `MEDIA_TTS_SOURCE_URI=https://huggingface.co/hexgrad/Kokoro-82M`
- `MEDIA_TTS_ATTRIBUTION=Kokoro-82M by hexgrad; Kokoro-FastAPI by remsky`

This is an evaluation candidate, not an automatically enabled provider or a legal conclusion that every bundled dependency, voice, or generated output has identical licensing. Review the exact model revision, voice pack, phonemizer/G2P dependencies, container SBOM and notices before distribution or commercial deployment. Record the exact selected revision and license evidence for each deployed runtime. Do not use a mutable `:latest` tag in staging or production; pin a reviewed release and immutable image digest. Keep the TTS endpoint on a private worker network, require an API key where supported, and use TLS if traffic crosses a host/network boundary.

Kokoro-FastAPI documents that its streamed WAV response may use `0xFFFFFFFF` sentinel values for RIFF/data lengths, which are not interpreted as a finite duration by Python's standard `wave` module. Aurora normalizes those size fields against the already bounded response bytes before validating the WAV stream. A mocked regression test covers this compatibility behavior; a real service/voice acceptance test is still required before enabling the media worker.

## Runtime prerequisites

- PostgreSQL reachable through `DATABASE_URL`.
- A media storage backend. The default `local` adapter requires a persistent, absolute `MEDIA_STORAGE_ROOT` and is intended for development or a single worker node. For multi-replica deployments, select the private S3-compatible adapter; it supports AWS S3, Cloudflare R2 and MinIO-style endpoints.
- `ffmpeg` and `ffprobe` executables available to the worker. The dedicated Compose media image installs FFmpeg and verifies `libx264`; the API/lightweight worker image remains slim.
- An explicitly operated TTS service implementing `POST /v1/audio/speech` and returning WAV audio.
- An FFmpeg build containing the explicitly configured video/audio encoders and the `mov_text` subtitle encoder.
- When Wikimedia Commons is selected, the application validates the Commons API, `upload.wikimedia.org`/`thumb.wikimedia.org` thumbnail hosts and bounded thumbnail sizing parameters. The ComfyUI adapter uses an operator-supplied workflow and routes only the Docker-host endpoint through the proxy. Hosted egress must be restricted to exact approved endpoints.

The application does not download model weights, install TTS runtimes, or silently fall back to a paid provider. Wikimedia retrieves existing rights-filtered stills; ComfyUI can generate still images from the configured workflow. Aurora does not currently produce motion-video clips through ComfyUI.

## Configuration

Set these values only when intentionally enabling media production:

- `MEDIA_WORKER_ENABLED=true`
- `MEDIA_TTS_BASE_URL`, `MEDIA_TTS_MODEL`, `MEDIA_TTS_VOICE`
- Optional `MEDIA_TTS_API_KEY`
- `MEDIA_TTS_RIGHTS_BASIS` and `MEDIA_TTS_SOURCE_URI` (an HTTPS page identifying the selected model/voice and its rights). For `cc0`, `licensed` and `cc_by`, also set `MEDIA_TTS_LICENSE_ID` and `MEDIA_TTS_LICENSE_URI`; for `licensed` and `cc_by`, set `MEDIA_TTS_ATTRIBUTION`. The quality gate fails closed when any required source/license URL or attribution is missing or not HTTPS.
- `MEDIA_STORAGE_BACKEND=local` (default) and `MEDIA_STORAGE_ROOT` (absolute persistent path), or `MEDIA_STORAGE_BACKEND=s3` with `MEDIA_S3_ENDPOINT_URL`, `MEDIA_S3_BUCKET`, `MEDIA_S3_REGION`, `MEDIA_S3_ACCESS_KEY_ID` and `MEDIA_S3_SECRET_ACCESS_KEY`. Optional temporary credentials use `MEDIA_S3_SESSION_TOKEN`. The S3 adapter uses private path-style requests, AWS Signature Version 4, bounded downloads and no public ACLs.
- `MEDIA_FFMPEG_PATH`, `MEDIA_FFPROBE_PATH`
- `MEDIA_VISUAL_PROVIDER=local_abstract` (default) or `wikimedia_commons`
- `MEDIA_VISUAL_PROVIDER=comfyui` with `MEDIA_COMFYUI_BASE_URL`, `MEDIA_COMFYUI_WORKFLOW_PATH` and `MEDIA_COMFYUI_OUTPUT_NODE_ID`; optional API key, style/negative prompts, and a bounded task timeout are supported. The local Docker endpoint is allowlisted only as `host.docker.internal:8188` through the media proxy.
- When using Wikimedia Commons: `MEDIA_WIKIMEDIA_USER_AGENT` (Aurora Studio identifier with a valid operator email or HTTPS contact), `MEDIA_WIKIMEDIA_TIMEOUT_SECONDS`, `MEDIA_WIKIMEDIA_MAX_API_RESPONSE_BYTES` and `MEDIA_WIKIMEDIA_MAX_IMAGE_BYTES`
- `MEDIA_VIDEO_ENCODER` (required; must exist in the installed FFmpeg build)
- `MEDIA_MOTION_STYLE` (`gentle_drift` by default, or `static`); gentle drift adds a slow, alternating 5% camera move to still-image scenes during the single FFmpeg render. It creates no intermediate frame files.

### Channel visual direction

The channel's versioned editorial profile can also set a ComfyUI style suffix, lighting direction, render motion (`static` or `gentle_drift`), and output shape (`9:16`, `16:9`, or `1:1`). New production runs snapshot these values; changing the profile does not alter a run already in progress. Output shape changes render dimensions to 1080×1920, 1920×1080, or 1080×1080 respectively. These controls do not generate motion footage: ComfyUI currently supplies scene stills and FFmpeg applies the selected camera move.

For local development, a self-hosted TTS endpoint may use an HTTP loopback URL such as `http://127.0.0.1:8880/v1`. Production requires HTTPS for the configured TTS endpoint. S3-compatible endpoints may use HTTP only in non-production environments (for example, a local MinIO service); production requires HTTPS. Keep a dedicated bucket private. Grant the worker `s3:ListBucket` on that bucket for bounded orphan inventory, plus `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` for its objects. Do not share this bucket with unrelated application data; the current inventory scans the dedicated media bucket and filters to Aurora's strict UUID-partitioned object keys.

The TTS model and voice-pack rights are an explicit operator assertion recorded with the audio artifact, including the source URI, license identifier, canonical license URI and required attribution. Visual and audio attribution credits are carried into the quality-check stage manifest so a future publisher can construct a compliant YouTube description. Unknown or incomplete rights metadata blocks publishing approval. The encoder choice is a deployment decision. The API/lightweight worker image does not include FFmpeg; the dedicated media image installs FFmpeg only when explicitly built with `INSTALL_FFMPEG=true`. For example, `libx264` requires a GPL-enabled FFmpeg build. Review the FFmpeg build configuration, codec/library licenses, patent obligations and source-distribution obligations before distributing a worker image. The application does not select an encoder implicitly.

## Start the worker

Set `MEDIA_WORKER_ENABLED=true` only when the TTS endpoint, storage backend, FFmpeg encoder and required secrets are configured. The worker is opt-in and has its own Compose profile. To run the complete local application, story and media workers:

```bash
docker compose --profile app --profile story --profile media up -d
```

The `media-production-worker` service uses a separate image built with `INSTALL_FFMPEG=true`, a persistent local media volume, a read-only root filesystem, a bounded temporary filesystem, dropped Linux capabilities and an `on-failure` restart policy. If `MEDIA_WORKER_ENABLED=false`, the process exits cleanly rather than restarting in a loop.

For a bounded one-shot pass:

```bash
docker compose --profile media run --rm media-production-worker python -m app.workers.run_media_production_worker --drain --max-runtime-seconds 3600
```

For a host-based development run instead of Compose:

```bash
cd backend/api
python -m app.workers.run_media_production_worker
```

The media worker uses PostgreSQL stage leases, renewals, retry recovery and idempotent media artifact registration. A drain run exits when no media stage is currently runnable; if story stages are still pending, run the media worker again after the story worker completes them. This makes scheduled, n8n-style execution safe: each invocation claims only the next runnable stage, commits its manifest and artifacts, and exits without redoing completed stages. If an invocation is interrupted, lease recovery returns only the in-flight stage to the queue.

For a low-memory machine, keep PostgreSQL, Redis and the API running, then invoke
the story and media workers in bounded drain mode. Stop a model service only after
its stage has completed and its manifest is committed. Ollama can unload a model
after each request with `OLLAMA_KEEP_ALIVE=0`; Kokoro/Qwen-TTS may be stopped after
the `narration` stage, and ComfyUI after `visual_generation`. Do not stop a service
while a stage lease is active, and do not remove the PostgreSQL or media storage
volumes between stages. A future host controller can use the same stage status and
lease boundaries to start/stop Windows model processes without risking duplicate or
partial work.

## Current limitations

- S3-compatible storage is implemented with SigV4 and bounded object reads/writes, but it has only deterministic HTTP transport tests so far; live AWS/R2/MinIO/OCI interoperability and automated retention/lifecycle policy remain outstanding.
- Local abstract visuals remain the default. The opt-in Wikimedia Commons adapter accepts only CC0 1.0 and CC BY 4.0 still images, validates exact HTTPS hosts/paths and image dimensions, rejects redirects, bounds API/image responses and preserves source/license/attribution metadata. CC BY credits include creator, title, source, license and the resize/crop modification notice; the future publisher must include those credits in the YouTube description or another compliant presentation.
- TTS is an endpoint adapter; the TTS model/runtime/voice pack must be selected, licensed and operated separately.
- No automated image safety/moderation classifier or rendered-video preview/per-scene review UI is integrated. Retrieved visuals remain subject to editorial review.
- Story scene narration segments are validated as an ordered, verbatim partition of the full narration before captions are generated. Captions are split into readable cues of at most seven words. Providers with word timings preserve those timings; providers without them distribute cues over the measured narration duration, then add an invisible tail cue only for video padding. This prevents drift when a short narration is padded to the target duration. Phoneme/word alignment remains optional follow-up work for karaoke-level timing.
- The renderer rejects narration audio that would require more than two seconds of
  silence padding or more than 750 ms of trimming to fit the target duration. TTS
  timing still depends on the selected voice/model.
- Technical QA checks streams, dimensions, portrait aspect ratio, duration and subtitle presence. FFmpeg applies single-pass EBU-style loudness normalization targeting -14 LUFS and -1 dBTP; the QA pass measures integrated loudness and true peak, rejects output outside -17 to -11 LUFS or above -0.5 dBFS true peak, and detects near-black segments lasting at least 500 ms. The caption sidecar is also parsed before review: cue timestamps must be valid, monotonic and non-overlapping; cue text must be non-empty and no more than seven words; and the cue timeline must cover the rendered duration without extending beyond it. These are engineering guardrails, not a guarantee of subjective speech quality. True word/phoneme alignment remains follow-up work.
- A periodic orphan-object reconciliation worker removes stale, unregistered objects after a 24-hour grace period. It is part of the opt-in media worker; live bucket interoperability and provider-specific lifecycle policies still require deployment validation.
- The generated MP4 is placed in editorial review; this worker never uploads or publishes to YouTube.

## Orphaned object reconciliation

The media worker inventories private local or S3-compatible storage objects once per hour. It considers only UUID-partitioned media keys older than a 24-hour grace period, then checks the PostgreSQL media-artifact registry before deleting an unreferenced object. This protects normal upload/commit windows and ensures registered artifacts are retained.

S3-compatible inventory uses signed ListObjectsV2 requests, validates every returned key, and resumes pagination across worker polling cycles with a five-page scan budget per reconciliation run. Deletion failures are logged and retried on a later pass. The worker remains disabled by default; reconciliation runs only when the media worker is explicitly enabled.
