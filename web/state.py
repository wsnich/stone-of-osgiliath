"""
Shared application state — lives in memory, shared between the
monitor background tasks and the FastAPI request handlers.
"""

import asyncio
import json
import re
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

_PROJECT_ROOT = Path(__file__).parent.parent
_STATE_FILE = _PROJECT_ROOT / "app_state.json"
_DEALS_FILE = _PROJECT_ROOT / "tracked_deals.json"
_HUB_FILE   = _PROJECT_ROOT / "products_hub.json"


def set_data_dir(data_dir: Path) -> None:
    """Override all state file paths to use a custom data directory."""
    global _STATE_FILE, _DEALS_FILE, _HUB_FILE
    data_dir.mkdir(parents=True, exist_ok=True)
    _STATE_FILE = data_dir / "app_state.json"
    _DEALS_FILE = data_dir / "tracked_deals.json"
    _HUB_FILE   = data_dir / "products_hub.json"


@dataclass
class ProductStatus:
    index: int
    name: str
    url: str
    site: str
    max_price: float
    enabled: bool = True
    check_interval: int = 0          # seconds; 0 = use global interval
    price: Optional[float] = None
    available: bool = False
    last_checked: Optional[str] = None
    checking: bool = False
    error: Optional[str] = None
    next_check_in: Optional[int] = None   # seconds until next check (display only)
    image_url: Optional[str] = None
    tags: dict = field(default_factory=dict)  # {retailer, set, product_type}
    # TCGPlayer reference data
    tcg_low_price: Optional[float] = None
    tcg_market_low: Optional[float] = None  # 5th percentile from listing distribution
    tcg_quantity: Optional[int] = None
    # Listing prices (for histogram)
    listing_prices: Optional[list] = None
    # TCGPlayer recent sales
    tcg_sales: Optional[list] = None  # [{price, date, condition}]
    tcg_price_history: Optional[list] = None  # daily buckets [{date, marketPrice, low, high, sold}]
    # eBay sold data
    ebay_median: Optional[float] = None
    ebay_avg: Optional[float] = None
    ebay_low: Optional[float] = None
    ebay_high: Optional[float] = None
    ebay_sold_count: Optional[int] = None
    ebay_by_grade: Optional[dict] = None
    ebay_sales: Optional[list] = None  # [{title, price, grade, condition, ignored}]
    ebay_live: Optional[list] = None   # [{title, price, grade, ignored}]
    ebay_ignored_titles: list = field(default_factory=list)
    # Google Shopping
    google_shopping: Optional[list] = None  # [{title, price, shipping, total, retailer, domain, url, discount_pct}]
    google_shopping_checked: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Deal:
    timestamp: str
    product_name: str
    price: float
    max_price: float
    url: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Deal tracking (aggregated from Discord) ─────────────────────

_STRIP_PREFIXES = re.compile(
    r"^(magic:\s*the\s*gathering\s*|mtg\s+|pokemon\s+tcg\s*|yu-?gi-?oh!?\s*)",
    re.IGNORECASE,
)
_STRIP_SUFFIXES = re.compile(
    r"\s*[-–—]\s*(in stock|deal|sale|back in stock|price drop|restock|low price|new low).*$",
    re.IGNORECASE,
)
_RETAILER_RE = re.compile(
    r"\b(walmart|amazon|target|best\s*buy|bestbuy|tcgplayer|gamestop|"
    r"barnes|walgreens|costco|meijer|fan\s*atics)\b",
    re.IGNORECASE,
)
_NOISE_WORDS = {"the", "a", "an", "and", "or", "for", "of", "in", "on", "at",
                "to", "is", "it", "by", "with", "from", "up", "as", "new"}


