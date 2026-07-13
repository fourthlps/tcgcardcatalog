---
name: voyage-release-safety
description: Review Voyage Log changes for release readiness and verify an explicitly authorized deployment. Use before committing, pushing, running write-capable workflows, deploying, or declaring production success, and after deployment for live verification and rollback readiness. Do not use as permission to release, modify secrets, or bypass missing tests and approvals.
---

# Voyage Release Safety

Block unsafe releases and require live proof after an authorized deployment.

## Pre-release gate

1. Confirm explicit authority for the exact commit, push, workflow run, or deployment action.
2. Run `git status --short --branch` and review the complete diff and changed-file scope.
3. Verify no secrets, credentials, personal data, generated clutter, or unrelated changes are included.
4. Confirm relevant functional checks, browser QA, SEO/accessibility checks, and performance evidence are complete.
5. Inspect effects on config, generated data, price pipelines, Supabase fallback, GitHub Pages paths, workflows, and both active games.
6. Define rollback instructions before releasing. Use real repository history and files; do not invent a rollback command.

## Release rules

- Never commit, push, deploy, or trigger a write-capable workflow without explicit user authorization.
- Keep commits intentional and limited to reviewed files.
- Do not modify secrets or permissions as an incidental release step.
- Stop on an unexpected base revision, dirty overlap, failing check, missing evidence, or scope expansion.

## Production verification

- Record the released commit SHA and expected changed files.
- Wait for GitHub Pages propagation when needed, then verify the live URL rather than only repository contents.
- Test the changed path at mobile and desktop widths, check console and resources, and sample critical neighboring behavior.
- Confirm pipeline and security containment remain intact when relevant.
- Report verified, failed, and untested items separately. Never claim deployment success before the live result matches the reviewed revision.
