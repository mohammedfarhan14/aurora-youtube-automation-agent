# Local demo

Aurora is a local-first production workflow. It can run live research, generate a story with a configured LLM, create scene stills with ComfyUI, synthesize narration, render captions/video with FFmpeg, and queue an explicitly approved upload to YouTube. The exact providers depend on your local `.env` and installed services.

## Requirements

- Linux or WSL2, Docker Engine/Desktop with Compose v2, and Node.js 24.
- Copy `.env.example` to `.env`, replace placeholder secrets, and select providers you have installed/configured.
- For local image generation, install ComfyUI and the model/workflow configured in the environment. The launcher does not download model weights or install packages.
- For speech, install/configure only the selected TTS bridge. Aurora does not start both Qwen TTS and Kokoro together.
- YouTube uploads require a Google OAuth application, YouTube Data API access, an authorized channel with upload scope, and a reviewed production run.

## Start and stop

From the repository root in WSL/Linux:

```bash
./scripts/ops/start-aurora-demo.sh start
```

Open <http://localhost:5173>. Stop the app without deleting persistent database/media volumes:

```bash
./scripts/ops/start-aurora-demo.sh stop
```

The same script can be started from Windows PowerShell by invoking the script through its `\\wsl.localhost\<Distro>` path. It infers the WSL distro, Linux user, and project path from that location. Alternatively, pass `-Distro`, `-LinuxUser`, and `-ProjectPath` explicitly.

## Demo workflow

1. Sign in and verify providers from Overview → Production readiness.
2. Configure the channel profile, visual provider/workflow, narration voice, caption style, and content niche in Settings.
3. Research a topic or start a draft from the channel's niche. Research results are source material; they do not automatically become a video until production is queued.
4. Review the generated scenes, narration, captions, rights warnings, title, description, tags, and visibility.
5. Approve the exact draft, then use **Upload now** or a configured schedule. The publishing card shows the worker status and YouTube processing state.

## Capabilities and limits

- ComfyUI creates scene still images. FFmpeg can apply camera motion to stills; generated motion-video clips are not provided by the default workflow.
- Caption alignment quality depends on the configured timing/alignment provider. Without word-level alignment, timings are estimates.
- A successful local render is not evidence that YouTube accepted an upload. Start with private visibility on a channel you control. Automatic publishing requires separate channel-owner consent and an enabled worker.
- Rights warnings need review. An audited manual override may document acceptance of editorial warnings, but it does not bypass missing channel access, malformed sources/attribution, or technical QA failures.
- Local machine memory, GPU VRAM and disk capacity bound model choices and throughput. The launcher uses existing installations and models only.

Provider credentials belong in the ignored local `.env`; never publish a populated environment file or OAuth tokens.
