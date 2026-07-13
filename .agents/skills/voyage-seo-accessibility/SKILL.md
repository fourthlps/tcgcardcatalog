---
name: voyage-seo-accessibility
description: Validate and implement Voyage Log technical SEO and accessibility for affected public pages, including metadata, canonical URLs, Open Graph, structured data, semantic HTML, headings, alt text, keyboard access, focus visibility, contrast, and internal linking. Use for audits or approved page changes. Do not use to redesign the external SEO research or article drafting workflow, invent SEO facts, or promise rankings.
---

# Voyage SEO & Accessibility

Audit only verified routes and facts. Separate findings from authorized fixes.

## Technical SEO checks

- Inspect title, description, canonical URL, robots directives, language, Open Graph, Twitter metadata, favicon, and share image.
- Validate structured data against visible page facts. Do not invent author, date, rating, price, availability, or organization claims.
- Account for the GitHub Pages subpath and hash-router limitations. Report unsupported crawl assumptions explicitly.
- Check internal links, meaningful anchor text, orphan risk, duplicate metadata, broken routes, and redirect behavior.
- Preserve the approved external keyword research and article workflow.

## Accessibility checks

- Require semantic landmarks and one clear page `h1` with ordered headings.
- Require descriptive alt text for informative images and empty alt text for decorative images.
- Test keyboard navigation, focus order, focus visibility, dialogs/menus, escape behavior, and skip navigation where applicable.
- Check accessible names, labels, instructions, error messages, live updates, touch targets, zoom, reduced motion, and contrast.
- Ensure color, icon, or position is not the only carrier of meaning.

## Evidence and output

- Inspect source and rendered DOM; test affected interactions with `voyage-browser-qa`.
- Report the exact route, element, evidence, impact, and recommended fix.
- Label unsupported assumptions and validation gaps. Never promise ranking gains or accessibility compliance from an incomplete check.
