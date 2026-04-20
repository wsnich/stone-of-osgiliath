"""
Discord message filtering and DM sending.

Provides filter_message() for the Gateway WebSocket monitor and
send_dm() for Discord bot DM notifications.
"""

import logging
import re
from typing import Optional

log = logging.getLogger("discord_mon")

_API_BASE = "https://discord.com/api/v10"


class DiscordMonitor:

    def __init__(self):
        self._channel_names: dict[str, str] = {}  # channel_id -> name


    async def send_dm(self, token: str, user_id: str, content: str) -> bool:
        """Send a DM to a Discord user. Returns True on success."""
        import aiohttp
        # First, open/get the DM channel
        try:
            async with aiohttp.ClientSession() as session:
                # Create DM channel
                async with session.post(
                    f"{_API_BASE}/users/@me/channels",
                    headers={"Authorization": token, "Content-Type": "application/json"},
                    json={"recipient_id": user_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        log.warning(f"Discord DM: failed to open channel (status {resp.status})")
                        return False
                    data = await resp.json()
                    dm_channel_id = data.get("id")

                # Send message
                async with session.post(
                    f"{_API_BASE}/channels/{dm_channel_id}/messages",
                    headers={"Authorization": token, "Content-Type": "application/json"},
                    json={"content": content},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        return True
                    log.warning(f"Discord DM: send failed (status {resp.status})")
                    return False
        except Exception as e:
            log.warning(f"Discord DM error: {e}")
            return False


    def filter_message(self, msg: dict, config: dict) -> tuple[Optional[dict], dict]:
        """
        Apply the full filtering pipeline to a single Discord message.
        Returns (matched_entry_or_None, audit_log_entry).
        Used by both REST poll() and Gateway mode.

        msg: raw Discord message object (id, channel_id, content, author, embeds, timestamp)
        """
        dc = config.get("discord", {})
        ignored_patterns = [p.lower() for p in dc.get("ignored_patterns", [])]
        blocked_retailers = [r.lower() for r in dc.get("blocked_retailers", [])]

        # Build keywords
        disabled = set(k.lower() for k in dc.get("disabled_keywords", []))
        keywords = [k.lower() for k in dc.get("keywords", []) if k.lower() not in disabled]
        for p in config.get("products", []):
            tags = p.get("tags", {})
            cat = tags.get("category", "")
            if cat in ("single", "comic"):
                if not tags.get("notify_on_discord", False):
                    continue
            s = tags.get("set", "")
            pt = tags.get("product_type", "")
            if s and s.lower() not in keywords and s.lower() not in disabled:
                keywords.append(s.lower())
            if pt and pt.lower() not in keywords and pt.lower() not in disabled:
                keywords.append(pt.lower())
            if cat in ("single", "comic") and tags.get("notify_on_discord"):
                name_kw = re.sub(r'[^\w\s]', '', p.get("name", "")).lower().strip()
                if name_kw and name_kw not in keywords and name_kw not in disabled:
                    keywords.append(name_kw)

        # Build watchlist token sets for ignore-pattern bypass
        watchlist_token_sets = []
        for p in config.get("products", []):
            tags = p.get("tags", {})
            if tags.get("category") in ("single", "comic"):
                continue
            parts = [p.get("name", "")]
            if tags.get("set"): parts.append(tags["set"])
            if tags.get("product_type"): parts.append(tags["product_type"])
            combined = re.sub(r'[^\w\s]', '', " ".join(parts)).lower()
            words = set(w for w in combined.split() if len(w) >= 3)
            if words:
                watchlist_token_sets.append(words)

        mid = str(msg.get("id", ""))
        content = msg.get("content", "")
        content_lower = content.lower()
        author = msg.get("author", {})
        channel_id = str(msg.get("channel_id", ""))

        embed_text = ""
        embed_title = ""
        embed_fields_str = ""
        for e in msg.get("embeds", []):
            embed_text += " " + (e.get("title", "") + " " + e.get("description", "")).lower()
            if e.get("title"):
                embed_title = e["title"][:100]
            for f in e.get("fields", []):
                fname = f.get("name", "")
                fval = f.get("value", "")
                embed_fields_str += f"{fname}: {fval}\n"
                embed_text += " " + fval.lower()

        full_text = content_lower + embed_text

        # Extract price
        price = self._extract_price(content)
        if price is None:
            for e in msg.get("embeds", []):
                for f in e.get("fields", []):
                    if f.get("name", "").strip().lower() == "price":
                        price = self._extract_price(f.get("value", ""))
                        if price: break
                if price: break

        log_entry = {
            "msg_id": mid, "channel_id": channel_id,
            "author": author.get("username", "unknown") if isinstance(author, dict) else str(author),
            "content": content[:500], "embed_title": embed_title,
            "embed_fields": embed_fields_str[:500], "price": price,
            "timestamp": msg.get("timestamp", ""),
        }

        # Check ignored patterns with watchlist bypass
        matched_ignore = [p for p in ignored_patterns if p in full_text]
        if matched_ignore:
            msg_words = set(w for w in re.sub(r'[^\w\s]', '', full_text).split() if len(w) >= 3)
            watchlist_hit = any(
                len(msg_words & wl) / len(msg_words | wl) >= 0.40
                for wl in watchlist_token_sets if msg_words | wl
            )
            if not watchlist_hit:
                return None, {**log_entry, "action": "filtered", "reason": f"ignored pattern: {matched_ignore[0]}"}

        # Check blocked retailers
        if blocked_retailers:
            all_urls = embed_text + " " + embed_fields_str.lower()
            blocked_match = [r for r in blocked_retailers if r in all_urls]
            if blocked_match:
                return None, {**log_entry, "action": "filtered", "reason": f"blocked retailer: {blocked_match[0]}"}

        # Check keyword matches
        if keywords:
            matched = [kw for kw in keywords if kw in full_text]
            if not matched:
                return None, {**log_entry, "action": "filtered", "reason": "no keyword match"}
        else:
            matched = []

        # Price floor filter
        min_price = dc.get("min_price", 0)
        if min_price and price is not None and price < min_price:
            return None, {**log_entry, "action": "filtered", "reason": f"price ${price:.2f} < min ${min_price}"}
        if min_price and price is None:
            return None, {**log_entry, "action": "filtered", "reason": "no price detected"}

        # Passed all filters
        audit = {**log_entry, "action": "shown", "reason": f"matched: {', '.join(matched) if matched else 'all'}"}

        result = {
            "id": mid,
            "channel_id": channel_id,
            "channel_name": self._channel_names.get(channel_id, ""),
            "author": author.get("username", "unknown") if isinstance(author, dict) else str(author),
            "avatar": author.get("avatar", "") if isinstance(author, dict) else "",
            "author_id": author.get("id", "") if isinstance(author, dict) else "",
            "content": content[:500],
            "timestamp": msg.get("timestamp", ""),
            "matched_keywords": matched,
            "price": price,
            "embeds": self._extract_embeds(msg.get("embeds", [])),
        }

        return result, audit

    @staticmethod
    def _extract_price(text: str) -> Optional[float]:
        """Extract the first dollar amount from message text."""
        m = re.search(r'\$\s*([\d,]+\.?\d*)', text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_embeds(embeds: list) -> list[dict]:
        """Extract useful info from Discord embeds including fields."""
        results = []
        for e in embeds[:3]:
            info = {}
            if e.get("title"):
                info["title"] = e["title"][:100]
            if e.get("description"):
                info["description"] = e["description"][:200]
            if e.get("url"):
                info["url"] = e["url"]
            if e.get("thumbnail", {}).get("url"):
                info["image"] = e["thumbnail"]["url"]
            elif e.get("image", {}).get("url"):
                info["image"] = e["image"]["url"]
            # Extract key fields (Price, Type, Seller, ASIN)
            fields = {}
            for f in e.get("fields", []):
                name = f.get("name", "").strip()
                value = f.get("value", "").strip()
                if name and value and name not in ("** **", ""):
                    fields[name] = value
            if fields:
                info["fields"] = fields
            if info:
                results.append(info)
        return results
