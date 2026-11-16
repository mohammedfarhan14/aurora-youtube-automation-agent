# Proposed branch protection — not applied

Status: proposal only. No repository settings have been changed.

## `develop` — integration branch

- Require pull requests before merging.
- Require Backend CI and Frontend CI to pass on the latest commit.
- Require branch to be up to date before merge, subject to CI capacity.
- Require conversation resolution.
- Require linear history / squash merge.
- Block force pushes and branch deletion.
- Do not require an approving review while the repository is maintained by a solo developer; instead require the owner's explicit PR self-review checklist and passing required checks.
- Permit only repository maintainers to bypass, with bypass events audited.

## `main` — release branch

- Require pull requests before merging.
- Require Backend CI and Frontend CI to pass.
- Require successful staging acceptance and a release checklist.
- Require linear history / squash merge.
- Block force pushes and branch deletion.
- No direct pushes.
- Keep release/deployment approval separate from code merge.

## Recovery and rollout

1. Verify the exact current branch rules and available status-check names.
2. Apply rules to `develop` first and validate a test PR.
3. Apply the stricter release rules to `main` only after the develop rules are confirmed.
4. Document an owner-controlled emergency bypass with an audit trail.
5. Keep this proposal unapplied until repository owner settings access is explicitly available.

## Rationale

The repository has one developer and no second reviewer. Required automated checks, immutable shared history, and an explicit self-review/release checklist provide a practical baseline without pretending a second human approval exists.
