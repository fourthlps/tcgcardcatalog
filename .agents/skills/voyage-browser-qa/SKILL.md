---
name: voyage-browser-qa
description: Perform evidence-based browser QA for Voyage Log using the available browser or Playwright surface. Use to reproduce bugs, verify approved changes, test affected routes at mobile and desktop widths, and assess navigation, interactions, layout, console errors, resources, overflow, accessibility basics, and regression risks. Do not use to implement fixes or mutate production data.
---

# Voyage Browser QA

Test behavior as a user and produce reproducible evidence. Keep production testing read-only.

## Test setup

1. Confirm the requested routes, expected behavior, environment, and change scope.
2. For local testing, use an HTTP server from `onepiece-catalog/`; do not open the SPA with `file://`.
3. Record the tested revision or working-tree state and viewport.
4. Prefer stable DOM locators and inspect visible state before interacting.

## Coverage

- Test at least one representative mobile width around 390-412px and one desktop width around 1280-1440px when layout is affected.
- Exercise entry, navigation, Back/Forward hash routing, search, filters, language/edition controls, cards, empty/error states, and affected admin behavior as relevant.
- Check wrapping, clipping, horizontal overflow, sticky elements, overlays, touch targets, keyboard focus, and reduced-motion behavior.
- Inspect console errors, failed resources, missing images, and unexpected network fallbacks.
- When shared code changes, sample both One Piece and Lorcana.
- Take screenshots at the failure and after a verified fix when visual evidence matters.

## Severity

- `P0`: data loss, secret exposure, production outage, or unusable critical path.
- `P1`: major user path broken, materially wrong price/data, or broad mobile failure.
- `P2`: important defect with a workaround or limited scope.
- `P3`: polish, minor inconsistency, or low-impact edge case.

## Report format

For each finding, include severity, route, viewport, precondition, exact steps, expected result, actual result, and evidence. Separate confirmed defects from observations. Retest the same path before marking a fix verified.
