// Generate per-game homepage showcase files (data/{game}/showcase.json).
// The homepage is a PLATFORM page: it composes these small files instead of
// loading full game datasets, preserving lazy-load isolation.
// Runs locally (cwd = onepiece-catalog folder) and on CI (cwd = repo root).
const fs = require("fs");
const path = require("path");

const DATA = fs.existsSync("onepiece-catalog/data") ? "onepiece-catalog/data" : "data";
const TODAY = new Date().toISOString().slice(0, 10);
const J = f => JSON.parse(fs.readFileSync(f, "utf8"));
const W = (f, o) => fs.writeFileSync(f, JSON.stringify(o));
const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
const base = n => (n || "").split("(")[0].trim();

// ═══ ONE PIECE ═══
(function () {
  const cards = J(`${DATA}/one-piece/cards.json`);
  const prices = J(`${DATA}/one-piece/prices.json`).markets || {};
  const sets = J(`${DATA}/one-piece/sets.json`).sets || [];
  const products = J(`${DATA}/one-piece/products.json`);
  const flat = [];
  for (const [code, arr] of Object.entries(cards)) arr.forEach(c => flat.push({ ...c, _set: code }));
  const priceOf = c => {
    const en = (prices[c.card_image_id] || []).find(m => m.source_market === "EN");
    if (en && en.source_price > 0) return en.source_price;
    const m = Number(c.market_price); return m > 0 ? m : 0;
  };
  // OP chase scoring (mirror of the site's name-pattern model)
  const score = c => {
    const n = (c.card_name || "").toLowerCase(), r = (c.rarity || "").toUpperCase();
    let s = 0;
    if (/manga/.test(n)) s = 100; else if (/ghost/.test(n)) s = 98; else if (r === "TR") s = 96;
    else if (/red\s*super\s*alt/.test(n)) s = 94; else if (/super\s*alt/.test(n)) s = 90;
    else if (/\(sp\)/.test(n) || r === "SP" || r === "SPR") s = 85; else if (/full\s*art/.test(n)) s = 78;
    else if (/alt(?:ernate)?\s*art/.test(n)) s = 75; else if (/box\s*topper/.test(n)) s = 68;
    else if (/parallel/.test(n)) s = 60; else if (r === "SEC") s = 55;
    return s;
  };
  const rec = c => ({ game: "one-piece", id: c.card_image_id, set: c._set, name: c.card_name, rarity: c.rarity, price_usd: +priceOf(c).toFixed(2), image: c.card_image, chase: score(c) });
  // de-dup exact repeat scrape rows by image id
  const seen = new Set();
  const uniq = flat.filter(c => { if (seen.has(c.card_image_id)) return false; seen.add(c.card_image_id); return true; });
  const chase = uniq.filter(c => score(c) > 0 && priceOf(c) >= 15)
    .sort((a, b) => (score(b) - score(a)) || (priceOf(b) - priceOf(a))).slice(0, 12).map(rec);
  const featured = uniq.filter(c => score(c) >= 85 && priceOf(c) > 0)
    .sort((a, b) => priceOf(b) - priceOf(a)).slice(0, 6).map(rec);
  // characters: curated featured_characters ∪ card counts
  const names = new Set(); sets.forEach(s => (s.featured_characters || []).forEach(n => names.add(base(n))));
  const chars = [...names].map(n => {
    const k = norm(n);
    const mine = uniq.filter(c => norm(base(c.card_name)).includes(k));
    return { game: "one-piece", name: n, cards: mine.length, sets: new Set(mine.map(c => c._set)).size };
  }).filter(c => c.cards > 0).sort((a, b) => b.cards - a.cards).slice(0, 10);
  // products: JP booster boxes with curated box prices, hot first
  const boxPrices = { "OP-05": 16500, "OP-09": 4500, "OP-13": 6500, "OP-01": 4800, "OP-12": 3400, "OP-16": 2950 };
  const prods = Object.keys(boxPrices).map(sc => {
    const p = products.find(x => x.set_code === sc && x.product_type === "Booster Box");
    if (!p) return null;
    const topCard = uniq.filter(c => c._set === sc).sort((a, b) => priceOf(b) - priceOf(a))[0];
    return { game: "one-piece", product_id: p.product_id, name: `${sc} ${p.set_name} Booster Box`, type: "Booster Box", set_code: sc, image: topCard ? topCard.card_image : "", price_thb: boxPrices[sc], price_usd: null };
  }).filter(Boolean).slice(0, 5);
  W(`${DATA}/one-piece/showcase.json`, { game: "one-piece", generated: TODAY, featured_pool: featured, top_chase: chase, characters: chars, products: prods });
  console.log("one-piece showcase: featured", featured.length, "chase", chase.length, "chars", chars.length, "products", prods.length);
})();

