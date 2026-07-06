# Voyage Log — Multi-Game Architecture

> Version 1.1 · 2026-07-06 · Refactor by FIFTH (approved scope: config-driven unlimited-TCG support)
> v1.1 additions: per-game homepage showcase files (`data/{game}/showcase.json`, regenerated daily
> by lorcana-market.yml via `scripts/gen_showcase.js`) — the homepage is a platform page composed
> from showcases, never from full game datasets. Platform meta/OG tags + cross-game search chips
> + platform-wide stats added after CEOFOURTH post-implementation review.
> Rule of the architecture: **shared UI never branches on a game slug.** All game behavior comes from `config/games.json`. Game-specific `if` statements are forbidden in shared components; only data (config) decides.

---

## 1. Folder structure

```
onepiece-catalog/                  (repo web root)
├── index.html                     shared app shell — game-agnostic
├── config/
│   └── games.json                 ← single source of game configuration
├── data/
│   ├── one-piece/                 cards.json · prices.json · sets.json ·
│   │                              products.json · characters.json · market.json
│   └── lorcana/                   cards.json · prices.json · sets.json ·
│                                  products.json · characters.json
├── images/
│   ├── one-piece/                 reserved for future local images (OP01/…)
│   └── lorcana/                   reserved for future local images (LOR01/…)
├── search/
│   ├── one-piece-index.json       lightweight per-game search index
│   └── lorcana-index.json
├── data/*.json                    LEGACY: One Piece trend-pipeline outputs
│                                  (top-gainers, status, …) — paths are read
│                                  from one-piece config, migrate with pipeline
├── docs/multi-game-architecture.md (this file)
└── scripts/update_lorcana_prices.py (future daily job — not yet scheduled)
```

No shared cards/products/prices/characters files exist. Each game owns its folder completely.

## 2. Data loading flow

```
init()
 ├─ loadConfig()            fetch config/games.json (embedded DEFAULT_CONFIG fallback)
 ├─ loadGame(home_game)     one Promise.all over that game's data_paths ONLY
 ├─ renderHome()
 └─ lazy triggers for other games:
      · openGame(slug)      game tile / nav click
      · _bootGamesNeeded()  saved collection/wishlist/recent ids referencing the game
      · searchAllGames()    explicit global search
```
Opening One Piece downloads zero Lorcana bytes and vice versa (verified via the browser resource log). `loadGame` caches per slug (`_promise` guard) — a game is fetched at most once per session.

## 3. Game config format (`config/games.json`)

Per game: `slug · display_name · short_name · tile_name · theme_color · icon · status(active|coming_soon) · id_prefixes · data_paths · image_mode + image_base_path · supported_fields · card_detail_layout (stats[] loop-rendered; boolean fields; meta_line; flavor_key) · rarity_list + rarity_labels · filter_groups (categories, set_categories, show_language_filter) · price_policy · product_types · character_grouping + character_match + characters_from_file + character_hub_include_unpriced · search_fields · collection_fields · compare_fields · set_overview_fields · coming_soon_source(+coming_soon) · box_prices · set_category_map · list_note`.

`index.html` embeds `DEFAULT_CONFIG` (boot resilience). **Keep it in sync with config/games.json** — the JSON file wins when it loads.

## 4. State model

```js
D.games[slug] = { setIndex, cardsBySet, sets, products, prices, pricesMeta,
                  characters, setData, marketData, loaded, _promise }   // ISOLATED
D.cache / D.products / D.setData / D.marketData / D.index                // READ-ONLY
                  // views rebuilt wholesale by _rebuildViews() — needed for
                  // cross-game surfaces (collection, jumpCard, global search)
S.game            // active game slug; gd() = D.games[S.game]; cfgActive() = its config
```
Loaders write only into `D.games[slug]`; no fetch handler can overwrite another game's arrays (the async-race class of bugs is structurally impossible now).

## 5. Price policy model

One choke point: `marketFor(card, lang)` — resolves the card's game from its id, applies the config policy:
- `type:"edition"` (One Piece): JP/EN market records, JP→THB labeled reference, edition badges/toggles active.
- `type:"finish"` (Lorcana): finish-labeled records; headline price follows `fallback_order` (Normal → Cold Foil → Holofoil); one fixed `edition_badge`; JP/EN toggles hidden.
Every surface (tile, detail, related, collection, wishlist, compare, search, products) resolves through `marketFor`/`realPrice`. Missing price ⇒ **"Price unavailable / ไม่มีข้อมูลราคา"** — never ฿0/$0.

## 6. Filter model

