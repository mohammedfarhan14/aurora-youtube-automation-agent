# Word-level captions and forced alignment

The media direction stage emits deterministic one-word WebVTT cues using scene duration and word-length weighting. During rendering, the pipeline prefers exact `TTSWordTiming` records persisted by the selected TTS provider.

If the TTS provider does not return word timings, an optional `WhisperXAlignmentProvider` can be injected into `run_media_stage_once`. Its contract accepts the narration audio, exact narration text and language, and returns absolute audio-timeline word timings. The core project intentionally does not choose a hosted endpoint, model, or GPU runtime; those vary by deployment and must be configured by the operator.

Aligned tokens must match narration order, contain one word each, and have monotonic in-range timestamps. Invalid alignments fail the render rather than silently displaying incorrect words. No external alignment service or credentials are used by default.
