"""
SQLite price history database.

Records every check result so you can see how prices move over time
and know whether a price is actually a good deal or just normal.

Schema
------
price_history
  id           INTEGER  PK
  product_name TEXT     display name
  url          TEXT     product URL (used as the primary key for lookups)
  site         TEXT     'walmart', 'ebay', etc.
  price        REAL     NULL if price couldn't be read
  available    INTEGER  1 = in stock, 0 = out of stock
  blocked      INTEGER  1 = bot-wall detected
  checked_at   TEXT     ISO-8601 UTC timestamp
"""

import aiosqlite
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).parent
DB_PATH = _PROJECT_ROOT / "price_history.db"


def set_db_path(data_dir: Path) -> None:
    """Override the database path to use a custom data directory."""
    global DB_PATH
    DB_PATH = data_dir / "price_history.db"

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT    NOT NULL,
                url          TEXT    NOT NULL,
                site         TEXT    NOT NULL DEFAULT 'walmart',
                price        REAL,
                available    INTEGER NOT NULL DEFAULT 0,
                blocked      INTEGER NOT NULL DEFAULT 0,
                checked_at   TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_url_time ON price_history(url, checked_at)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tcg_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name   TEXT    NOT NULL,
                url            TEXT    NOT NULL,
                market_price   REAL,
                low_price      REAL,
                quantity        INTEGER,
                listings        INTEGER,
                checked_at     TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tcg_url_time ON tcg_history(url, checked_at)")

        # Migration: add listing_prices_json column
        try:
            await db.execute("ALTER TABLE tcg_history ADD COLUMN listing_prices_json TEXT")
        except Exception:
            pass  # column already exists

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ebay_sold (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT    NOT NULL,
                url          TEXT    NOT NULL,
                median_price REAL,
                avg_price    REAL,
                low_price    REAL,
                high_price   REAL,
                sold_count   INTEGER,
                checked_at   TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ebay_url_time ON ebay_sold(url, checked_at)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS discord_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id       TEXT    NOT NULL,
                channel_id   TEXT,
                author       TEXT,
                content      TEXT,
                embed_title  TEXT,
                embed_fields TEXT,
                price        REAL,
                action       TEXT    NOT NULL,
                reason       TEXT,
                timestamp    TEXT,
                logged_at    TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_discord_time ON discord_log(logged_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_discord_action ON discord_log(action)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS retailer_sightings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT    NOT NULL,
                game         TEXT,
                retailer     TEXT    NOT NULL,
                price        REAL,
                asin         TEXT,
                product_url  TEXT,
                checkout_url TEXT,
                channel_id   TEXT,
                msg_id       TEXT,
                timestamp    TEXT    NOT NULL,
                logged_at    TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rs_retailer ON retailer_sightings(retailer)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rs_game ON retailer_sightings(game)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rs_product ON retailer_sightings(product_name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rs_time ON retailer_sightings(timestamp)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id        TEXT    NOT NULL,
                channel_id    TEXT,
                seller        TEXT,
                seller_id     TEXT,
                intent        TEXT,
                raw_text      TEXT,
                items_json    TEXT,
                matched_json  TEXT,
                timestamp     TEXT,
                logged_at     TEXT    NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mp_time ON marketplace_messages(timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mp_seller ON marketplace_messages(seller)")
        await db.commit()

# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def record_check(
    product_name: str,
    url: str,
    site: str,
    price: Optional[float],
    available: bool,
    blocked: bool,
    checked_at: str,          # "YYYY-MM-DD HH:MM:SS"
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO price_history
               (product_name, url, site, price, available, blocked, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (product_name, url, site, price,
             1 if available else 0,
             1 if blocked else 0,
             checked_at),
        )
        await db.commit()

# ---------------------------------------------------------------------------
# Write — TCGPlayer history
# ---------------------------------------------------------------------------

async def record_tcg_check(
    product_name: str,
    url: str,
    market_price: Optional[float],
    low_price: Optional[float],
    quantity: Optional[int],
    listings: Optional[int],
    checked_at: str,
    listing_prices_json: Optional[str] = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO tcg_history
               (product_name, url, market_price, low_price, quantity, listings, checked_at, listing_prices_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_name, url, market_price, low_price, quantity, listings, checked_at, listing_prices_json),
        )
        await db.commit()

# ---------------------------------------------------------------------------
# Read — TCGPlayer history for chart
# ---------------------------------------------------------------------------

async def get_tcg_history(url: str, days: int = 30, max_points: int = 500) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT market_price, low_price, quantity, listings, checked_at
               FROM tcg_history
               WHERE url = ?
                 AND checked_at >= datetime('now', ?)
               ORDER BY checked_at ASC""",
            (url, f"-{days} days"),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if len(rows) > max_points:
        step = len(rows) / max_points
        rows = [rows[int(i * step)] for i in range(max_points - 1)] + [rows[-1]]

    return rows


async def get_tcg_listing_snapshots(url: str, days: int = 90) -> list[dict]:
    """Return listing-price snapshots: [{checked_at, listing_prices_json}]."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT checked_at, listing_prices_json
               FROM tcg_history
               WHERE url = ?
                 AND checked_at >= datetime('now', ?)
                 AND listing_prices_json IS NOT NULL
               ORDER BY checked_at ASC""",
            (url, f"-{days} days"),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Read — TCGPlayer aggregate stats
# ---------------------------------------------------------------------------

async def get_tcg_stats(url: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT
                   MIN(market_price)  AS min_market,
                   MAX(market_price)  AS max_market,
                   AVG(market_price)  AS avg_market,
                   MIN(low_price)     AS min_low,
                   MAX(low_price)     AS max_low,
                   AVG(low_price)     AS avg_low,
                   MIN(quantity)      AS min_qty,
                   MAX(quantity)      AS max_qty,
                   AVG(quantity)      AS avg_qty,
                   MAX(listings)      AS max_listings,
                   COUNT(*)           AS total_checks,
                   MIN(checked_at)    AS first_seen,
                   MAX(checked_at)    AS last_seen
               FROM tcg_history
               WHERE url = ? AND (market_price IS NOT NULL OR low_price IS NOT NULL)""",
            (url,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_tcg_trends() -> list[dict]:
    """Return 7-day price/listing trend for all TCGPlayer products."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT url,
                   AVG(CASE WHEN checked_at >= datetime('now', '-1 day') THEN market_price END) AS price_1d,
                   AVG(CASE WHEN checked_at >= datetime('now', '-7 days') THEN market_price END) AS price_7d,
                   AVG(CASE WHEN checked_at >= datetime('now', '-1 day') THEN listings END) AS listings_1d,
                   AVG(CASE WHEN checked_at >= datetime('now', '-7 days') THEN listings END) AS listings_7d
            FROM tcg_history
            WHERE checked_at >= datetime('now', '-7 days')
              AND (market_price IS NOT NULL OR listings IS NOT NULL)
            GROUP BY url
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Retailer Intelligence
# ---------------------------------------------------------------------------

async def record_retailer_sighting(
    product_name: str, game: Optional[str], retailer: str,
    price: Optional[float], asin: Optional[str],
    product_url: Optional[str], checkout_url: Optional[str],
    channel_id: Optional[str], msg_id: Optional[str],
    timestamp: str,
) -> None:
    from datetime import datetime as _dt
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO retailer_sightings
               (product_name, game, retailer, price, asin, product_url, checkout_url,
                channel_id, msg_id, timestamp, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_name, game, retailer, price, asin, product_url, checkout_url,
             channel_id, msg_id, timestamp, _dt.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()


async def get_retailer_overview() -> dict:
    """Build the retailer intelligence overview from sighting data."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Retailer summary
        async with db.execute("""
            SELECT retailer, game,
                   COUNT(*) as sightings,
                   COUNT(DISTINCT product_name) as products,
                   MIN(price) as min_price, MAX(price) as max_price,
                   AVG(price) as avg_price,
                   MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
            FROM retailer_sightings
            GROUP BY retailer, game
            ORDER BY sightings DESC
        """) as cur:
            retailer_game = [dict(r) for r in await cur.fetchall()]

        # Products across retailers
        async with db.execute("""
            SELECT product_name, game, retailer,
                   COUNT(*) as sightings,
                   MIN(price) as min_price, MAX(price) as max_price,
                   AVG(price) as avg_price,
                   MAX(timestamp) as last_seen
            FROM retailer_sightings
            WHERE price IS NOT NULL
            GROUP BY product_name, retailer
            ORDER BY sightings DESC
            LIMIT 100
        """) as cur:
            product_retailers = [dict(r) for r in await cur.fetchall()]

        # Recent activity
        async with db.execute("""
            SELECT product_name, game, retailer, price, timestamp
            FROM retailer_sightings
            ORDER BY timestamp DESC
            LIMIT 50
        """) as cur:
            recent = [dict(r) for r in await cur.fetchall()]

        # Timing patterns (hour of day)
        async with db.execute("""
            SELECT retailer,
                   CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                   COUNT(*) as cnt
            FROM retailer_sightings
            WHERE timestamp != ''
            GROUP BY retailer, hour
            ORDER BY retailer, hour
        """) as cur:
            timing = [dict(r) for r in await cur.fetchall()]

        # Day-of-week patterns
        async with db.execute("""
            SELECT retailer,
                   CAST(strftime('%w', timestamp) AS INTEGER) as dow,
                   COUNT(*) as cnt
            FROM retailer_sightings
            WHERE timestamp != ''
            GROUP BY retailer, dow
            ORDER BY retailer, dow
        """) as cur:
            day_of_week = [dict(r) for r in await cur.fetchall()]

        return {
            "retailer_game": retailer_game,
            "product_retailers": product_retailers,
            "recent": recent,
            "timing": timing,
            "day_of_week": day_of_week,
        }


async def backfill_retailer_sightings() -> int:
    """Backfill retailer_sightings from existing discord_log data."""
    import re as _re
    count = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check if already backfilled
        async with db.execute("SELECT COUNT(*) as cnt FROM retailer_sightings") as cur:
            existing = (await cur.fetchone())["cnt"]
        if existing > 0:
            return 0  # already has data

        async with db.execute("SELECT * FROM discord_log WHERE embed_fields IS NOT NULL AND embed_fields != ''") as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        for r in rows:
            fields = r.get('embed_fields') or ''
            title = r.get('embed_title') or ''
            price = r.get('price')
            text = (title + ' ' + fields).lower()

            # Detect game
            game = None
            if any(k in text for k in ('magic', 'mtg', 'strixhaven', 'spider-man', 'avatar', 'secret lair')):
                game = 'MTG'
            elif any(k in text for k in ('pokemon', 'pokémon', 'prismatic', 'paldea')):
                game = 'Pokemon'
            elif 'riftbound' in text:
                game = 'Riftbound'
            elif any(k in text for k in ('yu-gi-oh', 'yugioh')):
                game = 'Yu-Gi-Oh'
            elif 'lorcana' in text:
                game = 'Lorcana'
            elif 'star wars' in text:
                game = 'Star Wars'
            elif 'digimon' in text:
                game = 'Digimon'
            elif any(k in text for k in ('flesh and blood', 'fab ')):
                game = 'FAB'
            elif 'one piece' in text:
                game = 'One Piece'
            elif 'dragon ball' in text:
                game = 'Dragon Ball'

            # Detect retailer
            retailer = None
            if 'amazon.com' in fields or 'amazon.ca' in fields:
                retailer = 'Amazon'
            elif 'walmart.com' in fields:
                retailer = 'Walmart'
            elif 'target.com' in fields:
                retailer = 'Target'
            elif 'bestbuy.com' in fields:
                retailer = 'Best Buy'
            else:
                seller_m = _re.search(r'(?:Seller|Site):\s*(.+)', fields)
                if seller_m:
                    s = seller_m.group(1).strip().lower()
                    if 'amazon' in s: retailer = 'Amazon'
                    elif 'walmart' in s: retailer = 'Walmart'
                    elif 'target' in s: retailer = 'Target'
                    elif 'best buy' in s: retailer = 'Best Buy'
                    else: retailer = seller_m.group(1).strip()
                site_m = _re.search(r'Site:\s*(.+)', fields)
                if site_m and not retailer:
                    retailer = site_m.group(1).strip()

            if not retailer or not title:
                continue

            # Extract ASIN
            asin_m = _re.search(r'(?:ASIN|SKU):\s*(?:```)?([A-Z0-9]{10})', fields)
            asin = asin_m.group(1) if asin_m else None

            # Extract product URL
            product_url = None
            url_m = _re.search(r'\[.*?\]\((https?://(?:www\.)?(?:amazon|walmart|target|bestbuy|tcgplayer)[^\)]+)\)', fields)
            if url_m:
                product_url = url_m.group(1)
            elif asin:
                product_url = f'https://www.amazon.com/dp/{asin}'

            # Extract checkout URL
            checkout_url = None
            cart_m = _re.search(r'\[(?:ATC\w*|Add to cart|Click Here)\]\((https?://[^\)]+)\)', fields, _re.I)
            if cart_m:
                checkout_url = cart_m.group(1)

            # Clean title
            product_name = title.strip('*').strip()[:120]

            await db.execute(
                """INSERT INTO retailer_sightings
                   (product_name, game, retailer, price, asin, product_url, checkout_url,
                    channel_id, msg_id, timestamp, logged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (product_name, game, retailer, price, asin, product_url, checkout_url,
                 r.get('channel_id'), r.get('msg_id'), r.get('timestamp', ''),
                 r.get('logged_at', '')),
            )
            count += 1

        await db.commit()
    return count


# ---------------------------------------------------------------------------
# Marketplace messages
# ---------------------------------------------------------------------------

async def record_marketplace_message(
    msg_id: str, channel_id: str, seller: str, seller_id: str,
    intent: str, raw_text: str, items_json: str, matched_json: str,
    timestamp: str,
) -> None:
    from datetime import datetime as _dt
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO marketplace_messages
               (msg_id, channel_id, seller, seller_id, intent, raw_text,
                items_json, matched_json, timestamp, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, channel_id, seller, seller_id, intent, raw_text[:1000],
             items_json, matched_json, timestamp,
             _dt.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()


async def get_marketplace_listings(limit: int = 50, intent: str = "") -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM marketplace_messages"
        params = []
        if intent:
            query += " WHERE intent = ?"
            params.append(intent)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with db.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


# Write — eBay sold history
# ---------------------------------------------------------------------------

async def record_ebay_sold(
    product_name: str,
    url: str,
    median_price: Optional[float],
    avg_price: Optional[float],
    low_price: Optional[float],
    high_price: Optional[float],
    sold_count: int,
    checked_at: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ebay_sold
               (product_name, url, median_price, avg_price, low_price, high_price, sold_count, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_name, url, median_price, avg_price, low_price, high_price, sold_count, checked_at),
        )
        await db.commit()

# ---------------------------------------------------------------------------
# Write — Discord audit log
# ---------------------------------------------------------------------------

async def log_discord_message(
    msg_id: str, channel_id: str, author: str, content: str,
    embed_title: str, embed_fields: str, price: Optional[float],
    action: str, reason: str, timestamp: str,
) -> None:
    """Log a Discord message with its filter action/reason."""
    from datetime import datetime
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO discord_log
               (msg_id, channel_id, author, content, embed_title, embed_fields,
                price, action, reason, timestamp, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, channel_id, author, content, embed_title, embed_fields,
             price, action, reason, timestamp,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        await db.commit()

# ---------------------------------------------------------------------------
# Read — Discord audit log
# ---------------------------------------------------------------------------

async def search_discord_log(
    query: str = "", action: str = "", limit: int = 100, offset: int = 0,
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        conditions = []
        params = []
        if query:
            conditions.append("(content LIKE ? OR embed_title LIKE ? OR embed_fields LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        if action:
            conditions.append("action = ?")
            params.append(action)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.extend([limit, offset])
        async with db.execute(
            f"""SELECT * FROM discord_log {where}
                ORDER BY logged_at DESC LIMIT ? OFFSET ?""",
            params,
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def discord_log_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT action, COUNT(*) as count FROM discord_log GROUP BY action"""
        ) as cur:
            rows = await cur.fetchall()
            return {r["action"]: r["count"] for r in rows}

# ---------------------------------------------------------------------------
# Read — history for chart
# ---------------------------------------------------------------------------

async def get_history(url: str, days: int = 30, max_points: int = 500) -> list[dict]:
    """
    Return up to `max_points` rows for a product URL over the last `days` days.
    When there are more rows than max_points the data is evenly downsampled so
    the chart stays fast regardless of how long the monitor has been running.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT price, available, blocked, checked_at
               FROM price_history
               WHERE url = ?
                 AND checked_at >= datetime('now', ?)
                 AND blocked = 0
               ORDER BY checked_at ASC""",
            (url, f"-{days} days"),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    # Downsample if needed — keep first + last + evenly-spaced interior points
    if len(rows) > max_points:
        step  = len(rows) / max_points
        rows  = [rows[int(i * step)] for i in range(max_points - 1)] + [rows[-1]]

    return rows

# ---------------------------------------------------------------------------
# Read — aggregate stats
# ---------------------------------------------------------------------------

async def get_stats(url: str) -> dict:
    """All-time stats for a product URL (only counts rows where price is known)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT
                   MIN(price)    AS min_price,
                   MAX(price)    AS max_price,
                   AVG(price)    AS avg_price,
                   COUNT(*)      AS total_checks,
                   SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS in_stock_count,
                   MIN(checked_at) AS first_seen,
                   MAX(checked_at) AS last_seen
               FROM price_history
               WHERE url = ? AND price IS NOT NULL AND blocked = 0""",
            (url,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}