def _normalize_name(text: str) -> str:
    """Normalize a product name for matching."""
    text = _STRIP_PREFIXES.sub("", text)
    text = _STRIP_SUFFIXES.sub("", text)
    text = _RETAILER_RE.sub("", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _tokenize(text: str) -> set[str]:
    """Tokenize a normalized name into significant words."""
    return {w for w in text.split() if w not in _NOISE_WORDS and len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _extract_retailer(msg: dict) -> str:
    """Extract retailer from a Discord message (embed fields, URL, or content)."""
    # Check embed fields first (most reliable)
    for e in msg.get("embeds", []):
        fields = e.get("fields", [])
        seller = _get_embed_field(fields, "Seller", "seller", "Store", "store", "Site", "site")
        if seller:
            return seller.strip()
        # Check URL domain
        url = e.get("url", "")
        if "walmart.com" in url:
            return "Walmart"
        if "amazon.com" in url:
            return "Amazon"
        if "target.com" in url:
            return "Target"
        if "bestbuy.com" in url:
            return "Best Buy"
        if "tcgplayer.com" in url:
            return "TCGPlayer"
        if "gamestop.com" in url:
            return "GameStop"
        if "barnesandnoble.com" in url:
            return "Barnes & Noble"

    # Scan content, author name, embed text for retailer keywords
    text_parts = [msg.get("content", ""), msg.get("author", "")]
    for e in msg.get("embeds", []):
        text_parts.append(e.get("title", ""))
        text_parts.append(e.get("description", ""))
        footer = e.get("footer", {})
        if isinstance(footer, dict):
            text_parts.append(footer.get("text", ""))
    content_lower = " ".join(text_parts).lower()
    retailer_map = [
        ("walmart", "Walmart"), ("amazon", "Amazon"), ("target", "Target"),
        ("best buy", "Best Buy"), ("bestbuy", "Best Buy"), ("tcgplayer", "TCGPlayer"),
        ("gamestop", "GameStop"), ("barnes", "Barnes & Noble"),
        ("costco", "Costco"), ("meijer", "Meijer"), ("walgreens", "Walgreens"),
        ("mattel", "Mattel Creations"), ("pokemon center", "Pokemon Center"),
    ]
    for kw, name in retailer_map:
        if kw in content_lower:
            return name
    return "Unknown"


def _extract_product_name(msg: dict) -> str:
    """Extract the best product name from a Discord message."""
    for e in msg.get("embeds", []):
        fields = e.get("fields", [])
        # Check embed fields for explicit "Product" field (Refract, Valor bots)
        product_field = _get_embed_field(fields, "Product", "Item", "product", "item")
        if product_field:
            return product_field.strip()[:120]
        # Use embed title if it's a real product name (not "New Checkout" etc.)
        title = (e.get("title") or "").strip()
        skip_titles = {"new checkout", "checked out", "checkout", "success", ""}
        if title and title.lower().rstrip("! 🎉") not in skip_titles:
            return title[:120]
        # Try embed description (Valor: "Checked Out! 🎉 Product Name")
        desc = (e.get("description") or "").strip()
        if desc:
            # Strip common prefixes
            for prefix in ["Checked Out!", "Checked Out", "New Checkout!", "New Checkout"]:
                if desc.startswith(prefix):
                    desc = desc[len(prefix):].strip().lstrip("🎉").strip()
                    break
            if desc:
                return desc[:120]
    # Fallback: message content (first line)
    content = msg.get("content", "").strip()
    first_line = content.split("\n")[0].strip()
    return first_line[:120] if first_line else "Unknown Product"


def _get_embed_field(fields, *names) -> str:
    """Get a field value from either [{name,value}] or {name:value} format."""
    if isinstance(fields, list):
        for f in fields:
            for n in names:
                if (f.get("name") or "").lower() == n.lower():
                    return f.get("value", "")
    elif isinstance(fields, dict):
        for n in names:
            if fields.get(n):
                return fields[n]
    return ""


def _extract_url(msg: dict) -> str:
    """Extract the best product URL from a Discord message."""
    for e in msg.get("embeds", []):
        if e.get("url"):
            return e["url"]
    # Try to find URL in content
    m = re.search(r'https?://\S+', msg.get("content", ""))
    return m.group(0) if m else ""


def _extract_image(msg: dict) -> Optional[str]:
    """Extract product image from a Discord message."""
    for e in msg.get("embeds", []):
        if e.get("image"):
            return e["image"]
        thumb = e.get("thumbnail", {})
        if isinstance(thumb, dict) and thumb.get("url"):
            return thumb["url"]
    return None


def _extract_checkout_links(msg: dict) -> list[dict]:
    """Extract add-to-cart / checkout links from Discord embed fields."""
    links = []
    _seen = set()
    for e in msg.get("embeds", []):
        fields = e.get("fields", {})
        if isinstance(fields, dict):
            text = " ".join(fields.values())
        elif isinstance(fields, list):
            text = " ".join(f.get("value", "") for f in fields)
        else:
            continue
        # Find markdown links: [Label](url)
        import re as _re
        for match in _re.finditer(r'\[([^\]]+)\]\((https?://[^)]+)\)', text):
            label, url = match.group(1), match.group(2)
            # Filter to actual checkout/cart links
            url_lower = url.lower()
            # Exclude bot task/setup links (not real checkout links)
            is_bot_link = any(kw in url_lower for kw in [
                'refractbot.com', 'valoraio.com', 'task', 'setup',
            ])
            if is_bot_link:
                continue
            is_cart = any(kw in url_lower for kw in [
                'cart', 'add.html', 'offers.zephr',
                '/click/', 'checkout', 'atc',
            ]) or any(kw in label.lower() for kw in [
                'atc', 'add to cart', 'checkout', 'buy',
            ])
            if is_cart and url not in _seen:
                _seen.add(url)
                links.append({"label": label, "url": url})
    return links


@dataclass
class DealSighting:
    price: Optional[float]
    retailer: str
    url: str
    timestamp: str
    msg_id: str
    image_url: Optional[str] = None
    checkout_urls: list = field(default_factory=list)  # [{label, url}]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrackedDeal:
    id: str
    name: str                                  # canonical display name
    normalized: str                            # normalized for matching
    tokens: set = field(default_factory=set)   # tokenized for Jaccard
    image_url: Optional[str] = None
    sightings: list[DealSighting] = field(default_factory=list)
    dismissed: bool = False
    tags: dict = field(default_factory=dict)   # {set, product_type, retailer}
    first_seen: str = ""
    last_seen: str = ""

    def best_price(self) -> Optional[float]:
        """Lowest price from all sightings."""
        prices = [s.price for s in self.sightings if s.price is not None]
        return min(prices) if prices else None

    def latest_sighting(self) -> Optional[DealSighting]:
        """Most recent sighting."""
        return self.sightings[0] if self.sightings else None

    def retailers(self) -> dict[str, DealSighting]:
        """Latest sighting per retailer."""
        result: dict[str, DealSighting] = {}
        for s in self.sightings:
            if s.retailer not in result:
                result[s.retailer] = s
        return result

    def all_checkout_urls(self) -> list[dict]:
        """Collect unique checkout URLs from all sightings, newest first."""
        seen = set()
        urls = []
        for s in self.sightings:
            for cu in (s.checkout_urls or []):
                if cu["url"] not in seen:
                    seen.add(cu["url"])
                    urls.append(cu)
        return urls

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "normalized": self.normalized,
            "image_url": self.image_url,
            "sightings": [s.to_dict() for s in self.sightings],
            "dismissed": self.dismissed,
            "tags": self.tags,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "best_price": self.best_price(),
            "retailer_prices": {r: s.to_dict() for r, s in self.retailers().items()},
            "checkout_urls": self.all_checkout_urls(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrackedDeal":
        sightings = [DealSighting(**s) for s in d.get("sightings", [])]
        norm = d.get("normalized", _normalize_name(d.get("name", "")))
        return cls(
            id=d["id"],
            name=d["name"],
            normalized=norm,
            tokens=_tokenize(norm),
            image_url=d.get("image_url"),
            sightings=sightings,
            dismissed=d.get("dismissed", False),
            tags=d.get("tags", {}),
            first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""),
        )


class DealTracker:
    """Aggregates Discord messages into tracked deals by product similarity."""

    MATCH_THRESHOLD = 0.40   # Jaccard threshold for auto-grouping

    def __init__(self):
        self.deals: list[TrackedDeal] = []

    def ingest(self, msg: dict) -> Optional[TrackedDeal]:
        """
        Ingest a Discord message and return the TrackedDeal it was added to
        (either existing match or newly created).
        """
        raw_name = _extract_product_name(msg)
        normalized = _normalize_name(raw_name)
        tokens = _tokenize(normalized)
        if not tokens:
            return None

        price = msg.get("price")
        retailer = _extract_retailer(msg)
        url = _extract_url(msg)
        image = _extract_image(msg)
        ts = msg.get("timestamp", datetime.now().isoformat())
        msg_id = msg.get("id", "")

        checkout_links = _extract_checkout_links(msg)

        sighting = DealSighting(
            price=price, retailer=retailer, url=url,
            timestamp=ts, msg_id=msg_id, image_url=image,
            checkout_urls=checkout_links,
        )

        # Find best matching existing deal
        best_deal = None
        best_score = 0.0
        for deal in self.deals:
            score = _jaccard(tokens, deal.tokens)
            if score > best_score:
                best_score = score
                best_deal = deal

        if best_deal and best_score >= self.MATCH_THRESHOLD:
            # Add sighting to existing deal (newest first)
            best_deal.sightings.insert(0, sighting)
            # Cap sightings to avoid unbounded growth
            if len(best_deal.sightings) > 50:
                best_deal.sightings = best_deal.sightings[:50]
            best_deal.last_seen = ts
            if image and not best_deal.image_url:
                best_deal.image_url = image
            return best_deal

        # Create new deal
        deal = TrackedDeal(
            id=str(uuid.uuid4())[:8],
            name=raw_name,
            normalized=normalized,
            tokens=tokens,
            image_url=image,
            sightings=[sighting],
            first_seen=ts,
            last_seen=ts,
        )
        self.deals.insert(0, deal)
        return deal

    def find_by_id(self, deal_id: str) -> Optional[TrackedDeal]:
        for d in self.deals:
            if d.id == deal_id:
                return d
        return None

    def save_to_disk(self) -> None:
        try:
            data = [d.to_dict() for d in self.deals]
            _DEALS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def restore_from_disk(self) -> None:
        if not _DEALS_FILE.exists():
            return
        try:
            data = json.loads(_DEALS_FILE.read_text(encoding="utf-8"))
            self.deals = [TrackedDeal.from_dict(d) for d in data]
        except Exception:
            pass


# ── Product Hub (centralized product registry) ──────────────────

@dataclass
class RetailerLink:
    retailer: str
    url: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductEntry:
    id: str
    name: str
    image_url: Optional[str] = None
    tags: dict = field(default_factory=dict)          # {set, product_type}
    retailer_urls: list[RetailerLink] = field(default_factory=list)
    tcgplayer_index: Optional[int] = None             # links to TCGPlayer tab item
    deal_ids: list[str] = field(default_factory=list)  # links to tracked deals

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "image_url": self.image_url,
            "tags": self.tags,
            "retailer_urls": [r.to_dict() for r in self.retailer_urls],
            "tcgplayer_index": self.tcgplayer_index,
            "deal_ids": self.deal_ids,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ProductEntry":
        urls = [RetailerLink(**r) for r in d.get("retailer_urls", [])]
        return cls(
            id=d["id"],
            name=d["name"],
            image_url=d.get("image_url"),
            tags=d.get("tags", {}),
            retailer_urls=urls,
            tcgplayer_index=d.get("tcgplayer_index"),
            deal_ids=d.get("deal_ids", []),
        )


class ProductHub:
    def __init__(self):
        self.entries: list[ProductEntry] = []

    def find_by_id(self, entry_id: str) -> Optional[ProductEntry]:
        for e in self.entries:
            if e.id == entry_id:
                return e
        return None

    def save_to_disk(self) -> None:
        try:
            data = [e.to_dict() for e in self.entries]
            _HUB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def restore_from_disk(self) -> None:
        if not _HUB_FILE.exists():
            return
        try:
            data = json.loads(_HUB_FILE.read_text(encoding="utf-8"))
            self.entries = [ProductEntry.from_dict(d) for d in data]
        except Exception:
            pass


class ConnectionManager:
    def __init__(self):
        self._connections: set = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws) -> None:
        async with self._lock:
            self._connections.add(ws)

    async def disconnect(self, ws) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        dead = set()
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            await self.disconnect(ws)


class AppState:
    def __init__(self):
        self.product_statuses: list[ProductStatus] = []
        self.deals: list[Deal] = []
        self.log_buffer: deque[dict] = deque(maxlen=300)
        self.reddit_posts: deque[dict] = deque(maxlen=100)
        self.discord_posts: deque[dict] = deque(maxlen=100)
        self.monitor_running: bool = False
        self.sleeping: bool = False          # True when outside check hours
        self.sleeping_label: str = ""        # e.g. "Sleeping until 07:00 (6h 12m)"
        self.ws = ConnectionManager()
        self._monitor_task: Optional[asyncio.Task] = None
        self._reddit_task: Optional[asyncio.Task] = None
        self.force_refresh_categories: set = set()  # {"single", "comic", "tcgplayer"}

    async def log(self, level: str, message: str, source: str = "system") -> None:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "source": source,
            "msg": message,
        }
        self.log_buffer.append(entry)
        await self.ws.broadcast({"type": "log", "data": entry})

    async def update_product(self, index: int, **kwargs) -> None:
        if 0 <= index < len(self.product_statuses):
            # Sanitize 130point proxy URLs to direct eBay URLs
            if "image_url" in kwargs and kwargs["image_url"] and "130point.com" in str(kwargs["image_url"]):
                from urllib.parse import unquote
                url = kwargs["image_url"]
                if "url=" in url:
                    inner = url.split("url=")[1].split("&")[0]
                    kwargs["image_url"] = unquote(inner)
            for k, v in kwargs.items():
                setattr(self.product_statuses[index], k, v)
            await self.ws.broadcast({
                "type": "product_update",
                "data": self.product_statuses[index].to_dict(),
            })

    async def record_deal(self, product_name: str, price: float, max_price: float, url: str, source: str) -> None:
        deal = Deal(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            product_name=product_name,
            price=price,
            max_price=max_price,
            url=url,
            source=source,
        )
        self.deals.insert(0, deal)
        await self.ws.broadcast({"type": "deal_found", "data": deal.to_dict()})
        await self.log("deal", f"{product_name} — ${price:.2f} (limit ${max_price:.2f})", source)

    def snapshot(self) -> dict:
        from web.state import deal_tracker, product_hub
        return {
            "type": "state",
            "data": {
                "monitor_running": self.monitor_running,
                "sleeping":        self.sleeping,
                "sleeping_label":  self.sleeping_label,
                "products":        [p.to_dict() for p in self.product_statuses],
                "deals":           [d.to_dict() for d in self.deals],
                "tracked_deals":   [d.to_dict() for d in deal_tracker.deals],
                "product_hub":     [e.to_dict() for e in product_hub.entries],
                "log":             list(self.log_buffer),
                "reddit_posts":    list(self.reddit_posts),
                "discord_posts":   list(self.discord_posts),
            },
        }

    def load_from_config(self, config: dict) -> None:
        existing = {ps.index: ps for ps in self.product_statuses}
        new_statuses = []
        for i, p in enumerate(config.get("products", [])):
            if i in existing:
                ps = existing[i]
                ps.name             = p["name"]
                ps.url              = p["url"]
                ps.max_price        = float(p.get("max_price", 9999))
                ps.enabled          = p.get("enabled", True)
                ps.site             = p.get("site", "walmart")
                ps.check_interval   = int(p.get("check_interval_seconds", 0))
                ps.tags             = p.get("tags", {})
                if not ps.image_url:
                    ps.image_url = p.get("image_url")
            else:
                ps = ProductStatus(
                    index=i,
                    name=p["name"],
                    url=p["url"],
                    site=p.get("site", "walmart"),
                    max_price=float(p.get("max_price", 9999)),
                    enabled=p.get("enabled", True),
                    check_interval=int(p.get("check_interval_seconds", 0)),
                    tags=p.get("tags", {}),
                    image_url=p.get("image_url"),
                )
            new_statuses.append(ps)
        self.product_statuses = new_statuses

    def save_to_disk(self) -> None:
        """Persist last-known product state and deals so they survive restarts."""
        try:
            data = {
                "products": [p.to_dict() for p in self.product_statuses],
                "deals":    [d.to_dict() for d in self.deals],
            }
            _STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass  # non-critical — don't crash the app

    def restore_from_disk(self) -> None:
        """Restore last-known values after load_from_config has built the product list."""
        if not _STATE_FILE.exists():
            return
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        # Sanitize saved data before restoring
        from urllib.parse import unquote
        for p in data.get("products", []):
            # Fix 130point proxy image URLs
            img = p.get("image_url", "")
            if img and "130point.com" in img and "url=" in img:
                inner = img.split("url=")[1].split("&")[0]
                p["image_url"] = unquote(inner)
            # Clean "Opens in a new window or tab" from ignored titles
            cleaned = []
            for t in p.get("ebay_ignored_titles", []):
                cleaned.append(t.replace("Opens in a new window or tab", "").replace("Opens in a new window or ta", "").strip())
            p["ebay_ignored_titles"] = cleaned
            # Clean from sales too
            for s in p.get("ebay_sales", []) or []:
                s["title"] = s.get("title", "").replace("Opens in a new window or tab", "").replace("Opens in a new window or ta", "").strip()

        # Restore product state by matching on index
        saved = {p["index"]: p for p in data.get("products", []) if "index" in p}
        for ps in self.product_statuses:
            s = saved.get(ps.index)
            if not s:
                continue
            # Only restore runtime fields — config fields (name, url, etc.)
            # are already set by load_from_config
            if s.get("price") is not None:
                ps.price          = s["price"]
            ps.available          = s.get("available", False)
            ps.last_checked       = s.get("last_checked")
            ps.error              = s.get("error")
            ps.next_check_in      = s.get("next_check_in")
            ps.tcg_low_price      = s.get("tcg_low_price")
            ps.tcg_market_low     = s.get("tcg_market_low")
            ps.tcg_quantity       = s.get("tcg_quantity")
            ps.tcg_sales          = s.get("tcg_sales")
            ps.tcg_price_history  = s.get("tcg_price_history")
            ps.listing_prices     = s.get("listing_prices")
            ps.ebay_median        = s.get("ebay_median")
            ps.ebay_avg           = s.get("ebay_avg")
            ps.ebay_low           = s.get("ebay_low")
            ps.ebay_high          = s.get("ebay_high")
            ps.ebay_sold_count    = s.get("ebay_sold_count")
            ps.ebay_by_grade      = s.get("ebay_by_grade")
            ps.ebay_sales           = s.get("ebay_sales")
            ps.ebay_live            = s.get("ebay_live")
            ps.ebay_ignored_titles  = s.get("ebay_ignored_titles", [])
            if s.get("image_url") and not ps.image_url:
                ps.image_url      = s["image_url"]

        # Restore deals
        for d in data.get("deals", []):
            try:
                self.deals.append(Deal(**d))
            except Exception:
                pass



app_state = AppState()
deal_tracker = DealTracker()
product_hub = ProductHub()
