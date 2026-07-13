---
name: voyage-performance-check
description: Assess Voyage Log performance risks and verify optimizations with before-and-after evidence. Use when a change affects images, layout shifts, scripts, large JSON data, data loading, rendering, caching, or page responsiveness. Do not use for purely visual preference reviews, speculative rewrites, or claims that lack a comparable baseline.
---

# Voyage Performance Check

Measure first, change second, and compare like-for-like.

## Baseline

1. Record revision, route, viewport, network/cache conditions, and test method.
2. Inspect transferred resources, script execution, render timing, image dimensions/loading, and visible layout shifts.
3. Identify the specific bottleneck and affected user action. Do not infer speed from file size alone.

## Review focus

- Check initial and lazy-loaded JSON by game; preserve the config-driven loading boundary.
- Check duplicate requests, full-dataset loading, repeated parsing/rendering, and unnecessary shared-game work.
- Check image format, intrinsic dimensions, responsive sizing, lazy loading, decoding, and broken-image fallback.
- Check render-blocking resources, third-party scripts, long tasks, repeated DOM work, animation cost, and event-listener duplication.
- Check layout stability for cards, hero media, fonts, sticky navigation, and async data.
- Avoid new production dependencies unless explicitly approved.

## Optimization evidence

- Make one scoped change per demonstrated cause when possible.
- Repeat the same measurement conditions after the change.
- Report before, after, delta, variability, and any tradeoff.
- Test mobile behavior and affected interactions for regressions.
- Describe unmeasured improvements as hypotheses, not completed optimizations.
