# YouTube Channel Authorization — Design

- Status: OAuth, explicit channel selection, incremental publishing consent, resumable upload worker and scheduling are implemented; live-channel acceptance remains outstanding
- Date: 2026-11-09
- Branch: `feature/youtube-oauth-integration`

## Goal

Connect a customer's YouTube channel to an Aurora Studio organization using Google's server-side OAuth 2.0 authorization-code flow. This is separate from Aurora Studio user authentication.

## First milestone scope

- Start an authorization flow for an organization by an authenticated, verified organization owner/admin.
- Generate a cryptographically random, single-use OAuth state value; persist only its SHA-256 hash, bound to the initiating user and organization, with a short expiry.
- Exchange the authorization code server-side and retrieve every authenticated channel using paginated YouTube Data API v3 `channels.list` with `mine=true`.
- Persist short-lived channel choices and an encrypted pending refresh token; require the initiating owner/admin to explicitly select a channel before creating/updating a connection. Never return tokens to the frontend or write them to logs.
- List connected channels only to authorized organization members.
- Disconnect a channel and revoke Google credentials.
- Cover state replay, expiry, cross-organization access, missing refresh token, provider failures, and encryption with tests.

## Authorization and scopes

The initial connection requests `youtube.readonly` for channel identity and `yt-analytics.readonly` for non-monetary analytics reports. It does not request upload access. When an organization owner/admin explicitly starts publishing consent (`POST /api/v1/organizations/{organization_id}/youtube/authorize?purpose=publish`), request those two scopes plus `youtube.upload`, the narrow upload permission accepted by `videos.insert`. Do not request the broader `youtube`, `youtube.force-ssl`, or partner scopes. OAuth state persists the exact requested scope set, and callback processing rejects an incomplete grant without replacing an existing channel credential. Older in-flight OAuth attempts are accepted, but existing connections must reauthorize to grant the Analytics scope; the Channels page exposes this as “Reconnect for analytics.” The channel-list response exposes `granted_scopes` and `can_publish`; analytics permission does not imply publishing permission. A tenant-scoped publishing intent and resumable upload worker exist; a real controlled-channel upload has not been acceptance-tested.

Enable the YouTube Analytics API in the Google Cloud project used by the OAuth client and add `https://www.googleapis.com/auth/yt-analytics.readonly` to the OAuth consent screen's data access/scopes. Google may require a consent-screen verification step before external users can grant the scope.

Use Google's web-server OAuth flow with:
- `response_type=code`
- `access_type=offline`
- `include_granted_scopes=true`
- exact configured redirect URI
- random state checked on callback
- `prompt=consent` is currently sent on each connection attempt to ensure Google issues a refresh token

## Security requirements

- OAuth client ID, client secret, and token-encryption key are server-side secrets.
- Persist a hash of OAuth state, never the raw state.
- State is short-lived, one-time-use, and bound to the authenticated user and organization.
- Initial connection does not persist any channel until the initiating owner/admin explicitly selects one from the pending choices. Pending refresh tokens are encrypted with attempt-specific authenticated context and expire with the selection.
- Refresh tokens are encrypted using AES-256-GCM with a dedicated integration-token key, independent of the email outbox key.
- Never store access tokens; obtain short-lived access tokens from the refresh token when API operations need them.
- Never expose token ciphertext or provider credentials in API responses.
- Enforce organization membership/role checks on every channel operation.
- Do not allow OAuth callback parameters to select or override the organization/user; derive them only from the persisted state record.
- Disconnect revokes the provider grant and removes local credential material.
- Callback responses must not render third-party assets or leak authorization codes through referrers; redirect to a clean frontend URL after processing.

## Data model

- OAuth attempt: user ID, organization ID, state hash, requested scope set, optional target channel ID for publishing re-consent, expiry, consumed timestamp, creation timestamp.
- Connected channel: organization ID, channel ID, channel title, custom URL/thumbnail when available, encrypted refresh token, encryption key version, granted scopes, connected-by user ID, timestamps, connection status.
- A channel ID is unique within an organization. The initial implementation permits the same YouTube channel to be connected to multiple organizations; product policy should confirm whether that is acceptable before production.

## API shape

