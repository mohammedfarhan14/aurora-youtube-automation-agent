# Reference project assessment

Reviewed on 14 November 2026 (UTC).

## Pixelle-Video

[Pixelle-Video](https://github.com/ATH-MaaS/Pixelle-Video) is Apache-2.0 licensed. Aurora does not copy its application code or bundled assets. The useful product patterns are its separate system-configuration surface, swappable provider capabilities, clear script-to-visual-to-audio-to-video stages, template previews, and visible generation progress. The current Aurora Settings page adopts the configuration/status pattern, while Aurora keeps its own tenant, evidence, rights, review, and publishing workflow.

Pixelle's documented implementation also offers direct image/video APIs, ComfyUI/RunningHub workflows, script modes, audio preview, user media, background music and layout templates. Aurora currently has a provider registry for LLM/TTS, Commons still-image retrieval, an offline HTML/Chromium compositor, and FFmpeg rendering. Those Pixelle features are not automatically present in Aurora: direct motion-generation adapters, user asset import and review, music-library configuration, template preview/editor, and audio preview remain separate implementation work. Any provider must be wired through Aurora's worker boundaries, budget controls, rights metadata and moderation gates.

## youtube-automation-agent

[youtube-automation-agent](https://github.com/darkzOGx/youtube-automation-agent) remains the workflow and review benchmark: research and script generation, editable production stages, human approval, metadata, scheduling and analytics. Aurora's corresponding pipeline is independently implemented using durable stages, evidence and rights provenance, tenant-scoped artifacts, and a separate resumable publisher. The reference's documented self-hosted browser/FFmpeg setup does not establish an equivalent unrestricted Docker network policy for Aurora.

## Acceptance distinction

Reference features and local provider health checks do not certify a completed production run or YouTube publication. Aurora still needs a rights-cleared end-to-end render, private upload, YouTube processing check, thumbnail/metadata review, and recovery acceptance on an explicitly selected controlled test channel.
