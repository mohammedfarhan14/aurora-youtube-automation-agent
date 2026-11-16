# YouTube Shorts thumbnails

The generator in `app.media.thumbnail` creates a 1080x1920 JPEG from a background image, channel logo and title. It performs no file or network I/O. Pass a trusted font path for scripts not covered by Pillow's built-in default font. The output is capped at 50 MB, matching the current YouTube Data API thumbnail upload limit.

The YouTube upload client exposes `upload_thumbnail`. It accepts JPEG/PNG content, enforces the 50 MB limit, and calls `thumbnails.set` through the existing allowlisted YouTube HTTP client. The caller supplies the target video ID, a YouTube upload-capable OAuth access token, and the current processing status. For Shorts, the adapter refuses the request until YouTube reports `processingDetails.processingStatus=succeeded`; the API may not apply the thumbnail before processing finishes.

The production worker still needs a tenant-aware source for the background image and channel logo and an orchestration step that polls video processing status before calling the thumbnail uploader. The channel must be eligible to upload custom thumbnails. Tests use `httpx.MockTransport` and do not contact Google. No real upload or credentials are used in tests.
