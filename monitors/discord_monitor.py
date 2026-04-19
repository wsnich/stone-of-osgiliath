"""
Discord channel monitor via REST API.

Polls configured channels for new messages matching keywords.
Uses a Discord user token (Authorization header) — no bot or
library required. Compatible with any Python version.

Config:
  discord.enabled: true
  discord.token: "YOUR_USER_TOKEN"
  discord.channels_to_monitor: ["channel_id_1", "channel_id_2"]
  discord.keywords: ["strixhaven", "collector booster"]
"""

import logging
import re
from typing import Optional

log = logging.getLogger("discord_mon")

_API_BASE = "https://discord.com/api/v10"


class DiscordMonitor:

    def __init__(self):
        self._seen_ids: set[str] = set()
        self._initialized = False
        self._channel_names: dict[str, str] = {}  # channel_id -> name

    async def resolve_channel_name(self, token: str, channel_id: str) -> str:
        """Fetch channel name from Discord API. Caches results."""
        if channel_id in self._channel_names:
            return self._channel_names[channel_id]
        import aiohttp
        url = f"{_API_BASE}/channels/{channel_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": token}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        name = data.get("name", f"#{channel_id}")
                        self._channel_names[channel_id] = name
                        return name
        except Exception:
            pass
        self._channel_names[channel_id] = f"#{channel_id[-4:]}"
        return self._channel_names[channel_id]

    async def poll(self, config: dict) -> list[dict]:
        """
        Poll all configured channels for new messages.
        Returns list of new matching messages:
          [{id, channel_id, author, content, timestamp, matched_keywords, price}]
        """
        dc = config.get("discord", {})
        if not dc.get("enabled"):
            return []

        token    = dc.get("token", "")
        channels = dc.get("channels_to_monitor", [])
        ignored_patterns = [p.lower() for p in dc.get("ignored_patterns", [])]
        blocked_retailers = [r.lower() for r in dc.get("blocked_retailers", [])]

        # Build keywords: config keywords + auto-generated from products
        disabled = set(k.lower() for k in dc.get("disabled_keywords", []))
        keywords = [k.lower() for k in dc.get("keywords", []) if k.lower() not in disabled]
        for p in config.get("products", []):
            tags = p.get("tags", {})
            cat = tags.get("category", "")
            if cat in ("single", "comic"):
                continue
            s = tags.get("set", "")
            pt = tags.get("product_type", "")
            if s and s.lower() not in keywords and s.lower() not in disabled:
                keywords.append(s.lower())
            if pt and pt.lower() not in keywords and pt.lower() not in disabled:
                keywords.append(pt.lower())

        if not token or not channels:
            return []

        new_messages = []
        filtered_messages = []  # all messages with action/reason for audit log

        # Resolve channel names on first run
        for ch_id in channels:
            await self.resolve_channel_name(token, str(ch_id))

        for ch_id in channels:
            msgs = await self._fetch_messages(token, str(ch_id), limit=25)
            for msg in msgs:
                mid = msg.get("id", "")
                if mid in self._seen_ids:
                    continue
                self._seen_ids.add(mid)

                if not self._initialized:
                    continue

                content = msg.get("content", "")
                content_lower = content.lower()
                author = msg.get("author", {})

                embed_text = ""
                embed_title = ""
                embed_fields_str = ""
                for e in msg.get("embeds", []):
                    embed_text += " " + (e.get("title", "") + " " + e.get("description", "")).lower()
                    if e.get("title"):
                        embed_title = e["title"][:100]
                    for f in e.get("fields", []):
                        fname = f.get("name", "")
                        fval  = f.get("value", "")
                        embed_fields_str += f"{fname}: {fval}\n"
                        # Include field values in keyword search text
                        embed_text += " " + fval.lower()

                full_text = content_lower + embed_text

                # Extract price
                price = self._extract_price(content)
                if price is None:
                    for e in msg.get("embeds", []):
                        for f in e.get("fields", []):
                            if f.get("name", "").strip().lower() == "price":
                                price = self._extract_price(f.get("value", ""))
                                if price:
                                    break
                        if price:
                            break

                # Build base log entry
                log_entry = {
                    "msg_id": mid, "channel_id": ch_id,
                    "author": author.get("username", "unknown"),
                    "content": content[:500], "embed_title": embed_title,
                    "embed_fields": embed_fields_str[:500], "price": price,
                    "timestamp": msg.get("timestamp", ""),
                }

                # Check ignored patterns
                matched_ignore = [p for p in ignored_patterns if p in full_text]
                if matched_ignore:
                    filtered_messages.append({**log_entry, "action": "filtered", "reason": f"ignored pattern: {matched_ignore[0]}"})
                    continue

                # Check blocked retailers (by URL domain in embeds)
                if blocked_retailers:
                    all_urls = embed_text + " " + embed_fields_str.lower()
                    blocked_match = [r for r in blocked_retailers if r in all_urls]
                    if blocked_match:
                        filtered_messages.append({**log_entry, "action": "filtered", "reason": f"blocked retailer: {blocked_match[0]}"})
                        continue

                # Check keyword matches
                if keywords:
                    matched = [kw for kw in keywords if kw in full_text]
                    if not matched:
                        filtered_messages.append({**log_entry, "action": "filtered", "reason": "no keyword match"})
                        continue
                else:
                    matched = []

                # Price floor filter
                min_price = dc.get("min_price", 0)
                if min_price and price is not None and price < min_price:
                    filtered_messages.append({**log_entry, "action": "filtered", "reason": f"price ${price:.2f} < min ${min_price}"})
                    continue
                if min_price and price is None:
                    filtered_messages.append({**log_entry, "action": "filtered", "reason": "no price detected"})
                    continue

                # Passed all filters
                filtered_messages.append({**log_entry, "action": "shown", "reason": f"matched: {', '.join(matched) if matched else 'all'}"})

                new_messages.append({
                    "id": mid,
                    "channel_id": ch_id,
                    "channel_name": self._channel_names.get(str(ch_id), ""),
                    "author": author.get("username", "unknown"),
                    "avatar": author.get("avatar", ""),
                    "author_id": author.get("id", ""),
                    "content": content[:500],
                    "timestamp": msg.get("timestamp", ""),
                    "matched_keywords": matched,
                    "price": price,
                    "embeds": self._extract_embeds(msg.get("embeds", [])),
                })

        self._audit_log = filtered_messages  # expose for the poll loop to save

        if not self._initialized:
            self._initialized = True
            log.info(f"Discord monitor initialized — tracking {len(self._seen_ids)} existing messages across {len(channels)} channels")

        return new_messages

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

    async def _fetch_messages(self, token: str, channel_id: str, limit: int = 25) -> list[dict]:
        """Fetch recent messages from a Discord channel via REST API."""
        import aiohttp
        url = f"{_API_BASE}/channels/{channel_id}/messages?limit={limit}"
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 401:
                        log.error("Discord: invalid token (401)")
                    elif resp.status == 403:
                        log.warning(f"Discord: no access to channel {channel_id} (403)")
                    elif resp.status == 429:
                        data = await resp.json()
                        retry = data.get("retry_after", 5)
                        log.warning(f"Discord: rate limited — retry in {retry}s")
                    else:
                        log.debug(f"Discord: channel {channel_id} returned {resp.status}")
        except Exception as e:
            log.debug(f"Discord fetch error: {e}")
        return []

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