// ═══ LORCANA ═══
(function () {
  const cards = J(`${DATA}/lorcana/cards.json`);
  const prices = J(`${DATA}/lorcana/prices.json`).markets || {};
  const characters = J(`${DATA}/lorcana/characters.json`).characters || [];
  const products = J(`${DATA}/lorcana/products.json`).products || [];
  const TIER = { IC: 100, EN: 90, EP: 80, LG: 60 };
  const flat = [];
  for (const [code, arr] of Object.entries(cards)) arr.forEach(c => flat.push(c));
  const priceOf = c => {
    const recs = (prices[c.card_image_id] || []).filter(m => m.source_market === "EN");
    if (!recs.length) return 0;
    const order = { normal: 0, "cold-foil": 1, holofoil: 2 };
    recs.sort((a, b) => (order[a.finish] ?? 3) - (order[b.finish] ?? 3));
    return recs[0].source_price || 0;
  };
  const maxPrice = c => Math.max(0, ...(prices[c.card_image_id] || []).map(m => m.source_price || 0));
  const rec = c => ({ game: "lorcana", id: c.card_image_id, set: c.set_id, name: c.card_name, rarity: c.rarity, rarity_name: c.rarity_name, price_usd: +maxPrice(c).toFixed(2), image: c.card_image, chase: TIER[c.rarity] || 0 });
  const chase = flat.filter(c => TIER[c.rarity] && maxPrice(c) >= 15)
    .sort((a, b) => (TIER[b.rarity] - TIER[a.rarity]) || (maxPrice(b) - maxPrice(a))).slice(0, 12).map(rec);
  const featured = flat.filter(c => (c.rarity === "IC" || c.rarity === "EN") && maxPrice(c) > 100)
    .sort((a, b) => maxPrice(b) - maxPrice(a)).slice(0, 6).map(rec);
  const chars = characters.slice().sort((a, b) => b.card_count - a.card_count).slice(0, 10)
    .map(ch => ({ game: "lorcana", name: ch.name, cards: ch.card_count, sets: (ch.sets || []).length, franchise: (ch.franchise || [])[0] || "" }));
  // products: newest box, best-value box, trove near MSRP, starter deck, gift set
  const boxes = products.filter(p => p.product_type === "Booster Box" && p.market_price_usd > 0);
  const bestBox = boxes.slice().sort((a, b) => a.market_price_usd - b.market_price_usd)[0];
  const newBox = boxes.find(p => p.set_code === "LOR-12") || boxes[boxes.length - 1];
  const trove = products.filter(p => p.product_type === "Illumineer's Trove" && p.market_price_usd > 0 && p.market_price_usd < 70).sort((a, b) => a.market_price_usd - b.market_price_usd)[0];
  const starter = products.find(p => p.product_type === "Starter Deck" && p.set_code === "LOR-10") || products.find(p => p.product_type === "Starter Deck");
  const gift = products.filter(p => p.product_type === "Gift Set" && p.market_price_usd > 0).sort((a, b) => b.release_date.localeCompare(a.release_date))[0];
  const prods = [newBox, bestBox, trove, starter, gift].filter(Boolean).filter((p, i, a) => a.findIndex(x => x.product_id === p.product_id) === i)
    .map(p => ({ game: "lorcana", product_id: p.product_id, name: p.product_name, type: p.product_type, set_code: p.set_code, image: p.product_image || "", price_thb: null, price_usd: p.market_price_usd || p.msrp_usd || null }));
  W(`${DATA}/lorcana/showcase.json`, { game: "lorcana", generated: TODAY, featured_pool: featured, top_chase: chase, characters: chars, products: prods });
  console.log("lorcana showcase: featured", featured.length, "chase", chase.length, "chars", chars.length, "products", prods.length);
})();
