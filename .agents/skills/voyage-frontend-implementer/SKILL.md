---
name: voyage-frontend-implementer
description: Implement approved Voyage Log frontend changes in the existing vanilla HTML, CSS, and JavaScript architecture. Use for scoped changes to the public SPA or admin UI, including components, routing, rendering, responsive behavior, and data consumption. Do not use for read-only reviews, article-only work, pipeline rewrites, production data mutations, framework migrations, or deployments.
---

# Voyage Frontend Implementer

Implement only an approved, bounded frontend change and preserve working production behavior.

## Workflow

1. Apply `voyage-repo-guardian` preflight and confirm implementation is authorized.
2. Inspect the exact rendering function, CSS rules, state, route, config, and data contract involved.
3. Reuse existing helpers, components, tokens, and patterns before adding new ones.
4. Implement the smallest coherent change with `apply_patch`.
5. Verify affected routes and neighboring regressions with `voyage-browser-qa`.

## Implementation rules

- Keep the frontend vanilla HTML/CSS/JavaScript. Do not introduce a framework, bundler, or dependency without approval.
- Preserve hash routing and relative paths under the GitHub Pages subdirectory.
- Keep shared UI game-agnostic. Express game differences through `config/games.json` and existing config-driven renderers.
- If changing canonical game config, inspect and synchronize the embedded `DEFAULT_CONFIG` fallback where required.
- Resolve prices through current helpers and policies; never mutate scraped price JSON from UI code.
- Keep Supabase failures non-destructive and preserve documented fallbacks. Never expose privileged credentials.
- Preserve bilingual behavior where the affected surface supports Thai and English.
- Make loading, missing, error, and disabled states honest and visible.
- Prefer mobile-first CSS and avoid isolated inline styles when a reusable pattern exists.

## Verification

- Exercise the changed route at representative mobile and desktop widths.
- Check keyboard operation, focus, overflow, console errors, and broken resources.
- Confirm another active game still renders correctly when shared code changes.
- Report files changed, behavior proven, limitations, and any untested path. Do not claim deployment.
