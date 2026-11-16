# Background music and rights metadata

The FFmpeg renderer accepts an optional `BackgroundMusicAsset` alongside narration. The durable media-stage handler can receive a tenant/run-aware `BackgroundMusicProvider` implementation through its injection point; no provider or track source is enabled by default. The track is looped only to the target render duration, kept at a conservative base gain, and dynamically ducked with FFmpeg's `sidechaincompress` filter using the narration as the sidechain. The mixed output is limited to prevent peaks.

A track is rejected before FFmpeg execution unless:
- It is a bounded WAV asset with supported sample rate, channel count and duration.
- Its metadata uses schema version `1.0.0`.
- Rights status is `cleared`, with explicit commercial use and derivative-work permission.
- The usage scope includes `youtube` and `commercial`.
- Source and license URLs use HTTPS, attribution is present, and the content SHA-256 matches the metadata.

The canonical metadata schema is `docs/schemas/music_metadata.schema.json`. The renderer does not discover, download, or license music. A production deployment must implement and inject a tenant/run-aware track provider, select a track source, verify the actual license terms and territory/term restrictions, and provide the track bytes and matching metadata. No track or credentials are bundled by default.

When present, the rights metadata is returned on the rendered `VideoAsset` and persisted into the video artifact metadata and attribution credits by the media stage handler. Human review and the existing publishing rights gate remain in force.
