# The Stone of Osgiliath — Technical Handoff Document

## Project Overview

**Name:** "The Stone of Osgiliath" — Real-time price monitoring, deal tracking, and collection management for trading card games and collectibles.

**Repository:** https://github.com/wsnich/stone-of-osgiliath

**Architecture:** Single-process async Python (FastAPI/Uvicorn) + SQLite + Patchright/Playwright browser automation. Web UI is vanilla JavaScript (~7000 lines HTML) with real-time WebSocket updates. No build step, no framework.

**Total codebase:** ~8,400 lines Python + ~7,000 lines HTML/CSS/JS

---

## File Structure & Line Counts

```
stone-of-osgiliath/
  main.py                    (52 lines)   — Entry point, CLI args, uvicorn launcher
  db.py                      (659 lines)  — SQLite schema, migrations, CRUD functions
  config.example.json        (53 lines)   — Template config with placeholders
  requirements.txt                        — Python dependencies
  setup.bat / start.bat                   — Windows one-click setup/launch scripts
  README.md / CONTRIBUTING.md / LICENSE
  HANDOFF.md                              — This document

  web/
    app.py                   (2,183 lines) — FastAPI app, 62+ API routes, 4 background loops
    state.py                 (686 lines)   — Dataclasses, shared state, deal tracker, product hub
    index.html               (6,985 lines) — Entire frontend (HTML + CSS + JS)

  monitors/
    tcgplayer_monitor.py     (636 lines)   — TCGPlayer browser scraper
    ebay_monitor.py          (479 lines)   — eBay/130point sold listing scraper
    discord_gateway.py       (~350 lines)  — Discord browser-based DOM scraper (one tab per channel)
    discord_monitor.py       (~230 lines)  — Discord message filtering + DM sender
    marketplace_monitor.py   (170 lines)   — BST channel parser + product matcher
    defaults.py              (38 lines)    — Configurable defaults (UA, timeouts, browser)

  research/
    agent.py                                — AI research agent (Anthropic SDK tool-use loop)
    tools.py                                — Agent tool definitions (query_db, read_codebase, etc.)
    queries.py                              — Read-only DB access + scoped writes to research_findings
    prompts/system.md                       — Agent system prompt
    prompts/weekly_research.md              — Weekly research task prompt
```

---

## Dependencies (requirements.txt)

```
fastapi>=0.115.0       # Web framework
uvicorn>=0.29.0        # ASGI server
aiosqlite>=0.20.0      # Async SQLite
aiohttp>=3.9.0         # HTTP client (Discord, Reddit polling)
curl-cffi>=0.7.0       # TLS fingerprint matching HTTP client
patchright>=0.1.0      # CDP-stealth Chromium automation (Playwright fork)
pydantic>=2.0.0        # Data validation
```

---

## Database Schema (6 tables in price_history.db)

### price_history
General price check log for all products.
```sql
id INTEGER PK, product_name TEXT, url TEXT, site TEXT,
price REAL, available INTEGER, blocked INTEGER, checked_at TEXT
```

### tcg_history
TCGPlayer-specific price tracking with full listing snapshots.
```sql
id INTEGER PK, product_name TEXT, url TEXT,
market_price REAL, low_price REAL, quantity INTEGER, listings INTEGER,
checked_at TEXT, listing_prices_json TEXT (nullable — full [{price,qty,shipping,total}])
```

### ebay_sold
eBay completed listing aggregates.
```sql
id INTEGER PK, product_name TEXT, url TEXT,
median_price REAL, avg_price REAL, low_price REAL, high_price REAL,
sold_count INTEGER, checked_at TEXT
```

### discord_log
Every Discord message (shown and filtered) with audit trail.
```sql
id INTEGER PK, msg_id TEXT, channel_id TEXT, author TEXT,
content TEXT, embed_title TEXT, embed_fields TEXT, price REAL,
action TEXT ('shown'|'filtered'), reason TEXT, timestamp TEXT, logged_at TEXT
```

