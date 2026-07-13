# Voyage Log Repository Instructions

## Source of truth

- Treat live production behavior and the current repository as authoritative.
- Use `onepiece-catalog/docs/multi-game-architecture.md` for architecture intent, but verify every statement against current code before acting.
- Treat historical notes and `onepiece-catalog/README.md` as context only when they conflict with working production code.
- Report contradictions instead of silently choosing an older document.

## Required preflight

Before any repository edit:

1. Run `git status --short --branch` and preserve unrelated user changes.
2. Read this file and the skill that matches the task.
3. Inspect the affected entry point, config, data path, workflow, and nearby implementation patterns.
4. Separate read-only analysis from authorized implementation. Do not edit merely because a review found an issue.
5. Define the smallest safe file scope and the evidence needed to prove the result.

## Confirmed repository shape

- `onepiece-catalog/index.html` is the public vanilla HTML/CSS/JavaScript SPA and uses hash routes.
- `onepiece-catalog/admin.html` is the Supabase-backed admin entry point.
- `onepiece-catalog/config/games.json` is the canonical multi-game config. `index.html` contains a boot fallback named `DEFAULT_CONFIG`; keep it synchronized only when config changes require it.
- Game-owned data lives under `onepiece-catalog/data/{game}/`. One Piece and Lorcana are active.
- Search artifacts live under `onepiece-catalog/search/`.
- Root and `onepiece-catalog/data/` JSON files include legacy and pipeline outputs; inspect consumers before changing or moving them.
- `.github/workflows/update-prices.yml` and `.github/workflows/lorcana-market.yml` update price data independently and can commit generated data.
- No frontend framework, package manifest, build command, automated test suite, article directory, or production article conversion command is currently present in the repository.

## Non-negotiable safety rules

- Preserve the config-driven multi-game architecture. Do not add shared-UI branches for individual game slugs when config can express the behavior.
- Preserve GitHub Pages subpath compatibility and hash-route behavior. Avoid root-absolute asset or navigation paths unless current production already requires them.
- Keep scraped pipeline prices read-only. Apply manual corrections through `price_overrides`; do not hand-edit generated price truth.
- Never expose, print, request, or commit secrets. Do not modify `.env`, authentication configuration, Supabase production data, GitHub Actions secrets, workflow permissions, schedules, or deployment settings without explicit approval for that exact action.
- Do not introduce a frontend framework or production dependency without explicit approval.
- Do not delete, rewrite, or reformat unrelated files. Do not run destructive Git commands.
- Prefer existing components, helpers, tokens, and data contracts over one-off replacements.
- Do not use unapproved copyrighted-looking official artwork. Reuse only assets already approved in the repository or assets the user supplies for the task.
- Make broken, misleading, empty-without-explanation, or silently degraded states explicit.

## Product and content rules

- Design mobile-first for Voyage Log's primary audience.
- Preserve the premium, trustworthy, light lavender/purple visual direction; avoid generic dashboard or template styling.
- Treat the existing SEO research and article drafting workflow as complete and external to this repository. Do not redesign it.
- Convert only approved Markdown articles. Because no production article path or converter is currently present, discover the approved integration target before creating an article page; stop and report the missing decision if none exists.
- Preserve editorial meaning and distinguish verified facts from assumptions.

## Verification and release

- Use the repository's documented local preview only after confirming it still applies: run a local HTTP server from `onepiece-catalog/` rather than opening `index.html` with `file://`.
- Test affected behavior at representative mobile and desktop widths, including navigation, interactions, overflow, broken resources, and console errors.
- Require evidence before claiming a fix, optimization, release, or deployment succeeded.
- Do not commit, push, deploy, run a write-capable workflow, or mutate production unless the user explicitly authorizes that action.
- After an authorized deployment, verify the live GitHub Pages result; repository state alone is not deployment proof.

## Skill routing

- Use `voyage-repo-guardian` for architecture and change-boundary preflight.
- Use `voyage-frontend-implementer` for approved HTML/CSS/JavaScript implementation.
- Use `voyage-article-page-builder` for approved Markdown-to-production article work.
- Use `voyage-design-system` for visual consistency and reusable UI decisions.
- Use `voyage-seo-accessibility` for metadata, semantic HTML, SEO, and accessibility checks.
- Use `voyage-browser-qa` for browser regression testing and P0-P3 evidence.
- Use `voyage-performance-check` for measured performance review.
- Use `voyage-release-safety` before and after an authorized release.
