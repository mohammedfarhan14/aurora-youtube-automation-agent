# Aurora YouTube Automation Agent

Aurora Studio is a local-first workspace that turns researched topics into reviewable, narrated YouTube videos. It combines source retrieval, LLM story drafting, scene-media generation, TTS, captioned FFmpeg rendering, rights/quality review, and approval-gated YouTube publishing.

This release is optimized for a resource-constrained local machine: workers run in stages, existing local models are reused, and the launcher does not download model weights. Configure only the integrations you have installed.

## Local quick start

Requirements: Linux/WSL2, Docker Compose v2, Node.js 24, and configured local/provider services for the features you want to use.

```bash
git clone https://github.com/mohammedfarhan14/aurora-youtube-automation-agent.git
cd aurora-youtube-automation-agent
cp .env.example .env
# Edit .env: replace placeholders and configure your chosen providers.
./scripts/ops/start-aurora-demo.sh start
```

Open <http://localhost:5173>. Stop with `./scripts/ops/start-aurora-demo.sh stop`; this keeps persistent database and media volumes. See [Local demo](docs/operations/local-demo.md) for setup, workflow and limitations.

## Video workflow

**Research → story → scenes and narration → captions and render → technical/editorial review → approval → YouTube upload.** You can configure title, description, tags, visibility, channel style and provider settings in the UI. The manual publisher shows job state and YouTube processing after queueing.

## Current scope

ComfyUI currently supplies scene still images; FFmpeg can animate those images with camera motion. The default path does not generate full motion-video clips. Caption timing may be estimated when word-level alignment is not configured. Live provider behavior and YouTube publishing depend on your credentials, channel consent and local hardware. Test with private visibility first.

This repository does not currently declare an open-source license.
