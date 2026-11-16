# OCI Always Free demo deployment

This is the selected single-provider target for an always-on, free-tier Aurora
Studio showcase. OCI Always Free currently includes up to 2 Ampere A1 OCPUs and
12 GB RAM, 200 GB total boot/block volume storage, and 20 GB Object Storage.
Oracle can reclaim an Always Free VM that stays idle by its published CPU,
network and (for A1) memory thresholds. This is a constrained demo host, not an
availability commitment or a substitute for paid production operations. Check
the current OCI terms and region capacity before creating resources.

The compose stack runs the API, frontend, PostgreSQL, Redis, research/story/media
workers, YouTube publisher, isolated egress proxies and Caddy on one VM. Images
must support `linux/arm64`; the release workflow publishes both `linux/amd64` and
`linux/arm64` variants. OCI Object Storage is used through its S3-compatible API
so video artifacts remain separate from the VM disk and survive redeploys. The
adapter already implements S3 SigV4 and path-style requests, but OCI has not yet
been live-tested with this account; that interoperability smoke test is part of
deployment acceptance, not something CI can prove.

## Account and OCI resources

1. Create an OCI account and choose a home region with Always Free A1 capacity.
   Free capacity can be unavailable in some regions. OCI may require payment-card
   verification; do not proceed if the account console shows paid resources.
2. Create an Ubuntu ARM VM using `VM.Standard.A1.Flex`, 2 OCPUs, 12 GB memory,
   and a boot volume sized within the 200 GB free combined boot/block limit.
   Assign a reserved public IPv4 address.
3. In the VM's subnet security list/network security group, allow inbound TCP 22
   only from your administration IP, plus TCP 80 and 443 for the public demo.
   Do not expose PostgreSQL, Redis, worker ports, or Docker's API.
4. Create a **private Standard-tier Object Storage bucket** in the same region.
   The free allowance is limited to 20 GB across Object Storage tiers in an
   Always Free-only account. Track usage and delete demo artifacts when no
   longer needed.
5. Create an OCI Customer Secret Key for an OCI user that can manage objects in
   that bucket. Save its access-key ID and secret once; OCI does not show the
   secret again. Keep the permissions limited to the demo bucket.
6. Point a DNS name you control to the reserved IP with an A record. Caddy
   obtains and renews HTTPS automatically for that public name. Use this exact
   HTTPS origin for `FRONTEND_URL` and the Google OAuth redirect URI.

## Build and deploy

1. From GitHub Actions, run **Publish immutable application images** manually
   against the desired commit on `develop` (or create a reviewed `v*` release
   tag). Wait for all three image builds to finish. The resulting image tag is
   that exact commit SHA; do not use `latest`.
2. Connect to the VM over SSH. Install Docker Engine and the Docker Compose
   plugin using the official Docker instructions for Ubuntu ARM64.
3. Clone Aurora Studio at the same commit SHA used for the image tag. Create a
   private production environment file from the template:

   ```bash
   cp .env.production.example .env.production
   chmod 600 .env.production
   ```

4. Fill `.env.production` on the VM (never commit it):
   - Set `AURORA_IMAGE_TAG` to the exact image commit SHA.
   - Set `PUBLIC_DOMAIN` to the DNS name from step 6.
   - Set `FRONTEND_URL=https://<your-domain>` and matching YouTube callback and
     frontend-result URLs.
   - Set random independent values for PostgreSQL, Redis, JWT, rate-limit,
     integration-token, email-outbox and metrics secrets. Preserve encryption
     keyrings safely; losing an active key makes stored credentials unusable.
   - Set `MEDIA_S3_ENDPOINT_URL` to
     `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`,
     `MEDIA_S3_REGION` to the OCI region ID, `MEDIA_S3_BUCKET` to the private
     bucket, and the Customer Secret Key credentials from step 5.
   - Set the Wikimedia contact user agent, OpenAI-compatible LLM key/models,
     and ElevenLabs key, model, voice and reviewed rights metadata. Cloud workers
     cannot use the Ollama instance on your home machine through `localhost`.
   - Keep `YOUTUBE_PUBLISH_WORKER_ENABLED=false` until OAuth and the initial
     controlled-channel acceptance run are complete. Set research/story/media
     workers true only after their provider settings pass readiness checks.
5. Validate the resolved compose configuration without printing secrets:

   ```bash
   docker compose --env-file .env.production \
     -f docker-compose.prod.yml -f docker-compose.oci.yml \
     --profile youtube-publish config -q
   ```

6. Start the stack and inspect health:

   ```bash
   docker compose --env-file .env.production \
     -f docker-compose.prod.yml -f docker-compose.oci.yml \
     --profile youtube-publish up -d
   docker compose --env-file .env.production \
     -f docker-compose.prod.yml -f docker-compose.oci.yml ps
   ```

   Open `https://<your-domain>`. Confirm `/health/ready`, registration/login,
   readiness checks, live research, story generation, media rendering, review,
   channel connection and storage retrieval before enabling publish jobs.

## YouTube acceptance gate

In Google Cloud Console, configure an OAuth Web client with the exact redirect
URI `https://<your-domain>/api/v1/integrations/youtube/callback`, enable the
YouTube Data API, configure the consent screen and test users, and enter its
client ID/secret into the protected environment file. Connect a controlled test
channel and explicitly grant publishing consent in Aurora. First publish a
rights-cleared video as **private**; verify the resulting YouTube video ID,
description credits, captions, processing status and retry behavior. Only then
test scheduling and any public publishing. Google OAuth apps left in Testing
expire test-user authorizations after seven days when using scopes beyond basic
identity, so that mode is only for the short demo acceptance. Daily channel
automation requires an appropriate production consent-screen status and any
Google verification/compliance steps applicable to the app. YouTube upload
client requirements also require users to choose visibility (public, private or
unlisted); preserve Aurora's explicit privacy choice and channel-owner consent.
Never treat a green CI run as proof that Google OAuth or a real upload works.

## Recovery and limits

- PostgreSQL and Redis use named Docker volumes; the VM boot/block volume and
  Object Storage are still single-region resources. Configure and periodically
  test encrypted database backups before retaining important data.
- OCI Always Free Object Storage capacity is small; review consumption and
  retention regularly. Do not place local model weights or build caches on this
  VM. Qwen/Ollama and ComfyUI on the user's PC are not part of this cloud demo.
- The A1 VM is CPU-only for this stack and limited to 2 OCPUs. FFmpeg video
  renders will be slower than on a dedicated media worker. Only one demo
  production should run at a time; resource caps are in
  `docker-compose.oci.yml`.
- Set an OCI budget alert at zero/low spend and monitor billing, resource
  utilization, disk space, Object Storage usage, container health and worker
  logs. Free-tier resource reclamation and account/region capacity mean this is
  suitable for a showcase, not a dependable public production service.
- To update, back up PostgreSQL, change `AURORA_IMAGE_TAG` to the reviewed SHA,
  run Compose `up -d`, verify the migration and health checks, then smoke-test
  the demo. Keep the previous image SHA available for rollback.