### retailer_sightings
Normalized retailer intelligence extracted from Discord.
```sql
id INTEGER PK, product_name TEXT, game TEXT, retailer TEXT,
price REAL, asin TEXT, product_url TEXT, checkout_url TEXT,
channel_id TEXT, msg_id TEXT, timestamp TEXT, logged_at TEXT
```

### marketplace_messages
Buy/Sell/Trade channel parsed listings.
```sql
id INTEGER PK, msg_id TEXT, channel_id TEXT,
seller TEXT, seller_id TEXT, intent TEXT ('WTS'|'WTB'),
raw_text TEXT, items_json TEXT, matched_json TEXT,
timestamp TEXT, logged_at TEXT
```

---

## Key Data Models (web/state.py)

### ProductStatus — Live state per monitored product
```python
index, name, url, site, max_price, enabled, check_interval
price, available, last_checked, checking, error, next_check_in, image_url
tags: {retailer, set, product_type, category, condition, printing}
tcg_low_price, tcg_quantity, listing_prices, tcg_sales, tcg_price_history
ebay_median, ebay_avg, ebay_low, ebay_high, ebay_sold_count
ebay_by_grade, ebay_sales, ebay_live, ebay_ignored_titles
```

### TrackedDeal — Aggregated Discord deal
```python
id, name, normalized, tokens (word set for Jaccard matching)
sightings: [DealSighting] with price, retailer, url, checkout_urls
dismissed, tags, first_seen, last_seen
Methods: best_price(), retailers(), all_checkout_urls()
```

### ProductEntry — Watchlist hub item
```python
id, name, image_url, tags
retailer_urls: [RetailerLink], tcgplayer_index, deal_ids
```

### DealTracker — Groups Discord messages by product similarity
```python
MATCH_THRESHOLD = 0.40 (Jaccard)
ingest(msg) → TrackedDeal (new or matched existing)
```

---

## Background Tasks (web/app.py)

### monitor_loop()
Checks TCGPlayer/eBay products on schedule. Per-product intervals: sealed 30min default, singles 6hr, graded 7 days (configurable), per-product override. Max 4 concurrent checks. Pagination sanity check (85% DOM, 80% previous) prevents false listing count drops.

### discord_gateway_loop()
Monitors Discord channels via browser-based DOM scraping. Opens a visible Chrome window with one tab per monitored channel. Polls each tab's DOM every 3 seconds for new message elements. Extracts author, content, embeds (with links), and timestamps. Filters by keywords, min price, ignored patterns, blocked retailers. Ingests into deal tracker. Auto-assigns to watchlist. Sends DM notifications via bot token. Logs to audit table. Feeds retailer intelligence. Auto-clicks "Jump to present" after 5 minutes of channel inactivity.

### marketplace_gateway_loop()
Processes BST channel messages from the Discord Gateway. Parses WTS/WTB intent, extracts prices, matches against tracked TCGPlayer products. DM alerts for deals >15% below market.

### reddit_poll_loop()
Polls configurable subreddits every 30-300s. Supports multiple subreddits.

### research_loop()
Runs the AI research agent on a configurable schedule (default weekly). Analyzes SQLite data and codebase, generates evidence-backed feature recommendations. Requires Anthropic API key.

---

## API Routes (62+ endpoints)

**Categories:** Monitor control (start/stop), Products CRUD, Discord management (channels, keywords, ignore patterns, blocked retailers, audit log), Tracked deals (CRUD, merge, assign), Product hub (CRUD, retailer URLs, TCGPlayer linking), History/analytics (price history, TCG trends, listing snapshots, retailer overview, marketplace), Portfolio, Settings (all config editable via UI)

**Key endpoints:**
- `GET /api/retailer-overview` — retailer universe + timing heatmaps
- `GET /api/tcg-trends` — 7-day price/listing trends for all products
- `GET /api/tcg-history/{index}/listing-snapshots` — full listing distribution over time
- `GET /api/marketplace` — parsed BST listings
- `PUT /api/settings` — update any config value
- `WebSocket /ws` — real-time state broadcast

---

## Frontend (web/index.html — single file)

**7 tabs:** Watchlist, Deals, TCGPlayer, Graded, Portfolio, Intelligence