- `POST /api/v1/organizations/{organization_id}/youtube/authorize?purpose=connect` — verified owner/admin starts read-only channel connection; returns requested scopes and authorization URL.
- `POST /api/v1/organizations/{organization_id}/youtube/authorize?purpose=publish&channel_record_id={channel_record_id}` — verified owner/admin explicitly re-consents for the selected active channel using read + upload scopes; the callback verifies the Google account resolves to that exact channel before updating its grant.
- Channel-list responses expose granted scopes and a derived `can_publish` capability flag; all future upload operations must enforce this server-side, not rely on the UI.
- Publishing consent is bound to the selected channel's database ID in the one-time OAuth attempt. A mismatched Google account or a channel disconnected while consent is pending is rejected without changing the existing connection.
- `GET /api/v1/integrations/youtube/callback` — public provider callback; validates and consumes persisted state; exchanges code; persists the selected channel; redirects to the frontend status route.
- `GET /api/v1/organizations/{organization_id}/youtube-channels` — authorized organization members list linked channels without secrets.
- `DELETE /api/v1/organizations/{organization_id}/youtube-channels/{channel_id}` — owner/admin disconnects and revokes channel.

## Operational prerequisites

- Enable YouTube Data API v3 in Google Cloud.
- Configure a Google OAuth client of type Web application.
- Register the exact backend callback URI as an authorized redirect URI.
- Configure OAuth consent screen and publishing status/test users as appropriate.
- Set `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REDIRECT_URI`, and a dedicated `INTEGRATION_TOKEN_ENCRYPTION_KEY` in the ignored local environment file.
- The local frontend should receive only a clean success/failure redirect, never provider tokens.

## References

- Google OAuth web-server flow: https://developers.google.com/identity/protocols/oauth2/web-server
- YouTube Data API authorization: https://developers.google.com/youtube/data/guides/authentication
- `channels.list` method: https://developers.google.com/youtube/data/reference/rest/v3/channels/list
- `videos.insert` method and accepted upload scopes: https://developers.google.com/youtube/data/reference/rest/v3/videos/insert
- Google OAuth scopes: https://developers.google.com/identity/protocols/oauth2/scopes
- Google OAuth policies: https://developers.google.com/identity/protocols/oauth2/policies


## Explicit multi-channel selection

The initial read-only connection fetches all pages of the authenticated account's channels and deduplicates channel IDs. It never selects the first provider result. OAuth callback stores candidate metadata and an encrypted, short-lived pending refresh token on the one-time attempt, then redirects with only the attempt and workspace IDs. An authenticated owner/admin who initiated that attempt can list the candidate channels and select one. The selection endpoint re-checks the actor's active verified owner/admin membership, locks the pending attempt, verifies that the selected channel was in the provider response, and re-encrypts the refresh token using the selected channel ID as AES-GCM associated data before creating/updating the connection. Pending data is cleared atomically after success. Empty results, malformed IDs, repeated IDs, pagination-token anomalies, expired attempts, cross-user/workspace access and replayed selections fail closed. Publishing re-consent remains bound to the previously selected database channel and rejects a different Google channel.


## Channel-scoped production and scheduling

Each connected YouTube channel has its own immutable editorial profile versions. A production run records the exact channel database ID, profile version ID, version number and a JSON snapshot. Later profile edits create a new version and cannot alter an in-flight run. Composite foreign keys pair both channel/profile IDs with the organization ID so a run cannot reference another tenant's channel or profile.

A daily schedule is stored separately per channel with an IANA timezone, local posting time, enabled/paused state, next UTC occurrence and catch-up policy. Enabling automatic scheduling requires the channel's separate YouTube upload scope; read-only OAuth remains sufficient for research and draft generation. Schedule configuration does not itself authorize upload: human approval and the exact-channel publishing grant remain mandatory.

The due-schedule dispatcher is implemented in the research worker poll loop. It claims due channels transactionally using row locks and `SKIP LOCKED`, derives the local calendar date in the configured timezone, applies the `skip` or `run_once` catch-up policy, and creates one channel-scoped research run with a deterministic organization/channel/local-date uniqueness boundary. It advances the next occurrence in the same transaction. When evidence persistence completes the research run, the same transaction creates the matching production run using the exact profile version and snapshot captured at dispatch. If the schedule is paused, the channel is disconnected, or publishing consent is revoked before research completes, the production handoff is suppressed. The dispatcher does not upload or publish; production approval and exact-channel publishing authorization remain separate gates.
