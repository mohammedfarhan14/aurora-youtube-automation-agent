# YouTube altered/synthetic-media disclosure

Each `VisualAsset` carries an explicit `contains_synthetic_media` boolean. Visual providers must set it to `true` when their output contains realistic altered or synthetic material that requires YouTube disclosure. The current deterministic abstract and Wikimedia providers remain `false`; a future generative provider must make the declaration explicitly.

The visual-generation stage persists this flag per image artifact and in its stage manifest. Rendering aggregates the scene flags into the video artifact and render manifest. Final QA independently recomputes the value from the visual artifact records and blocks publication if the render/quality snapshots disagree.

The approved quality-manifest value is copied into the immutable YouTube publishing job snapshot. The worker passes that exact boolean to `videos.insert` as `status.containsSyntheticMedia`; it is never hardcoded in the upload client. The YouTube upload request already requests the `status` part.

The disclosure flag follows the provider's media manifest. Production AI visual providers must classify their outputs correctly and tests must cover both true and false cases. No YouTube upload or real credential is used in tests.
