---
name: voyage-article-page-builder
description: Convert an explicitly approved Markdown article into a production-quality Voyage Log article page while preserving editorial meaning and the existing external SEO research and drafting workflow. Use only when approved article content and an integration target are provided. Do not use to research keywords, rewrite the editorial workflow, invent article facts, draft an unapproved article, or choose a new content architecture without approval.
---

# Voyage Article Page Builder

Build an article page from approved content without altering upstream research or editorial decisions.

## Preconditions

1. Confirm the Markdown is approved and identify its title, language, author/source attribution, date, images, and allowed internal links.
2. Run repository preflight and inspect the current article integration convention.
3. If no production article directory, template, route, or conversion command exists, stop and request the missing architecture decision. Do not invent one.

## Build rules

- Preserve claims, nuance, headings, citations, and editorial meaning. Correct only approved formatting or clear transcription defects.
- Use semantic article structure: one `h1`, ordered headings, readable paragraphs, lists, figures, captions, and meaningful link text.
- Add navigation, breadcrumbs, related content, and internal links only to verified repository routes.
- Reuse Voyage Log layout, tokens, header, footer, and responsive patterns.
- Use supplied or repository-approved assets. Require useful alt text; do not source copyrighted-looking official art without approval.
- Add metadata and structured data only from verified article facts. Apply `voyage-seo-accessibility`.
- Preserve GitHub Pages subpath and hash-route constraints if the chosen architecture uses them.

## Verification

- Compare rendered content against the approved Markdown section by section.
- Test mobile and desktop layout, links, images, keyboard access, headings, metadata, and console errors.
- Report every editorial or structural deviation. Do not publish or deploy without explicit authorization.
