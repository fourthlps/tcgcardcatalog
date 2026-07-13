---
name: voyage-design-system
description: Maintain Voyage Log's established premium, trustworthy, light lavender/purple visual identity across typography, spacing, responsive behavior, and reusable UI patterns. Use when reviewing or implementing visual components, layouts, states, or design consistency. Do not use as authorization to change product behavior, data contracts, pipelines, editorial meaning, or deployment configuration.
---

# Voyage Design System

Keep visual changes cohesive, mobile-first, and recognizably Voyage Log.

## Process

1. Inspect current CSS variables, component patterns, artwork, typography, spacing, and states before proposing new tokens.
2. Capture the current mobile and desktop appearance of the affected surface.
3. Identify which existing pattern should be reused or extended.
4. Define hierarchy, state behavior, responsive rules, and accessibility requirements before styling.
5. Implement through `voyage-frontend-implementer` only when authorized.

## Visual rules

- Preserve the premium, trustworthy, light lavender/purple direction and nautical Voyage Log identity.
- Use restraint: emphasize one primary action or focal element per section.
- Prefer reusable classes and tokens over one-off inline styling.
- Keep typography hierarchy, spacing rhythm, radii, borders, shadows, and icon style consistent with nearby approved UI.
- Design mobile-first, then expand deliberately for desktop. Do not hide required functionality merely to fit a small screen.
- Provide complete default, hover, focus, active, loading, empty, error, disabled, and reduced-motion states when relevant.
- Avoid generic dashboard cards, excessive gradients, emoji-only controls, decorative clutter, and copied marketplace aesthetics.
- Use only approved repository or user-supplied assets; preserve image framing and legibility.

## Evidence

- Compare before and after at representative mobile and desktop widths.
- Check visual hierarchy, wrapping, overflow, touch targets, focus visibility, contrast, and bilingual text expansion.
- Explain reused patterns and any deliberate new token. Do not claim visual improvement without rendered evidence.