- Rarity chips: generated from rarities present in the current card list, ordered by config `rarity_list`.
- Category chips: the global `CATEGORY_DEFS` test library filtered by config `filter_groups.categories` (a game only exposes the categories it lists).
- Set-category chips + JP/EN language filter: config `filter_groups.set_categories` / `show_language_filter`.
- Set page rarity distribution: config `rarity_list` + `rarity_labels` (zero-count tiers auto-hidden).

## 7. Character model

- `character_grouping`: `"character"` (OP) or `"character_and_franchise"` (Lorcana → related characters resolved by shared franchise from the game's characters.json).
- `character_match`: `"name_base"` (parse card names) or `"character_name"` (structured field).
- `characters_from_file`: merge the game's characters.json into the shared character map (entries carry their game slug; the homepage strip shows only `home_game` characters).
- `character_hub_include_unpriced`: keep pre-release cards visible in hubs (Lorcana LOR-13).

## 8. Product model

Per-game `products.json` (`{schema_version, game, products:[]}` or bare array). Shared renderer; `product_types` in config document the game's line-up. Product prices resolve data-first: `market_price_usd` → `msrp_usd` → config `box_prices` → verified `market.json` records. Buying guides render Third's editorial when present, otherwise the "being prepared" placeholder. Affiliate stays disabled (`affiliate_url: null`).

## 9. Search index model

- Runtime search is game-scoped: active game first; **"🌐 Search all games"** action lazy-loads the rest, then re-runs globally. Products/characters/sets are scope-filtered the same way. Exact card-code jump is always cross-game (ids are prefix-unique).
- `search/{game}-index.json` are lightweight build artifacts (`id, n(name), s(set), r(rarity), t(type)[, ch, fr]`) prepared for the future server/edge search; the SPA does not fetch them yet.

## 10. GitHub Actions model

Independent per-game pipelines — one game's failure can never fail another:
- `.github/workflows/one-piece-market.yml` — the EXISTING daily workflow (currently named update-prices.yml; rename at deploy). Runs `update_prices.py`, which now writes **both** `one_piece_OP01-OP16_with_prices.json` (legacy) and `data/one-piece/cards.json` (new). Trend outputs stay in `data/` until the pipeline migrates them.
- `.github/workflows/lorcana-market.yml` — **NOT YET ENABLED** (per instruction). When approved it runs `scripts/update_lorcana_prices.py` daily: TCGCSV (category 71) → join by `tcgplayer_id` → rewrites `data/lorcana/prices.json` + refreshes product market prices. Abort-on-empty guard included; per-group failures skip, never wipe.

## 11. How to add a new game (Pokémon test)

1. Create `data/pokemon/` with `cards.json` (`{"SET-CODE":[cards…]}`, ids `PKM01-001`-style), `prices.json` (card-markets shape), `sets.json`, `products.json`, `characters.json`.
2. Add a `pokemon` entry in `config/games.json`: set `status:"active"`, `id_prefixes:["PKM"]`, data_paths, rarity list/labels, price_policy, card_detail_layout, filter groups.
3. Optional: `images/pokemon/`, `search/pokemon-index.json`, market workflow.
4. Test with the Requirement-15 checklist.
5. **Zero core code changes.** (Only optional garnish lives in code: a tile SVG in `renderGames`' `GAME_SVG` map and Thai search aliases.)

## 12. How to rollback

Nothing is deployed yet. After a future deploy: restore `index.pre-lorcana-backup.html` as `onepiece-catalog/index.html` via one GitHub-API PUT — it reads only the legacy root files, which the pipeline still refreshes (dual-write). The `config/ data/ search/ images/ docs/` folders are inert for the old build and can be removed at leisure.

## 13. Known limitations

1. `D.cache`/`D.products`/`D.setData` merged views exist for cross-game surfaces (collection, jumpCard, global search). They are rebuilt-only views, never write targets — isolation holds — but a purist would query `D.games` directly everywhere.
2. `DEFAULT_CONFIG` duplicates `config/games.json` (boot resilience). Sync manually when editing config.
3. Homepage widgets (stats, featured card, chase, trends, box rankings, character strip) are `home_game`-scoped by product decision — a multi-game homepage is a future product design task, not an architecture gap.
4. Game tile SVG art and Thai search aliases live in code (presentation assets), keyed by slug.
5. OP trend files remain at legacy `data/*.json` until the pipeline migrates (paths are config-read already).
6. Set-tile fallback icons come from the shared category icon map (booster ⚓ etc.), not per-game config.
7. Search indexes in `/search` are generated artifacts — regenerate when card data changes (see scratchpad `migrate.js` §search).

<!-- build-refresh 2026-07-06T20:08:56.4509531+07:00 -->
