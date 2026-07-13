---
name: voyage-repo-guardian
description: Protect Voyage Log repository architecture, data, secrets, deployment configuration, and working production behavior. Use before planning or editing repository files, when auditing architecture or Git state, or when a requested change could affect shared config, pipelines, generated data, authentication, workflows, or production. Do not use as the primary skill for isolated copywriting outside the repository or as authorization to implement a read-only review.
---

# Voyage Repo Guardian

Protect the repository before any implementation begins. Keep analysis read-only unless the user explicitly requests a change.

## Preflight

1. Read repository-root `AGENTS.md` completely.
2. Run `git status --short --branch`; identify pre-existing changes and do not overwrite them.
3. Inspect the requested area and its consumers. Include entry points, `config/games.json`, relevant `data/` files, scripts, and workflows when they can be affected.
4. Read `onepiece-catalog/docs/multi-game-architecture.md`, then confirm its claims against current code.
5. State the requested scope, allowed mutations, excluded areas, and required evidence.

## Guard the change boundary

- Prefer the smallest safe diff.
- Preserve `onepiece-catalog/index.html`, `admin.html`, hash routes, GitHub Pages subpaths, and the config-driven game model unless the approved change specifically requires them.
- Treat generated and scraped prices as read-only. Route manual corrections through `price_overrides`.
- Never inspect secret values or modify authentication, Supabase production state, workflow permissions, schedules, or deployment settings without exact authorization.
- Do not invent build, test, article, or deployment commands. Report when the repository does not define one.
- Stop and report if required work conflicts with unrelated local changes, current production behavior, or missing authority.

## Evidence

- Cite the files and commands inspected.
- Distinguish confirmed facts, inferences, and unresolved assumptions.
- For a review, report findings without editing.
- For implementation, hand off a bounded file list and verification plan to the focused skill.
