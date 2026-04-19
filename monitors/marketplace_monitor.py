"""
Discord Marketplace (BST) Monitor.

Parses Buy/Sell/Trade channels for listings that match tracked products.
Extracts seller, price, and intent (WTS/WTB) from free-text messages.
"""

import logging
import re
from typing import Optional

log = logging.getLogger("marketplace")

# Noise filters — skip these messages
_NOISE_PATTERNS = [
    r'\bprice\s*check\b', r'\bwhat.?s?\s*(this|it)\s*worth\b', r'\bpc\?\b',
    r'\bhow\s*much\b.*\?', r'\biso\b.*\?',
]
_NOISE_RE = [re.compile(p, re.I) for p in _NOISE_PATTERNS]


class MarketplaceListing:
    """A parsed marketplace listing."""
    def __init__(self, seller: str, seller_id: str, intent: str,
                 raw_text: str, items: list, msg_id: str,
                 channel_id: str, timestamp: str, jump_url: str):
        self.seller = seller
        self.seller_id = seller_id
        self.intent = intent          # "WTS" or "WTB"
        self.raw_text = raw_text
        self.items = items            # [{name, price, qty}]
        self.msg_id = msg_id
        self.channel_id = channel_id
        self.timestamp = timestamp
        self.jump_url = jump_url

    def to_dict(self) -> dict:
        return {
            "seller": self.seller,
            "seller_id": self.seller_id,
            "intent": self.intent,
            "raw_text": self.raw_text[:300],
            "items": self.items,
            "msg_id": self.msg_id,
            "channel_id": self.channel_id,
            "timestamp": self.timestamp,
            "jump_url": self.jump_url,
        }


def parse_intent(text: str) -> str:
    """Detect WTS (selling) or WTB (buying) from message text."""
    t = text[:200].lower()
    if any(k in t for k in ('wts', 'selling', 'for sale', 'fs ', 'h]', '[h]')):
        return 'WTS'
    if any(k in t for k in ('wtb', 'buying', 'looking for', 'w]', '[w]', 'iso')):
        return 'WTB'
    return 'WTS'  # default for selling channels


def is_noise(text: str) -> bool:
    """Check if a message is just a price check or question, not a listing."""
    if len(text) < 10:
        return True
    for pattern in _NOISE_RE:
        if pattern.search(text):
            return True
    return False


def extract_prices(text: str) -> list[dict]:
    """
    Extract price-item pairs from free-text marketplace messages.
    Returns [{name: str, price: float, qty: int}]
    """
    items = []

    # Split into lines for per-line parsing
    lines = text.split('\n')

    for line in lines:
        line = line.strip()
        if not line or len(line) < 5:
            continue

        # Skip common noise lines
        if any(k in line.lower() for k in ('paypal', 'venmo', 'zelle', 'fnf', 'f&f',
                                            'shipped', 'shipping', 'dm me', 'pm me',
                                            'timestamp', 'rep ', 'feedback')):
            if '$' not in line:
                continue

        # Extract all dollar amounts from the line
        price_matches = list(re.finditer(r'\$\s*([\d,]+\.?\d*)', line))
        if not price_matches:
            continue

        # Extract quantity patterns
        qty = 1
        qty_match = re.search(r'(\d+)\s*x\b', line, re.I) or re.search(r'\bx\s*(\d+)', line, re.I)
        if qty_match:
            try:
                qty = int(qty_match.group(1))
            except ValueError:
                pass

        # The price is usually the first dollar amount
        try:
            price = float(price_matches[0].group(1).replace(',', ''))
        except ValueError:
            continue

        if price < 1 or price > 50000:
            continue

        # The item name is the text before or around the price
        # Clean up the line to get the item name
        name = line
        # Remove price and quantity patterns
        name = re.sub(r'\$\s*[\d,]+\.?\d*', '', name)
        name = re.sub(r'\d+\s*x\b', '', name, flags=re.I)
        name = re.sub(r'\bx\s*\d+', '', name, flags=re.I)
        name = re.sub(r'\b(per|each|ea|shipped|ff|fnf|zelle|paypal)\b', '', name, flags=re.I)
        name = re.sub(r'[*_~`|]', '', name)  # strip markdown
        name = re.sub(r'\s+', ' ', name).strip(' -:,/')

        if len(name) < 3:
            continue

        items.append({
            "name": name,
            "price": round(price, 2),
            "qty": max(1, qty),
        })

    return items


def match_to_products(items: list[dict], product_names: list[dict]) -> list[dict]:
    """
    Match extracted marketplace items against tracked products.
    product_names: [{index, name, normalized_words}]
    Returns items with matched product_index added.
    """
    matched = []
    for item in items:
        item_words = set(re.findall(r'\w{3,}', item['name'].lower()))
        if len(item_words) < 2:
            continue

        best_match = None
        best_score = 0.0

        for prod in product_names:
            overlap = len(item_words & prod['words'])
            total = len(item_words | prod['words'])
            if total == 0:
                continue
            score = overlap / total
            if score > best_score:
                best_score = score
                best_match = prod

        if best_match and best_score >= 0.30:
            item['product_index'] = best_match['index']
            item['product_name'] = best_match['name']
            item['match_score'] = round(best_score, 2)
            matched.append(item)

    return matched
