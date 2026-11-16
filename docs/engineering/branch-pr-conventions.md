# Branch and pull-request conventions

## Branch naming

Use short, lowercase, slash-separated names:
- `feat/<capability>` for one user-visible capability.
- `fix/<defect>` for one defect.
- `test/<scope>` for test-only work.
- `docs/<subject>` for documentation.
- `chore/<subject>` for repository maintenance.
- `security/<subject>` for security controls.

Avoid stacking multiple unrelated changes on one branch. Branch from the current `develop` head unless a documented dependency requires otherwise.

## Pull requests

- One concern and one acceptance objective per PR.
- Include issue linkage, design/behavior summary, migration impact, test evidence, rollback/recovery notes, and explicit operator/configuration changes.
- Prefer squash merge to keep `develop` history readable.
- Require backend and frontend checks relevant to the changed surface.
- Never commit credentials, access tokens, production data, or local environment files.
- Do not merge a PR with failing or missing required checks.

## Branch lifecycle

- Keep `main` frozen except for explicitly approved releases.
- Integrate feature work through PRs into `develop`.
- After a PR is merged and the branch is verified as fully integrated, delete the head branch.
- Do not delete branches with open PRs, unreviewed unique work, or unresolved experimental status.
- Before manual cleanup of legacy branches, create and verify an annotated `archive/<branch-name>` tag pointing to the exact branch head, and record the tag and reason in the branch inventory.
- Never force-push or rewrite shared history.
- Enable GitHub's automatic head-branch deletion after merge where repository settings permit; verify the setting rather than assuming it is enabled.