**Key features:**
- Real-time WebSocket updates (no polling)
- Chart.js for histograms, trend charts, sales scatter plots
- Price distribution with IQR outlier pruning and historical playback slider
- Card and Table views with drag-and-drop reordering (localStorage persisted)
- Game detection (MTG, Pokemon, Yu-Gi-Oh, etc.) with color-coded badges
- Deal scoring ("27% below market") comparing Discord prices vs TCGPlayer data
- Checkout link extraction (ATC/add-to-cart from Moonitor, Zephr, Refract)
- Multi-game filter (checkbox dropdown)
- First-run setup wizard for new users
- Mobile responsive (768px + 480px breakpoints)
- Settings modal with all config options

---

## Data Flow: Discord Message → Deal

1. `discord_gateway_loop` polls DOM of each Discord browser tab (one per monitored channel)
2. New message elements extracted: author, content, embeds (with links/fields), timestamp
3. `filter_message()` applies: keywords, min_price, ignored_patterns (with watchlist bypass), blocked_retailers
4. Log to `discord_log` table (shown + filtered)
5. Ingest into `DealTracker` (Jaccard similarity grouping, threshold 0.40)
6. Extract: product name (from embed Product field or title), retailer (from Site/Seller fields, author, URLs), checkout links (ATC/Zephr, excluding bot task links), game detection
7. Auto-assign to watchlist items (threshold 0.55)
8. Record to `retailer_sightings` table (with normalized retailer names)
9. DM notification via bot token if price < TCGPlayer market * 0.85
10. Broadcast via WebSocket to UI

## Data Flow: TCGPlayer Check → Analysis

1. `monitor_loop` determines product is due
2. Launch headless browser, navigate to filtered URL
3. Intercept API responses: market price, low price, listings, sales, price history
4. Paginate listing pages via DOM clicks (capture all seller prices + quantities)
5. Filter empty box listings
6. Apply shipping costs to get true total per listing
7. Sanity check: if listing count < 70% of DOM or 60% of previous → keep previous
8. Store: `price_history`, `tcg_history` (with listing_prices_json), `ebay_sold`
9. Compute trends: 1-day vs 7-day average for price and listing arrows
10. Broadcast updated product state via WebSocket

---

## Config Structure (config.json)

```json
{
  "check_interval_seconds": 1800,
  "graded_interval_seconds": 604800,
  "data_dir": null,
  "stealth": { "jitter_pct", "headless", "user_agent", "browser_channel", "page_timeout_ms", "network_timeout_seconds" },
  "schedule": { "enabled", "start", "end" },
  "products": [{ "name", "url", "site", "max_price", "enabled", "tags", "image_url" }],
  "discord": { "enabled", "email", "password", "bot_token", "dm_user_id", "channels_to_monitor", "keywords", "disabled_keywords", "ignored_patterns", "blocked_retailers", "min_price" },
  "marketplace": { "enabled", "sell_channels", "buy_channels" },
  "reddit": { "subreddits", "poll_interval_seconds" },
  "research": { "enabled", "api_key", "interval_hours", "lookback_days", "max_findings", "model" }
}
```

---

## Known Technical Debt

1. **Partial listing capture** — TCGPlayer pagination sometimes fails; sanity check (85% DOM, 80% previous) catches most but not all cases
2. **No rate limiting/backoff** — rapid retries if site is down could trigger IP bans
3. **Discord browser resource usage** — one Chrome tab per monitored channel; ~200-400MB RAM for the Discord browser instance
4. **Discord session management** — browser session can expire; "Jump to present" auto-click handles stale views but manual re-login may be needed
5. **Marketplace parser is regex-based** — free-text parsing is fragile for unusual formats
6. **eBay ignored titles exact-match only** — doesn't learn phrases, just individual words
7. **Portfolio not fully built** — exists but lacks auto-valuation and P&L calculations
8. **Single HTML file** — 7000+ lines; no component framework, no build step
9. **No tests** — no pytest/CI
10. **Discord credentials in plain config** — .gitignore protects it but no encryption
11. **WebSocket has no reconnection backoff** — client retries every 3s on disconnect
12. **Listing prices stored as JSON blob** — not indexed; queries slow for large datasets
