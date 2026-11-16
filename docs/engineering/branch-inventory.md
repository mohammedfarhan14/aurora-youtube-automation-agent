# Live branch inventory — 14 November 2026

## Snapshot

- Source: GitHub branches API, checked 14 November 2026 UTC.
- Live remote branches after cleanup: **14**. The previous 13 November snapshot listed 143; the 24-branch snapshot was reconciled and 10 merged refs have since been archived and deleted. The `develop` SHA matches the Node 24 pin commit `0b2f918`.
- Current classifications: A (fully merged): **0**; B (merged PR refs): **0**; C (unique open PR work): **0**; D (uncertain/closed without merge): **11**; E (protected): **3**.
- The only open PR is draft #145. Its branch is kept in protected class E while its Backend CI failures are unresolved.
- GitHub reports `protected=false` for all 24 branches; actual required branch protection remains outstanding in issue #38.

## Live branch summary

The authoritative per-branch SHA, PR status, observed CI and disposition are in [branch-comparison.csv](branch-comparison.csv).

| Class | Branches | Disposition |
| --- | --- | --- |
| D | `docs/reconcile-research-intelligence-after-pr114`, `feat/aur-11-word-level-captions`, `feat/aur-12-youtube-ai-disclosure`, `feat/ffmpeg-video-renderer`, `feat/kokoro-streaming-wav-compatibility`, `feat/story-review-workspace`, `feat/wikimedia-commons-visual-provider`, `feat/youtube-access-token-refresh`, `feat/youtube-durable-publisher-worker`, `feat/youtube-resumable-upload-adapter`, `fix/ci-self-hosted-pnpm-cache` | Preserve until the work is reviewed; do not infer that closed means merged. |
| E | `develop`, `feat/creative-identity-registry`, `main` | Preserve. `feat/creative-identity-registry` is the open draft PR #145 branch. |

## Cleanup policy

The cleanup script is dry-run by default. It verifies every recorded SHA against live GitHub refs and archives selected branches as remote annotated tags before using an expected-SHA lease to delete them. Classes D and E are never automatic candidates. Class B requires the explicit `--include-superseded` option. Do not run cleanup until the checkout is clean and `develop` matches `origin/develop`.

Ten merged branch refs were archived as annotated remote tags under `archive/branch-cleanup/20261114T055645Z/` and deleted with expected-SHA leases. The 11 uncertain refs, `main`, `develop` and draft PR #145 remain untouched. The archive tags preserve recovery points for every deleted branch.
