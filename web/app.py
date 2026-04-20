"""
FastAPI web application — serves the UI and REST/WebSocket API.

Monitor loop uses a per-product scheduler:
  - Each product has its own next_check_at timestamp
  - Products with check_interval_seconds > 0 use that instead of the global interval
  - A schedule gate (start/end hours) blocks checks outside active hours
"""

import asyncio
import json
import os
import random
import re
import sys
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

from web.state import app_state, deal_tracker, product_hub
from monitors.tcgplayer_monitor import TCGPlayerMonitor
from monitors.ebay_monitor import EbayMonitor
from monitors.discord_monitor import DiscordMonitor
from monitors.marketplace_monitor import (
    parse_intent, is_noise, extract_prices, match_to_products, MarketplaceListing
)
from monitors.discord_gateway import DiscordGatewayMonitor
import db as price_db

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_tcgplayer = TCGPlayerMonitor()
_ebay      = EbayMonitor()
_discord   = DiscordMonitor()
_discord_gw = DiscordGatewayMonitor()

# Reddit poller state
_seen_reddit_ids: set[str] = set()
_reddit_initialized: bool  = False

# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

# ------------------------------------------------------------------
# Schedule helpers
# ------------------------------------------------------------------

def check_schedule(schedule: dict) -> tuple[bool, int, str]:
    """
    Returns (is_active, seconds_to_wait, display_label).
    When disabled or active: (True, 0, "").
    When sleeping:           (False, N, "Sleeping until HH:MM (Xh Ym)").
    """
    if not schedule.get("enabled", False):
        return True, 0, ""

    start_str = schedule.get("start", "07:00")
    end_str   = schedule.get("end",   "23:00")

    try:
        now   = datetime.now()
        today = now.date()
        start = datetime.combine(today, datetime.strptime(start_str, "%H:%M").time())
        end   = datetime.combine(today, datetime.strptime(end_str,   "%H:%M").time())
    except ValueError:
        return True, 0, ""  # bad config — don't block

    if start <= now <= end:
        return True, 0, ""

    next_start = start if now < start else start + timedelta(days=1)
    wait_secs  = int((next_start - now).total_seconds())
    h, rem     = divmod(wait_secs, 3600)
    m          = rem // 60
    label      = f"Sleeping until {start_str}"
    label     += f" ({h}h {m}m)" if h else f" ({m}m)"
    return False, wait_secs, label

# ------------------------------------------------------------------
# Reddit feed poller
# ------------------------------------------------------------------

async def reddit_poll_loop():
    """
    Polls configured subreddits for new posts.
    Reads subreddit list from config.reddit.subreddits (default: ["sealedmtgdeals"]).
    """
    import aiohttp

    global _seen_reddit_ids, _reddit_initialized

    headers = {"User-Agent": "StoneOfOsgiliath/1.0 (deal monitoring)"}

    await asyncio.sleep(5)
    await app_state.log("info", "Reddit feed starting...", "reddit")

    while True:
        try:
            config = load_config()
            reddit_cfg = config.get("reddit", {})
            subreddits = reddit_cfg.get("subreddits", ["sealedmtgdeals"])
            if not subreddits:
                await asyncio.sleep(60)
                continue

            async with aiohttp.ClientSession() as session:
                for subreddit in subreddits:
                    subreddit = subreddit.strip().lower()
                    if not subreddit:
                        continue
                    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                children = data.get("data", {}).get("children", [])

                                if not _reddit_initialized:
                                    for child in children:
                                        p = child["data"]
                                        _seen_reddit_ids.add(p["id"])
                                        app_state.reddit_posts.append(_make_reddit_entry(p, subreddit, is_new=False))
                                    await app_state.log("info", f"Loaded {len(children)} posts from r/{subreddit}", "reddit")
                                else:
                                    new_posts = []
                                    for child in children:
                                        p = child["data"]
                                        if p["id"] not in _seen_reddit_ids:
                                            _seen_reddit_ids.add(p["id"])
                                            entry = _make_reddit_entry(p, subreddit, is_new=True)
                                            app_state.reddit_posts.appendleft(entry)
                                            new_posts.append(entry)

                                    for entry in reversed(new_posts):
                                        await app_state.ws.broadcast({"type": "reddit_post", "data": entry})

                                    if new_posts:
                                        await app_state.log("info", f"{len(new_posts)} new post(s) in r/{subreddit}", "reddit")
                            elif resp.status == 403:
                                await app_state.log("warn", f"r/{subreddit}: access denied (private?)", "reddit")
                            elif resp.status != 429:
                                await app_state.log("warn", f"r/{subreddit}: HTTP {resp.status}", "reddit")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        await app_state.log("warn", f"r/{subreddit}: {e}", "reddit")

            if not _reddit_initialized:
                _reddit_initialized = True
                await app_state.ws.broadcast(app_state.snapshot())

        except asyncio.CancelledError:
            raise
        except Exception as e:
            await app_state.log("warn", f"Reddit poll error: {e}", "reddit")

        poll_interval = config.get("reddit", {}).get("poll_interval_seconds", 60)
        await asyncio.sleep(max(30, poll_interval))


def _make_reddit_entry(p: dict, subreddit: str, is_new: bool) -> dict:
    return {
        "id":           p["id"],
        "title":        p.get("title", ""),
        "author":       p.get("author", "[deleted]"),
        "subreddit":    subreddit,
        "score":        p.get("score", 0),
        "url":          p.get("url", ""),
        "permalink":    "https://www.reddit.com" + p.get("permalink", ""),
        "created_utc":  p.get("created_utc", 0),
        "flair":        p.get("link_flair_text") or "",
        "num_comments": p.get("num_comments", 0),
        "is_new":       is_new,
    }


# ------------------------------------------------------------------
# Discord Gateway monitor
# ------------------------------------------------------------------


async def discord_gateway_loop():
    """Listens to Discord Gateway via browser WebSocket interception."""
    await asyncio.sleep(5)

    config = load_config()
    dc = config.get("discord", {})
    if not dc.get("enabled"):
        return

    stealth_cfg = config.get("stealth", {})

    # Configure data dir for session persistence
    data_dir = config.get("data_dir")
    if data_dir:
        _discord_gw.set_data_dir(Path(data_dir))

    await app_state.log("info", "Discord Gateway monitor starting...", "discord")
    await app_state.ws.broadcast({"type": "discord_gateway_status", "data": {"state": "starting"}})

    ok = await _discord_gw.start(config, stealth_cfg)
    if not ok:
        state = _discord_gw.login_state
        await app_state.ws.broadcast({"type": "discord_gateway_status", "data": {
            "state": state, "error": _discord_gw.error_message,
        }})
        if state == "awaiting_2fa":
            await app_state.log("info", "Discord login requires 2FA — enter code in Settings", "discord")
            # Wait for 2FA to complete
            while _discord_gw.login_state == "awaiting_2fa":
                await asyncio.sleep(1)
            if _discord_gw.login_state != "logged_in":
                await app_state.log("error", f"Discord 2FA failed: {_discord_gw.error_message}", "discord")
                return
        else:
            await app_state.log("error", f"Discord Gateway failed: {_discord_gw.error_message}", "discord")
            return

    await app_state.ws.broadcast({"type": "discord_gateway_status", "data": {"state": "connected"}})
    await app_state.log("info", "Discord Gateway connected — receiving messages in real-time", "discord")

    # Share channel names with the REST monitor for backward compat
    _discord._channel_names.update(_discord_gw.channel_names)

    _initialized = False
    _seen_ids: set[str] = set()

    while True:
        try:
            config = load_config()
            _discord_gw.update_channels(config)

            await _discord_gw.poll_gateway_messages()
            raw_messages = await _discord_gw.drain_discord_queue()

            # On first batch, just mark seen IDs without processing
            if not _initialized:
                for msg in raw_messages:
                    _seen_ids.add(str(msg.get("id", "")))
                _initialized = True
                if raw_messages:
                    await app_state.log("info", f"Discord Gateway initialized — {len(_seen_ids)} existing messages", "discord")
                await asyncio.sleep(3)
                continue

            deals_changed = False
            audit_entries = []

            for msg in raw_messages:
                mid = str(msg.get("id", ""))
                if mid in _seen_ids:
                    continue
                _seen_ids.add(mid)

                # Use shared filtering pipeline
                _discord._channel_names.update(_discord_gw.channel_names)
                try:
                    result, audit = _discord.filter_message(msg, config)
                except Exception as e:
                    print(f"  [Discord GW] Filter error: {e}")
                    continue
                # Patch channel name from Gateway cache
                if result and not result.get("channel_name"):
                    result["channel_name"] = _discord_gw.channel_names.get(str(msg.get("channel_id", "")), "")
                audit_entries.append(audit)

                if not result:
                    continue

                entry = {
                    "id":       result["id"],
                    "author":   result["author"],
                    "content":  result["content"],
                    "timestamp": result["timestamp"],
                    "keywords": result.get("matched_keywords", []),
                    "price":    result.get("price"),
                    "embeds":   result.get("embeds", []),
                    "channel_name": result.get("channel_name", ""),
                    "is_new":   True,
                }
                app_state.discord_posts.appendleft(entry)
                await app_state.ws.broadcast({"type": "discord_post", "data": entry})

                # Ingest into deal tracker
                tracked = deal_tracker.ingest(entry)
                if tracked:
                    deals_changed = True
                    await app_state.ws.broadcast({
                        "type": "tracked_deal_update",
                        "data": tracked.to_dict(),
                    })

                    # DM notification
                    dm_user = config.get("discord", {}).get("dm_user_id")
                    bot_token = config.get("discord", {}).get("bot_token", "")
                    dm_token = f"Bot {bot_token}" if bot_token else ""
                    if dm_user and dm_token:
                        title = entry.get("embeds", [{}])[0].get("title", "") if entry.get("embeds") else entry.get("content", "")[:80]
                        price = entry.get("price")
                        price_str = f"${price:.2f}" if price else "no price"
                        score_str = ""
                        if price:
                            from web.state import _normalize_name, _tokenize, _jaccard
                            title_norm = _normalize_name(title)
                            title_tokens = _tokenize(title_norm)
                            best_match = None
                            best_score = 0.0
                            for ps in app_state.product_statuses:
                                if ps.site != "tcgplayer" or ps.price is None:
                                    continue
                                ps_tokens = _tokenize(_normalize_name(ps.name))
                                sc = _jaccard(title_tokens, ps_tokens)
                                if sc > best_score and sc >= 0.35:
                                    best_score = sc
                                    best_match = ps
                            if best_match:
                                pct = ((price - best_match.price) / best_match.price) * 100
                                score_str = f" ({pct:+.0f}% vs TCG ${best_match.price:.2f})"

                        url = ""
                        for e in entry.get("embeds", []):
                            if e.get("url"):
                                url = e["url"]
                                break

                        dm_msg = f"🔔 **Deal Alert**\n{title}\n**{price_str}**{score_str}"
                        if url:
                            dm_msg += f"\n🔗 {url}"
                        from web.state import _extract_checkout_links
                        cart_links = _extract_checkout_links(entry)
                        for cl in cart_links[:3]:
                            dm_msg += f"\n🛒 [{cl['label']}]({cl['url']})"
                        await _discord.send_dm(dm_token, dm_user, dm_msg)

                kw_str = ", ".join(result.get("matched_keywords", []))
                price_str = f" — ${result['price']:.2f}" if result.get("price") else ""
                await app_state.log("info",
                    f"[{result['author']}] {result['content'][:80]}{price_str} ({kw_str})", "discord")

            if deals_changed:
                deal_tracker.save_to_disk()
                from web.state import _normalize_name, _tokenize, _jaccard
                hub_changed = False
                for deal in deal_tracker.deals:
                    if any(deal.id in e.deal_ids for e in product_hub.entries):
                        continue
                    best_entry = None
                    best_score = 0.0
                    for pe in product_hub.entries:
                        entry_tokens = _tokenize(_normalize_name(pe.name))
                        if not entry_tokens:
                            continue
                        score = _jaccard(deal.tokens, entry_tokens)
                        if score > best_score:
                            best_score = score
                            best_entry = pe
                    if best_entry and best_score >= 0.55:
                        best_entry.deal_ids.append(deal.id)
                        hub_changed = True
                if hub_changed:
                    product_hub.save_to_disk()

            # Save audit log + retailer intelligence
            for audit in audit_entries:
                try:
                    await price_db.log_discord_message(
                        msg_id=audit["msg_id"], channel_id=audit["channel_id"],
                        author=audit["author"], content=audit["content"],
                        embed_title=audit.get("embed_title", ""),
                        embed_fields=audit.get("embed_fields", ""),
                        price=audit.get("price"),
                        action=audit["action"], reason=audit["reason"],
                        timestamp=audit.get("timestamp", ""),
                    )
                except Exception:
                    pass
                try:
                    await _ingest_retailer_sighting(audit)
                except Exception:
                    pass

            # Health check
            health = await _discord_gw.check_health()
            if not health:
                print(f"  [Discord GW] Health check FAILED: running={_discord_gw._running}, browser={bool(_discord_gw._browser)}, activity={_discord_gw._last_ws_activity}")
                await app_state.log("warn", "Discord Gateway unhealthy — restarting...", "discord")
                await app_state.ws.broadcast({"type": "discord_gateway_status", "data": {"state": "reconnecting"}})
                ok = await _discord_gw.restart(config, stealth_cfg)
                if ok:
                    await app_state.ws.broadcast({"type": "discord_gateway_status", "data": {"state": "connected"}})
                    await app_state.log("info", "Discord Gateway reconnected", "discord")
                else:
                    await app_state.log("error", "Discord Gateway restart failed", "discord")
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            await _discord_gw.stop()
            raise
        except Exception as e:
            print(f"  [Discord GW] Loop error: {e}")
            await app_state.log("warn", f"Discord gateway error: {e}", "discord")

        await asyncio.sleep(3)


async def marketplace_gateway_loop():
    """Processes marketplace messages from the Gateway queue."""
    await asyncio.sleep(10)

    _seen_ids: set[str] = set()
    _initialized = False

    while True:
        try:
            config = load_config()
            mp = config.get("marketplace", {})
            if not mp.get("enabled") or not _discord_gw.connected:
                await asyncio.sleep(5)
                continue

            raw_messages = await _discord_gw.drain_marketplace_queue()

            if not _initialized:
                for msg in raw_messages:
                    _seen_ids.add(str(msg.get("id", "")))
                _initialized = True
                await asyncio.sleep(3)
                continue

            for msg in raw_messages:
                mid = str(msg.get("id", ""))
                if mid in _seen_ids:
                    continue
                _seen_ids.add(mid)

                channel_id = str(msg.get("channel_id", ""))
                content = msg.get("content", "")
                author = msg.get("author", {})

                if not content or is_noise(content):
                    continue

                sell_channels = set(str(c) for c in mp.get("sell_channels", []))
                buy_channels = set(str(c) for c in mp.get("buy_channels", []))

                if channel_id in sell_channels:
                    intent = parse_intent(content) if parse_intent(content) != "WTB" else "WTS"
                elif channel_id in buy_channels:
                    intent = "WTB"
                else:
                    continue

                items = extract_prices(content)
                if not items:
                    continue

                # Match against tracked products
                product_names = [
                    {"index": i, "name": ps.name,
                     "words": set(re.findall(r'\w{3,}', ps.name.lower()))}
                    for i, ps in enumerate(app_state.product_statuses)
                    if ps.site == "tcgplayer"
                ]
                matched = match_to_products(items, product_names)

                guild_id = msg.get("guild_id", "")
                jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{mid}" if guild_id else ""

                listing = MarketplaceListing(
                    seller=author.get("username", "unknown") if isinstance(author, dict) else str(author),
                    seller_id=author.get("id", "") if isinstance(author, dict) else "",
                    intent=intent,
                    raw_text=content[:300],
                    items=items,
                    msg_id=mid,
                    channel_id=channel_id,
                    timestamp=msg.get("timestamp", ""),
                    jump_url=jump_url,
                )

                import json as _json
                await price_db.record_marketplace_message(
                    msg_id=mid, channel_id=channel_id,
                    seller=listing.seller, seller_id=listing.seller_id,
                    intent=intent, raw_text=content[:1000],
                    items_json=_json.dumps(items),
                    matched_json=_json.dumps(matched),
                    timestamp=msg.get("timestamp", ""),
                )

                if matched:
                    listing.items = matched
                    await app_state.ws.broadcast({
                        "type": "marketplace_listing",
                        "data": listing.to_dict(),
                    })

                    # DM notification for below-market deals
                    dm_user = config.get("discord", {}).get("dm_user_id")
                    bot_token = config.get("discord", {}).get("bot_token", "")
                    dm_token = f"Bot {bot_token}" if bot_token else ""
                    if dm_user and dm_token and intent == "WTS":
                        for item in matched:
                            pi = item.get("product_index")
                            if pi is not None and pi < len(app_state.product_statuses):
                                ps = app_state.product_statuses[pi]
                                if ps.price and item["price"] < ps.price * 0.85:
                                    pct = ((item["price"] - ps.price) / ps.price) * 100
                                    dm_msg = (
                                        f"🏪 **Marketplace Deal**\n"
                                        f"**{item.get('product_name', item['name'])}**\n"
                                        f"Seller: @{listing.seller} — **${item['price']:.2f}** "
                                        f"({pct:+.0f}% vs TCG ${ps.price:.2f})\n"
                                        f"[Jump to message]({jump_url})"
                                    )
                                    await _discord.send_dm(dm_token, dm_user, dm_msg)

                    await app_state.log("info",
                        f"Marketplace: @{listing.seller} {intent} {len(matched)} matched item(s)",
                        "marketplace")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug(f"Marketplace gateway error: {e}")

        await asyncio.sleep(3)


# ------------------------------------------------------------------
# Per-product scheduler loop
# ------------------------------------------------------------------

async def monitor_loop():
    app_state.monitor_running = True
    app_state.sleeping        = False
    next_check_at: dict[int, datetime] = {}

    await app_state.log("info", "Monitor started")

    while app_state.monitor_running:
        config      = load_config()
        global_iv   = config.get("check_interval_seconds", 90)
        stealth_cfg = config.get("stealth", {})
        jitter_pct  = stealth_cfg.get("jitter_pct", 20)
        schedule    = config.get("schedule", {})

        # ── Schedule gate ──────────────────────────────────────────
        active, wait_secs, sleep_label = check_schedule(schedule)
        if not active:
            if not app_state.sleeping:
                app_state.sleeping       = True
                app_state.sleeping_label = sleep_label
                await app_state.log("info", sleep_label, "schedule")
                await app_state.ws.broadcast({
                    "type": "sleeping",
                    "data": {"label": sleep_label, "seconds": wait_secs},
                })

            # Sleep in 60s chunks so stop signal is responsive
            await asyncio.sleep(min(60, wait_secs))
            continue

        # Coming back from sleep
        if app_state.sleeping:
            app_state.sleeping       = False
            app_state.sleeping_label = ""
            await app_state.log("info", "Back in check window — resuming", "schedule")
            await app_state.ws.broadcast({"type": "monitor_status", "data": {"running": True, "sleeping": False}})
            # Force all products to check immediately after waking
            next_check_at.clear()

        # ── Initialise next_check_at for new products ──────────────
        now = datetime.now()
        _SINGLES_IV = 6 * 3600
        _GRADED_IV = config.get("graded_interval_seconds", 7 * 86400)
        for i in range(len(app_state.product_statuses)):
            if i not in next_check_at:
                ps = app_state.product_statuses[i]
                cat = ps.tags.get("category", "")
                is_slow = cat in ("single", "comic")

                # For singles/graded: respect the last check time from persisted state
                if is_slow and ps.last_checked:
                    try:
                        last = datetime.strptime(ps.last_checked, "%Y-%m-%d %H:%M:%S")
                        default_iv = _GRADED_IV if cat == "comic" else _SINGLES_IV
                        interval = ps.check_interval or default_iv
                        due_at = last + timedelta(seconds=interval)
                        if due_at > now:
                            next_check_at[i] = due_at
                            continue
                    except Exception:
                        pass

                next_check_at[i] = now  # check immediately

        # ── Force refresh categories if requested (runs AFTER init so it wins) ──
        if app_state.force_refresh_categories:
            cats = app_state.force_refresh_categories.copy()
            app_state.force_refresh_categories.clear()
            now_f = datetime.now()
            for i, ps in enumerate(app_state.product_statuses):
                for cat in cats:
                    if cat == "tcgplayer" and ps.site == "tcgplayer":
                        next_check_at[i] = now_f
                    elif ps.tags.get("category") == cat:
                        next_check_at[i] = now_f

        # ── Check products that are due (parallel) ──────────────────
        _check_sem = asyncio.Semaphore(4)  # max 4 concurrent checks

        async def _check_one(i: int, ps):
            async with _check_sem:
                if not app_state.monitor_running:
                    return

                await app_state.update_product(i, checking=True, error=None)
                await app_state.log("info", f"Checking: {ps.name}", ps.site)

                try:
                    cfg = load_config()
                    if i >= len(cfg.get("products", [])):
                        await app_state.update_product(i, checking=False, error="Config out of sync")
                        return
                    product_cfg = cfg["products"][i]

                    # eBay-only items (comics): skip product check, just run eBay search
                    if ps.site == "ebay":
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        try:
                            ebay_result = await _ebay.check_single(product_cfg, stealth_cfg)
                            if ebay_result.count > 0:
                                # Save image from first listing if we don't have one
                                if ebay_result.image_url and not ps.image_url:
                                    local = await cache_image(ebay_result.image_url)
                                    img_url = local or ebay_result.image_url
                                    await app_state.update_product(i, image_url=img_url)
                                    try:
                                        cfg2 = load_config()
                                        if i < len(cfg2["products"]):
                                            cfg2["products"][i]["image_url"] = img_url
                                            save_config(cfg2)
                                    except Exception:
                                        pass

                                # Apply learned ignore patterns to new sales + live
                                sales = ebay_result.sales
                                live  = ebay_result.live
                                await app_state.update_product(i,
                                    checking=False, last_checked=ts,
                                    ebay_sales=sales,
                                    ebay_live=live,
                                )
                                _apply_ignore_patterns(ps)
                                _recompute_ebay_aggregates(ps)
                                await app_state.update_product(i,
                                    ebay_median=ps.ebay_median,
                                    ebay_avg=ps.ebay_avg,
                                    ebay_low=ps.ebay_low,
                                    ebay_high=ps.ebay_high,
                                    ebay_sold_count=ps.ebay_sold_count,
                                    ebay_by_grade=ps.ebay_by_grade,
                                    ebay_sales=ps.ebay_sales,
                                )
                                await price_db.record_ebay_sold(
                                    product_name=ps.name, url=ps.url or ps.name,
                                    median_price=ps.ebay_median, avg_price=ps.ebay_avg,
                                    low_price=ps.ebay_low, high_price=ps.ebay_high,
                                    sold_count=ps.ebay_sold_count, checked_at=ts,
                                )
                                await app_state.log("info",
                                    f"{ps.name} — eBay median ${ps.ebay_median:.2f} ({ps.ebay_sold_count} sold)" if ps.ebay_median else f"{ps.name} — eBay {len(sales)} listings (all ignored)", "ebay")
                            else:
                                await app_state.update_product(i, checking=False, last_checked=ts,
                                                               error=None)
                                await app_state.log("info", f"{ps.name} — no sold data found", "130point")
                        except Exception as e:
                            await app_state.update_product(i, checking=False, error=str(e))
                            await app_state.log("error", f"{ps.name}: {e}", "ebay")

                        is_comic = ps.tags.get("category") == "comic"
                        _GRADED_IV_L = config.get("graded_interval_seconds", 7 * 86400)
                        effective_iv = ps.check_interval or (_GRADED_IV_L if is_comic else global_iv)
                        _schedule_next(next_check_at, i, effective_iv, jitter_pct)
                        app_state.save_to_disk()
                        return

                    monitors = {
                        "tcgplayer": _tcgplayer,
                    }
                    mon = monitors.get(ps.site)
                    if not mon:
                        await app_state.log("warn", f"Unknown site: {ps.site}")
                        await app_state.update_product(i, checking=False)
                        _schedule_next(next_check_at, i, ps.check_interval or global_iv, jitter_pct)
                        return

                    result = await mon.check_product(product_cfg, stealth_cfg)
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    await price_db.record_check(
                        product_name=ps.name, url=ps.url, site=ps.site,
                        price=result.price, available=result.available,
                        blocked=result.blocked, checked_at=ts,
                    )

                    if ps.site == "tcgplayer" and (result.price is not None or result.low_price is not None):
                        # listings = number of sellers, quantity = total copies available
                        # listing_prices has per-listing qty data; tcg_quantity is listing count from DOM
                        lp = result.listing_prices or []

                        # Pagination sanity check: detect partial captures where the
                        # browser only got some pages of listings. Compare against
                        # DOM-reported quantity AND previous listing count.
                        prev_lp = ps.listing_prices or []
                        new_count = len(lp)
                        prev_count = len(prev_lp)
                        dom_qty = result.tcg_quantity or 0  # "N Listings" from DOM header

                        # The DOM quantity is the most reliable count — if we captured
                        # significantly fewer listings than the DOM reports, it's a
                        # partial capture. Also catch sudden drops vs previous.
                        is_partial = False
                        if dom_qty > 15 and new_count < dom_qty * 0.7:
                            is_partial = True  # got less than 70% of DOM-reported listings
                        elif prev_count > 15 and new_count < prev_count * 0.6:
                            is_partial = True  # dropped more than 40% from previous

                        if is_partial and prev_count > new_count:
                            await app_state.log("warn",
                                f"{ps.name}: partial listing capture ({new_count} vs DOM {dom_qty}, prev {prev_count}) — keeping previous",
                                "tcgplayer")
                            lp = prev_lp
                            result.listing_prices = prev_lp
                            if ps.tcg_low_price is not None:
                                result.low_price = ps.tcg_low_price

                        total_qty = sum(l.get("qty", 1) for l in lp if isinstance(l, dict)) if lp else result.tcg_quantity
                        listing_json = json.dumps(lp) if lp else None
                        await price_db.record_tcg_check(
                            product_name=ps.name, url=ps.url,
                            market_price=result.price, low_price=result.low_price,
                            quantity=total_qty, listings=result.tcg_quantity,
                            checked_at=ts, listing_prices_json=listing_json,
                        )

                    # eBay sold search for all TCGPlayer items (singles + sealed)
                    is_single = ps.tags.get("category") == "single"
                    is_comic  = ps.tags.get("category") == "comic"
                    if ps.site == "tcgplayer":
                        try:
                            ebay_result = await _ebay.check_single(cfg["products"][i], stealth_cfg)
                            if ebay_result.count > 0:
                                await app_state.update_product(i,
                                    ebay_sales=ebay_result.sales,
                                )
                                # Re-apply ignore patterns to fresh sales
                                _apply_ignore_patterns(ps)
                                _recompute_ebay_aggregates(ps)
                                await app_state.update_product(i,
                                    ebay_median=ps.ebay_median,
                                    ebay_avg=ps.ebay_avg,
                                    ebay_low=ps.ebay_low,
                                    ebay_high=ps.ebay_high,
                                    ebay_sold_count=ps.ebay_sold_count,
                                    ebay_by_grade=ps.ebay_by_grade,
                                    ebay_sales=ps.ebay_sales,
                                )
                                await price_db.record_ebay_sold(
                                    product_name=ps.name, url=ps.url or ps.name,
                                    median_price=ps.ebay_median, avg_price=ps.ebay_avg,
                                    low_price=ps.ebay_low, high_price=ps.ebay_high,
                                    sold_count=ps.ebay_sold_count, checked_at=ts,
                                )
                                await app_state.log("info",
                                    f"{ps.name} — eBay median ${ps.ebay_median:.2f} ({ps.ebay_sold_count} sold)" if ps.ebay_median else f"{ps.name} — eBay {ebay_result.count} listings (all ignored)",
                                    "ebay")
                        except Exception as e:
                            log.debug(f"eBay check error for {ps.name}: {e}")

                    _SINGLES_IV  = 6 * 3600
                    _GRADED_IV_L = config.get("graded_interval_seconds", 7 * 86400)
                    effective_iv = ps.check_interval or (_GRADED_IV_L if is_comic else _SINGLES_IV if is_single else global_iv)
                    next_secs    = _schedule_next(next_check_at, i, effective_iv, jitter_pct)

                    update_kwargs = dict(
                        price=result.price, available=result.available,
                        last_checked=ts, checking=False,
                        error=result.error,
                        next_check_in=next_secs,
                    )
                    if result.image_url:
                        # Download and cache locally
                        local = await cache_image(result.image_url)
                        img_url = local or result.image_url
                        update_kwargs["image_url"] = img_url
                        if i < len(cfg["products"]) and not cfg["products"][i].get("image_url"):
                            cfg["products"][i]["image_url"] = img_url
                            save_config(cfg)
                    if result.low_price is not None:
                        update_kwargs["tcg_low_price"] = result.low_price
                    if result.tcg_quantity is not None:
                        update_kwargs["tcg_quantity"] = result.tcg_quantity
                    if result.listing_prices:
                        update_kwargs["listing_prices"] = result.listing_prices
                        # Compute 5th percentile market low to exclude outliers
                        update_kwargs["tcg_market_low"] = _compute_market_low(
                            result.listing_prices, result.low_price
                        )
                    if result.tcg_sales:
                        update_kwargs["tcg_sales"] = result.tcg_sales
                    if result.tcg_price_history:
                        update_kwargs["tcg_price_history"] = result.tcg_price_history
                    await app_state.update_product(i, **update_kwargs)
                    app_state.save_to_disk()

                    if result.price:
                        low_str = f" · low ${result.low_price:.2f}" if result.low_price else ""
                        qty_str = f" · {result.tcg_quantity} listings" if result.tcg_quantity else ""
                        await app_state.log("info", f"{ps.name} — market ${result.price:.2f}{low_str}{qty_str}", ps.site)
                    else:
                        await app_state.log("warn", f"{ps.name} — could not read price", ps.site)

                except Exception as e:
                    await app_state.update_product(i, checking=False, error=str(e))
                    await app_state.log("error", f"{ps.name}: {e}", ps.site)
                    _schedule_next(next_check_at, i, ps.check_interval or global_iv, jitter_pct)

        # Gather all due checks and run them concurrently
        due = [
            (i, ps) for i, ps in enumerate(app_state.product_statuses)
            if ps.enabled
            and datetime.now() >= next_check_at.get(i, datetime.min)
        ]
        if due:
            await asyncio.gather(*[_check_one(i, ps) for i, ps in due])

        # ── Sleep until the soonest due product ────────────────────
        now = datetime.now()
        enabled_due = [
            next_check_at[i]
            for i, ps in enumerate(app_state.product_statuses)
            if ps.enabled and i in next_check_at
        ]
        if enabled_due:
            secs_to_next = max(1, int((min(enabled_due) - now).total_seconds()))
        else:
            secs_to_next = global_iv

        await app_state.ws.broadcast({"type": "next_check", "data": {"seconds": secs_to_next}})
        # Wake at least every 60s to re-check the schedule
        await asyncio.sleep(min(secs_to_next, 60))

    await app_state.log("info", "Monitor stopped")


def _schedule_next(next_check_at: dict, index: int, interval: int, jitter_pct: int) -> int:
    """Compute and store next_check_at for a product; return the actual seconds."""
    jitter   = interval * random.uniform(-jitter_pct / 100, jitter_pct / 100)
    secs     = max(30, int(interval + jitter))
    next_check_at[index] = datetime.now() + timedelta(seconds=secs)
    return secs

# ------------------------------------------------------------------
# Lifespan
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    # Configure data directory if specified
    data_dir = config.get("data_dir")
    if data_dir:
        from pathlib import Path as _P
        dd = _P(data_dir)
        dd.mkdir(parents=True, exist_ok=True)
        price_db.set_db_path(dd)
        from web.state import set_data_dir
        set_data_dir(dd)
        global IMAGES_DIR
        IMAGES_DIR = dd / "images"
        IMAGES_DIR.mkdir(exist_ok=True)
    await price_db.init_db()
    # Backfill retailer intelligence from existing discord_log
    backfilled = await price_db.backfill_retailer_sightings()
    if backfilled:
        print(f"  Backfilled {backfilled} retailer sightings from Discord log")
    app_state.load_from_config(config)
    app_state.restore_from_disk()
    deal_tracker.restore_from_disk()
    product_hub.restore_from_disk()
    # Re-apply ignore patterns so restored sales reflect ebay_ignored_titles
    for ps in app_state.product_statuses:
        if ps.ebay_sales and ps.ebay_ignored_titles:
            _apply_ignore_patterns(ps)
            _recompute_ebay_aggregates(ps)
    app_state.save_to_disk()  # persist the corrected ignore flags
    # Cache any remote images locally on startup
    for ps in app_state.product_statuses:
        if ps.image_url and ps.image_url.startswith("http"):
            local = await cache_image(ps.image_url)
            if local:
                ps.image_url = local
    task         = asyncio.create_task(monitor_loop())
    reddit_task  = asyncio.create_task(reddit_poll_loop())
    research_task = asyncio.create_task(research_loop())

    discord_task = asyncio.create_task(discord_gateway_loop())
    marketplace_task = asyncio.create_task(marketplace_gateway_loop())

    app_state._monitor_task = task
    app_state._reddit_task  = reddit_task
    yield
    app_state.save_to_disk()
    deal_tracker.save_to_disk()
    product_hub.save_to_disk()
    app_state.monitor_running = False
    for t in (task, reddit_task, discord_task, marketplace_task, research_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    # Clean up Gateway browser if running
    if _discord_gw._browser:
        await _discord_gw.stop()

app = FastAPI(lifespan=lifespan)

# ------------------------------------------------------------------
# Static + local image caching
# ------------------------------------------------------------------

INDEX_PATH  = Path(__file__).parent / "index.html"
IMAGES_DIR  = Path(__file__).parent / "images"
IMAGES_DIR.mkdir(exist_ok=True)

from fastapi.staticfiles import StaticFiles
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")

import hashlib
import aiohttp as _aiohttp

async def cache_image(url: str) -> Optional[str]:
    """
    Download an image URL and save it locally.
    Returns the local /images/HASH.ext path, or None on failure.
    """
    if not url or not url.startswith("http"):
        return None

    # Hash the URL for a stable filename
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    ext = ".jpg"
    if ".png" in url.lower():
        ext = ".png"
    elif ".webp" in url.lower():
        ext = ".webp"

    local_path = IMAGES_DIR / f"{url_hash}{ext}"

    # Already cached
    if local_path.exists() and local_path.stat().st_size > 100:
        return f"/images/{url_hash}{ext}"

    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=10),
                                   headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 100:
                        local_path.write_bytes(data)
                        return f"/images/{url_hash}{ext}"
    except Exception:
        pass
    return None

@app.get("/api/setup-status")
async def setup_status():
    """Check if the app needs initial setup."""
    config = load_config()
    dc = config.get("discord", {})
    email = dc.get("email", "")
    needs_setup = not email or "YOUR_" in email
    return {"needs_setup": needs_setup}

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))

# ------------------------------------------------------------------
# WebSocket
# ------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    await app_state.ws.connect(ws)
    try:
        await ws.send_text(json.dumps(app_state.snapshot()))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await app_state.ws.disconnect(ws)

# ------------------------------------------------------------------
# Monitor control
# ------------------------------------------------------------------

@app.post("/api/monitor/start")
async def start_monitor():
    if app_state.monitor_running:
        return {"status": "already_running"}
    task = asyncio.create_task(monitor_loop())
    app_state._monitor_task = task
    await app_state.ws.broadcast({"type": "monitor_status", "data": {"running": True, "sleeping": False}})
    return {"status": "started"}

@app.post("/api/monitor/stop")
async def stop_monitor():
    app_state.monitor_running = False
    app_state.sleeping        = False
    if app_state._monitor_task:
        app_state._monitor_task.cancel()
    await app_state.ws.broadcast({"type": "monitor_status", "data": {"running": False, "sleeping": False}})
    return {"status": "stopped"}

# ------------------------------------------------------------------
# Products
# ------------------------------------------------------------------

class ProductIn(BaseModel):
    name: str
    url: str
    site: str = "walmart"
    max_price: float
    enabled: bool = True
    check_interval_seconds: int = 0    # 0 = use global
    tags: dict = {}
    image_url: Optional[str] = None

def _product_dict(body: ProductIn) -> dict:
    d = {
        "name":                    body.name,
        "url":                     body.url,
        "site":                    body.site,
        "max_price":               body.max_price,
        "enabled":                 body.enabled,
        "check_interval_seconds":  body.check_interval_seconds,
        "tags":                    body.tags,
    }
    if body.image_url:
        d["image_url"] = body.image_url
    return d

def _broadcast_state() -> dict:
    return {
        "type": "state",
        "data": {
            "monitor_running": app_state.monitor_running,
            "sleeping":        app_state.sleeping,
            "sleeping_label":  app_state.sleeping_label,
            "products":        [p.to_dict() for p in app_state.product_statuses],
            "deals":           [d.to_dict() for d in app_state.deals],
            "log":             list(app_state.log_buffer),
        },
    }

@app.get("/api/products")
async def list_products():
    return [p.to_dict() for p in app_state.product_statuses]

@app.post("/api/products")
async def add_product(body: ProductIn):
    config = load_config()
    config["products"].append(_product_dict(body))
    save_config(config)
    app_state.load_from_config(config)
    await app_state.ws.broadcast(_broadcast_state())
    await app_state.log("info", f"Added product: {body.name}")

    # Auto-create a Products hub entry for TCGPlayer items
    if body.site == "tcgplayer":
        from web.state import ProductEntry
        new_index = len(config["products"]) - 1
        tags = body.tags or {}
        hub_tags = {}
        if tags.get("set"):          hub_tags["set"] = tags["set"]
        if tags.get("product_type"): hub_tags["product_type"] = tags["product_type"]
        # Only create if no existing hub entry matches this name closely
        from web.state import _normalize_name, _tokenize, _jaccard
        new_tokens = _tokenize(_normalize_name(body.name))
        already_exists = any(
            _jaccard(new_tokens, _tokenize(_normalize_name(e.name))) >= 0.7
            for e in product_hub.entries
        )
        if not already_exists:
            entry = ProductEntry(
                id=str(__import__("uuid").uuid4())[:8],
                name=body.name,
                tags=hub_tags,
                tcgplayer_index=new_index,
            )
            # Copy image if available
            if new_index < len(app_state.product_statuses):
                img = app_state.product_statuses[new_index].image_url
                if img:
                    entry.image_url = img
            product_hub.entries.insert(0, entry)
            product_hub.save_to_disk()
            await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})

    return {"status": "ok"}

@app.put("/api/products/{index}")
async def update_product(index: int, body: ProductIn):
    config = load_config()
    if index < 0 or index >= len(config["products"]):
        raise HTTPException(status_code=404, detail="Product not found")
    config["products"][index] = _product_dict(body)
    save_config(config)
    app_state.load_from_config(config)
    await app_state.ws.broadcast({"type": "product_update", "data": app_state.product_statuses[index].to_dict()})
    await app_state.log("info", f"Updated: {body.name}")
    return {"status": "ok"}

@app.delete("/api/products/{index}")
async def delete_product(index: int):
    config = load_config()
    if index < 0 or index >= len(config["products"]):
        raise HTTPException(status_code=404, detail="Product not found")
    name = config["products"][index]["name"]
    config["products"].pop(index)
    save_config(config)
    app_state.load_from_config(config)
    await app_state.ws.broadcast(_broadcast_state())
    await app_state.log("info", f"Removed: {name}")
    return {"status": "ok"}

# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

@app.post("/api/products/{index}/ignore-sale")
async def ignore_sale(index: int, body: dict):
    """Toggle ignore on a specific eBay sale by its title. Learns ignore patterns."""
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps = app_state.product_statuses[index]
    title = body.get("title", "")
    ignore = body.get("ignore", True)
    # Apply to both sold and live lists
    for sale_list in (ps.ebay_sales or [], ps.ebay_live or []):
        for sale in sale_list:
            if sale.get("title") == title:
                sale["ignored"] = ignore

    # Persist the ignored titles list
    if ignore and title not in ps.ebay_ignored_titles:
        ps.ebay_ignored_titles.append(title)
    elif not ignore and title in ps.ebay_ignored_titles:
        ps.ebay_ignored_titles.remove(title)

    if ps.ebay_sales:

        # Learn ignore keywords from ignored titles
        _learn_ignore_patterns(ps)

        # Auto-apply learned patterns to other sales
        _apply_ignore_patterns(ps)

        # Recompute aggregates excluding ignored sales
        _recompute_ebay_aggregates(ps)

        await app_state.ws.broadcast({"type": "product_update", "data": ps.to_dict()})
        app_state.save_to_disk()
    return {"status": "ok"}


def _learn_ignore_patterns(ps):
    """
    Extract words that appear frequently in ignored titles but rarely
    in kept titles. These become auto-ignore keywords.
    """
    if not ps.ebay_sales:
        return
    ignored_titles = [s["title"].lower() for s in ps.ebay_sales if s.get("ignored")]
    kept_titles    = [s["title"].lower() for s in ps.ebay_sales if not s.get("ignored")]

    if not ignored_titles:
        ps.tags.pop("ignore_keywords", None)
        return

    # Count word frequency in ignored vs kept
    stop_words = {"the", "a", "an", "of", "and", "or", "for", "in", "on", "to", "is",
                  "it", "by", "with", "from", "at", "as", "new", "mint", "nm", "vf"}
    ignored_words = {}
    for t in ignored_titles:
        for w in set(t.split()):
            w = w.strip(".,!?#()[]{}\"'").lower()
            if len(w) >= 3 and w not in stop_words:
                ignored_words[w] = ignored_words.get(w, 0) + 1

    kept_words = set()
    for t in kept_titles:
        for w in t.split():
            kept_words.add(w.strip(".,!?#()[]{}\"'").lower())

    # Keywords that appear in 50%+ of ignored titles but not in kept titles
    threshold = max(1, len(ignored_titles) * 0.4)
    keywords = sorted(w for w, c in ignored_words.items()
                      if c >= threshold and w not in kept_words)

    if keywords:
        ps.tags["ignore_keywords"] = keywords
    else:
        ps.tags.pop("ignore_keywords", None)


_RETAILER_CANONICAL = {
    "bestbuy": "Best Buy", "best buy": "Best Buy",
    "walmart": "Walmart",
    "amazon": "Amazon",
    "target": "Target",
    "gamestop": "GameStop", "game stop": "GameStop",
    "barnes & noble": "Barnes & Noble", "barnes and noble": "Barnes & Noble",
    "tcgplayer": "TCGPlayer", "tcg player": "TCGPlayer",
}

def _normalize_retailer(raw: str) -> str:
    """Normalize retailer name to canonical form."""
    key = raw.strip().lower()
    return _RETAILER_CANONICAL.get(key, raw.strip())

async def _ingest_retailer_sighting(entry: dict) -> None:
    """Extract retailer intelligence from a Discord audit log entry and store it."""
    import re as _re
    fields = entry.get("embed_fields", "") or ""
    title = entry.get("embed_title", "") or ""
    if not title or not fields:
        return
    text = (title + " " + fields).lower()

    # Detect game
    game = None
    if any(k in text for k in ('magic', 'mtg', 'strixhaven', 'spider-man', 'avatar', 'secret lair')):
        game = 'MTG'
    elif any(k in text for k in ('pokemon', 'pokémon', 'prismatic', 'paldea')):
        game = 'Pokemon'
    elif 'riftbound' in text: game = 'Riftbound'
    elif any(k in text for k in ('yu-gi-oh', 'yugioh')): game = 'Yu-Gi-Oh'
    elif 'lorcana' in text: game = 'Lorcana'
    elif 'star wars' in text: game = 'Star Wars'
    elif 'digimon' in text: game = 'Digimon'
    elif 'one piece' in text: game = 'One Piece'
    elif 'dragon ball' in text: game = 'Dragon Ball'

    # Detect retailer
    retailer = None
    if 'amazon.com' in fields or 'amazon.ca' in fields: retailer = 'Amazon'
    elif 'walmart.com' in fields: retailer = 'Walmart'
    elif 'target.com' in fields: retailer = 'Target'
    elif 'bestbuy.com' in fields: retailer = 'Best Buy'
    else:
        seller_m = _re.search(r'(?:Seller|Site):\s*(.+)', fields)
        if seller_m:
            s = seller_m.group(1).strip().lower()
            if 'amazon' in s: retailer = 'Amazon'
            elif 'walmart' in s: retailer = 'Walmart'
            elif 'target' in s: retailer = 'Target'
            elif 'best buy' in s: retailer = 'Best Buy'
            else: retailer = _normalize_retailer(seller_m.group(1))

    if not retailer:
        return
    retailer = _normalize_retailer(retailer)

    asin_m = _re.search(r'(?:ASIN|SKU):\s*(?:```)?([A-Z0-9]{10})', fields)
    asin = asin_m.group(1) if asin_m else None

    product_url = None
    url_m = _re.search(r'\[.*?\]\((https?://(?:www\.)?(?:amazon|walmart|target|bestbuy|tcgplayer)[^\)]+)\)', fields)
    if url_m: product_url = url_m.group(1)
    elif asin: product_url = f'https://www.amazon.com/dp/{asin}'

    checkout_url = None
    cart_m = _re.search(r'\[(?:ATC\w*|Add to cart|Click Here)\]\((https?://[^\)]+)\)', fields, _re.I)
    if cart_m: checkout_url = cart_m.group(1)

    product_name = title.strip('*').strip()[:120]

    await price_db.record_retailer_sighting(
        product_name=product_name, game=game, retailer=retailer,
        price=entry.get("price"), asin=asin,
        product_url=product_url, checkout_url=checkout_url,
        channel_id=entry.get("channel_id"), msg_id=entry.get("msg_id"),
        timestamp=entry.get("timestamp", ""),
    )


def _normalize_title(t: str) -> str:
    """Normalize an eBay title for fuzzy matching."""
    import re as _re
    return _re.sub(r'\s+', ' ', t.strip().lower())


def _compute_market_low(listing_prices: list[dict], fallback_low: float | None) -> float | None:
    """Compute 5th percentile from listing distribution to exclude outliers."""
    if not listing_prices or len(listing_prices) < 5:
        return fallback_low
    try:
        # Expand to individual prices weighted by quantity
        expanded = []
        for item in listing_prices:
            p = item.get("total") or item.get("price", 0)
            if p and p > 0:
                expanded.extend([p] * max(1, item.get("qty", 1)))
        if len(expanded) < 10:
            return fallback_low
        expanded.sort()
        # 5th percentile (10th if small sample)
        pct_idx = max(0, int(len(expanded) * (0.10 if len(expanded) < 50 else 0.05)))
        return round(expanded[pct_idx], 2)
    except Exception:
        return fallback_low


def _apply_ignore_patterns(ps):
    """Auto-ignore sales/live matching learned keywords OR previously ignored titles."""
    keywords = ps.tags.get("ignore_keywords", [])
    # Build both exact and normalized sets for matching
    ignored_exact = set(ps.ebay_ignored_titles)
    ignored_normalized = {_normalize_title(t) for t in ps.ebay_ignored_titles}

    for sale_list in (ps.ebay_sales or [], ps.ebay_live or []):
        for sale in sale_list:
            if sale.get("ignored"):
                continue
            title = sale["title"]
            title_lower = title.lower()
            # Exact match
            if title in ignored_exact:
                sale["ignored"] = True
                continue
            # Normalized match (handles whitespace/case differences)
            if _normalize_title(title) in ignored_normalized:
                sale["ignored"] = True
                continue
            # Keyword match (learned from ignore patterns)
            if keywords and any(kw in title_lower for kw in keywords):
                sale["ignored"] = True
                sale["auto_ignored"] = True


def _recompute_ebay_aggregates(ps):
    """Recalculate median/avg/low/high/by_grade from non-ignored sales."""
    import statistics
    if not ps.ebay_sales:
        return
    active = [s for s in ps.ebay_sales if not s.get("ignored")]
    prices = [s["price"] for s in active if s.get("price")]
    ps.ebay_median     = round(statistics.median(prices), 2) if prices else None
    ps.ebay_avg        = round(statistics.mean(prices), 2) if prices else None
    ps.ebay_low        = min(prices) if prices else None
    ps.ebay_high       = max(prices) if prices else None
    ps.ebay_sold_count = len(active)
    groups = {}
    for s in active:
        g = s.get("grade", "Raw")
        groups.setdefault(g, []).append(s["price"])
    ps.ebay_by_grade = {
        g: {"count": len(p), "median": round(statistics.median(p), 2), "low": min(p), "high": max(p)}
        for g, p in sorted(groups.items())
    }

@app.post("/api/products/toggle-retailer")
async def toggle_retailer(body: dict):
    """Enable or disable all products for a specific retailer."""
    retailer = body.get("retailer", "")
    enabled  = body.get("enabled", True)
    config   = load_config()
    count    = 0
    for i, ps in enumerate(app_state.product_statuses):
        if ps.tags.get("retailer") == retailer and ps.tags.get("category") not in ("single", "comic"):
            ps.enabled = enabled
            if i < len(config["products"]):
                config["products"][i]["enabled"] = enabled
            count += 1
    save_config(config)
    await app_state.ws.broadcast({"type": "state", "data": app_state.snapshot()["data"]})
    app_state.save_to_disk()
    await app_state.log("info", f"{'Enabled' if enabled else 'Disabled'} {count} {retailer} products", "system")
    return {"status": "ok", "count": count}

@app.post("/api/products/{index}/remove-keyword")
async def remove_keyword(index: int, body: dict):
    """Remove a learned auto-ignore keyword and un-ignore affected sales."""
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps = app_state.product_statuses[index]
    keyword = body.get("keyword", "")

    # Remove from keywords list
    kw_list = ps.tags.get("ignore_keywords", [])
    if keyword in kw_list:
        kw_list.remove(keyword)
        if kw_list:
            ps.tags["ignore_keywords"] = kw_list
        else:
            ps.tags.pop("ignore_keywords", None)

    # Un-ignore any sales that were auto-ignored by this keyword
    if ps.ebay_sales:
        for sale in ps.ebay_sales:
            if sale.get("auto_ignored") and keyword in sale.get("title", "").lower():
                sale["ignored"] = False
                sale.pop("auto_ignored", None)

    _recompute_ebay_aggregates(ps)
    await app_state.ws.broadcast({"type": "product_update", "data": ps.to_dict()})
    app_state.save_to_disk()
    return {"status": "ok"}

@app.post("/api/products/{index}/update-sale-grade")
async def update_sale_grade(index: int, body: dict):
    """Override the grade classification of a specific eBay sale."""
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps = app_state.product_statuses[index]
    sale_index = body.get("sale_index")
    new_grade = body.get("grade", "Raw")

    if ps.ebay_sales and 0 <= sale_index < len(ps.ebay_sales):
        ps.ebay_sales[sale_index]["grade"] = new_grade
        ps.ebay_sales[sale_index]["grade_override"] = True

        _recompute_ebay_aggregates(ps)
        await app_state.ws.broadcast({"type": "product_update", "data": ps.to_dict()})
        app_state.save_to_disk()

    return {"status": "ok"}


@app.post("/api/discord/test-dm")
async def test_discord_dm(body: dict):
    config = load_config()
    dc = config.get("discord", {})
    bot_token = dc.get("bot_token", "")
    token = f"Bot {bot_token}" if bot_token else dc.get("token", "")
    user_id = body.get("user_id", "").strip()
    if not token or not user_id:
        return {"status": "error", "message": "Bot token or User ID missing"}
    ok = await _discord.send_dm(token, user_id, "🔔 **Test Notification**\nThis is a test from The Stone of Osgiliath. Deal notifications are working!")
    return {"status": "ok" if ok else "error", "message": "DM sent!" if ok else "Failed to send DM — check your User ID"}

@app.get("/api/discord/channels")
async def get_discord_channels():
    """Return monitored channels with resolved names."""
    config = load_config()
    dc = config.get("discord", {})
    channels = dc.get("channels_to_monitor", [])
    result = []
    for raw in channels:
        # Parse guild_id/channel_id or full URL format
        _, cid = _discord_gw._parse_channel(str(raw))
        name = _discord_gw.channel_names.get(cid) or _discord._channel_names.get(cid) or f"#{cid[-4:]}"
        result.append({"id": str(raw), "name": name})
    return {"channels": result}

@app.post("/api/discord/channels/add")
async def add_discord_channel(body: dict):
    ch_id = body.get("channel_id", "").strip()
    if not ch_id:
        return {"error": "channel_id required"}
    config = load_config()
    channels = config.setdefault("discord", {}).setdefault("channels_to_monitor", [])
    if ch_id not in channels:
        channels.append(ch_id)
        save_config(config)
        # Channel name will be resolved from Gateway READY/GUILD_CREATE events
    return {"status": "ok"}

@app.post("/api/discord/channels/remove")
async def remove_discord_channel(body: dict):
    ch_id = body.get("channel_id", "").strip()
    config = load_config()
    channels = config.get("discord", {}).get("channels_to_monitor", [])
    if ch_id in channels:
        channels.remove(ch_id)
        config["discord"]["channels_to_monitor"] = channels
        save_config(config)
    return {"status": "ok"}

@app.get("/api/discord/keywords")
async def get_discord_keywords():
    """Return active Discord keywords (manual + auto-generated from products)."""
    config = load_config()
    dc = config.get("discord", {})
    manual = [k.lower() for k in dc.get("keywords", [])]
    disabled = dc.get("disabled_keywords", [])

    auto = []
    for p in config.get("products", []):
        tags = p.get("tags", {})
        if tags.get("category") in ("single", "comic"):
            continue
        s = tags.get("set", "")
        pt = tags.get("product_type", "")
        if s and s.lower() not in auto:
            auto.append(s.lower())
        if pt and pt.lower() not in auto:
            auto.append(pt.lower())

    ignored = dc.get("ignored_patterns", [])
    blocked_retailers = dc.get("blocked_retailers", [])
    return {
        "manual": manual,
        "auto": auto,
        "disabled": disabled,
        "ignored": ignored,
        "blocked_retailers": blocked_retailers,
        "min_price": dc.get("min_price", 0),
        "active": [k for k in (manual + auto) if k not in disabled],
    }

@app.post("/api/discord/keywords/add")
async def add_discord_keyword(body: dict):
    """Add a manual keyword to Discord filtering."""
    keyword = body.get("keyword", "").strip().lower()
    if not keyword:
        return {"error": "keyword required"}
    config = load_config()
    keywords = config.setdefault("discord", {}).setdefault("keywords", [])
    if keyword not in [k.lower() for k in keywords]:
        keywords.append(keyword)
        save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/keywords/remove")
async def remove_discord_keyword(body: dict):
    """Remove a manual keyword from Discord filtering."""
    keyword = body.get("keyword", "").strip().lower()
    config = load_config()
    keywords = config.get("discord", {}).get("keywords", [])
    config["discord"]["keywords"] = [k for k in keywords if k.lower() != keyword]
    save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/keywords/disable")
async def disable_discord_keyword(body: dict):
    """Disable a specific keyword from Discord filtering."""
    keyword = body.get("keyword", "").lower()
    config = load_config()
    disabled = config.get("discord", {}).get("disabled_keywords", [])
    if keyword not in disabled:
        disabled.append(keyword)
        config.setdefault("discord", {})["disabled_keywords"] = disabled
        save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/keywords/enable")
async def enable_discord_keyword(body: dict):
    """Re-enable a previously disabled keyword."""
    keyword = body.get("keyword", "").lower()
    config = load_config()
    disabled = config.get("discord", {}).get("disabled_keywords", [])
    if keyword in disabled:
        disabled.remove(keyword)
        config["discord"]["disabled_keywords"] = disabled
        save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/ignore")
async def ignore_discord_message(body: dict):
    """
    Ignore a Discord message and learn patterns from it.
    Extracts significant words from the message and adds them as ignored_patterns.
    """
    content = body.get("content", "")
    embeds  = body.get("embeds", [])
    msg_id  = body.get("id", "")

    # Build full text to extract patterns from
    full = content.lower()
    for e in embeds:
        full += " " + (e.get("title", "") + " " + e.get("description", "")).lower()

    # Extract meaningful words (3+ chars, not stop words)
    stop = {"the","and","for","with","from","this","that","have","has","are","was",
            "were","been","being","will","would","could","should","can","may","might",
            "new","now","just","get","got","one","two","all","any","each","out","not"}
    words = set()
    for w in full.split():
        w = w.strip(".,!?#()[]{}\"'`<>@:").lower()
        if len(w) >= 3 and w not in stop and not w.startswith("http") and not w.isdigit():
            words.add(w)

    # Get existing keywords so we don't ignore those
    config = load_config()
    dc = config.get("discord", {})
    active_kw = set(k.lower() for k in dc.get("keywords", []))
    # Also auto keywords
    for p in config.get("products", []):
        tags = p.get("tags", {})
        if tags.get("category") in ("single", "comic"):
            continue
        if tags.get("set"):
            for w in tags["set"].lower().split():
                active_kw.add(w)
        if tags.get("product_type"):
            for w in tags["product_type"].lower().split():
                active_kw.add(w)

    # Find words unique to this message (not in active keywords)
    candidates = words - active_kw

    # Return candidates for the user to pick from
    existing_ignored = set(dc.get("ignored_patterns", []))

    return {
        "candidates": sorted(candidates - existing_ignored),
        "existing": sorted(existing_ignored),
        "msg_id": msg_id,
    }

@app.post("/api/discord/ignore/add")
async def add_discord_ignore_pattern(body: dict):
    """Add specific patterns to the Discord ignore list."""
    patterns = body.get("patterns", [])
    config = load_config()
    dc = config.setdefault("discord", {})
    ignored = dc.get("ignored_patterns", [])
    for p in patterns:
        if p.lower() not in [x.lower() for x in ignored]:
            ignored.append(p.lower())
    dc["ignored_patterns"] = ignored
    save_config(config)

    # Remove the message from the feed
    msg_id = body.get("msg_id", "")
    if msg_id:
        app_state.discord_posts = deque(
            [p for p in app_state.discord_posts if p.get("id") != msg_id],
            maxlen=100,
        )
        await app_state.ws.broadcast({"type": "state", "data": app_state.snapshot()["data"]})

    return {"status": "ok", "ignored_patterns": ignored}

@app.get("/api/discord/log")
async def get_discord_log(q: str = "", action: str = "", limit: int = 100, offset: int = 0):
    """Search the Discord audit log. action=shown|filtered"""
    rows = await price_db.search_discord_log(query=q, action=action, limit=limit, offset=offset)
    stats = await price_db.discord_log_stats()
    return {"rows": rows, "stats": stats}

@app.post("/api/discord/min-price")
async def set_discord_min_price(body: dict):
    config = load_config()
    config.setdefault("discord", {})["min_price"] = body.get("min_price", 0)
    save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/ignore/remove")
async def remove_discord_ignore_pattern(body: dict):
    """Remove a pattern from the Discord ignore list."""
    pattern = body.get("pattern", "").lower()
    config = load_config()
    ignored = config.get("discord", {}).get("ignored_patterns", [])
    if pattern in ignored:
        ignored.remove(pattern)
        config["discord"]["ignored_patterns"] = ignored
        save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/blocked-retailers/add")
async def add_blocked_retailer(body: dict):
    retailer = body.get("retailer", "").strip().lower()
    if not retailer:
        return {"error": "retailer required"}
    config = load_config()
    blocked = config.setdefault("discord", {}).setdefault("blocked_retailers", [])
    if retailer not in blocked:
        blocked.append(retailer)
        save_config(config)
    return {"status": "ok"}

@app.post("/api/discord/blocked-retailers/remove")
async def remove_blocked_retailer(body: dict):
    retailer = body.get("retailer", "").strip().lower()
    config = load_config()
    blocked = config.get("discord", {}).get("blocked_retailers", [])
    if retailer in blocked:
        blocked.remove(retailer)
        config["discord"]["blocked_retailers"] = blocked
        save_config(config)
    return {"status": "ok"}

# ── Tracked deals API ──────────────────────────────────────────

@app.get("/api/tracked-deals")
async def get_tracked_deals():
    return {"deals": [d.to_dict() for d in deal_tracker.deals]}

@app.post("/api/tracked-deals/{deal_id}/dismiss")
async def dismiss_deal(deal_id: str):
    d = deal_tracker.find_by_id(deal_id)
    if not d:
        return {"error": "not found"}
    d.dismissed = True
    deal_tracker.save_to_disk()
    await app_state.ws.broadcast({"type": "tracked_deal_update", "data": d.to_dict()})
    return {"status": "ok"}

@app.post("/api/tracked-deals/{deal_id}/undismiss")
async def undismiss_deal(deal_id: str):
    d = deal_tracker.find_by_id(deal_id)
    if not d:
        return {"error": "not found"}
    d.dismissed = False
    deal_tracker.save_to_disk()
    await app_state.ws.broadcast({"type": "tracked_deal_update", "data": d.to_dict()})
    return {"status": "ok"}

@app.put("/api/tracked-deals/{deal_id}")
async def update_deal(deal_id: str, body: dict):
    from web.state import _normalize_name, _tokenize
    d = deal_tracker.find_by_id(deal_id)
    if not d:
        return {"error": "not found"}
    if "name" in body and body["name"]:
        d.name = body["name"]
        d.normalized = _normalize_name(d.name)
        d.tokens = _tokenize(d.normalized)
    if "tags" in body:
        d.tags = body["tags"]
    if "image_url" in body:
        d.image_url = body["image_url"]
    deal_tracker.save_to_disk()
    await app_state.ws.broadcast({"type": "tracked_deal_update", "data": d.to_dict()})
    return {"status": "ok", "deal": d.to_dict()}

@app.delete("/api/tracked-deals/{deal_id}")
async def delete_deal(deal_id: str):
    deal_tracker.deals = [d for d in deal_tracker.deals if d.id != deal_id]
    deal_tracker.save_to_disk()
    await app_state.ws.broadcast({"type": "tracked_deals_full", "data": [d.to_dict() for d in deal_tracker.deals]})
    return {"status": "ok"}

@app.post("/api/tracked-deals/merge")
async def merge_deals(body: dict):
    """Merge two deals together. keep_id stays, merge_id gets folded in."""
    keep_id = body.get("keep_id", "")
    merge_id = body.get("merge_id", "")
    keep = deal_tracker.find_by_id(keep_id)
    merge = deal_tracker.find_by_id(merge_id)
    if not keep or not merge:
        return {"error": "not found"}
    # Fold sightings from merge into keep
    keep.sightings = sorted(
        keep.sightings + merge.sightings,
        key=lambda s: s.timestamp, reverse=True,
    )[:50]
    keep.last_seen = keep.sightings[0].timestamp if keep.sightings else keep.last_seen
    if merge.image_url and not keep.image_url:
        keep.image_url = merge.image_url
    # Remove merged deal
    deal_tracker.deals = [d for d in deal_tracker.deals if d.id != merge_id]
    deal_tracker.save_to_disk()
    await app_state.ws.broadcast({"type": "tracked_deals_full", "data": [d.to_dict() for d in deal_tracker.deals]})
    return {"status": "ok", "deal": keep.to_dict()}

@app.post("/api/tracked-deals/clear-dismissed")
async def clear_dismissed_deals():
    deal_tracker.deals = [d for d in deal_tracker.deals if not d.dismissed]
    deal_tracker.save_to_disk()
    await app_state.ws.broadcast({"type": "tracked_deals_full", "data": [d.to_dict() for d in deal_tracker.deals]})
    return {"status": "ok"}


# ── Product Hub API ─────────────────────────────────────────────

@app.get("/api/product-hub")
async def get_product_hub():
    return {"entries": [e.to_dict() for e in product_hub.entries]}

@app.post("/api/product-hub")
async def create_hub_entry(body: dict):
    from web.state import ProductEntry, RetailerLink
    entry = ProductEntry(
        id=str(__import__("uuid").uuid4())[:8],
        name=body.get("name", ""),
        image_url=body.get("image_url"),
        tags=body.get("tags", {}),
        retailer_urls=[RetailerLink(**r) for r in body.get("retailer_urls", [])],
        tcgplayer_index=body.get("tcgplayer_index"),
        deal_ids=body.get("deal_ids", []),
    )
    product_hub.entries.insert(0, entry)
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok", "entry": entry.to_dict()}

@app.put("/api/product-hub/{entry_id}")
async def update_hub_entry(entry_id: str, body: dict):
    from web.state import RetailerLink
    e = product_hub.find_by_id(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    if "name" in body:
        e.name = body["name"]
    if "tags" in body:
        e.tags = body["tags"]
    if "image_url" in body:
        e.image_url = body["image_url"]
    if "retailer_urls" in body:
        e.retailer_urls = [RetailerLink(**r) for r in body["retailer_urls"]]
    if "tcgplayer_index" in body:
        e.tcgplayer_index = body["tcgplayer_index"]
    if "deal_ids" in body:
        e.deal_ids = body["deal_ids"]
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok", "entry": e.to_dict()}

@app.delete("/api/product-hub/{entry_id}")
async def delete_hub_entry(entry_id: str):
    product_hub.entries = [e for e in product_hub.entries if e.id != entry_id]
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok"}

@app.post("/api/product-hub/{entry_id}/retailer")
async def add_hub_retailer(entry_id: str, body: dict):
    from web.state import RetailerLink
    e = product_hub.find_by_id(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    e.retailer_urls.append(RetailerLink(retailer=body.get("retailer", ""), url=body.get("url", "")))
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok"}

@app.delete("/api/product-hub/{entry_id}/retailer/{idx}")
async def remove_hub_retailer(entry_id: str, idx: int):
    e = product_hub.find_by_id(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    if 0 <= idx < len(e.retailer_urls):
        e.retailer_urls.pop(idx)
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok"}

@app.post("/api/product-hub/{entry_id}/assign-deal")
async def assign_deal_to_hub(entry_id: str, body: dict):
    e = product_hub.find_by_id(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    deal_id = body.get("deal_id", "")
    if deal_id and deal_id not in e.deal_ids:
        e.deal_ids.append(deal_id)
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok"}

@app.post("/api/product-hub/{entry_id}/unassign-deal")
async def unassign_deal_from_hub(entry_id: str, body: dict):
    e = product_hub.find_by_id(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    deal_id = body.get("deal_id", "")
    if deal_id in e.deal_ids:
        e.deal_ids.remove(deal_id)
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok"}

@app.post("/api/product-hub/{entry_id}/link-tcgplayer")
async def link_tcgplayer_to_hub(entry_id: str, body: dict):
    e = product_hub.find_by_id(entry_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    idx = body.get("tcgplayer_index")
    e.tcgplayer_index = idx
    # Copy image from TCGPlayer item if hub entry doesn't have one
    if idx is not None and not e.image_url and 0 <= idx < len(app_state.product_statuses):
        tcg_img = app_state.product_statuses[idx].image_url
        if tcg_img:
            e.image_url = tcg_img
    product_hub.save_to_disk()
    await app_state.ws.broadcast({"type": "product_hub_full", "data": [e.to_dict() for e in product_hub.entries]})
    return {"status": "ok"}


@app.post("/api/refresh/{category}")
async def refresh_category(category: str):
    """Force all items of a category (or site) to check on next cycle."""
    if category == "tcgplayer":
        # Refresh all TCGPlayer items (singles + sealed)
        count = sum(1 for ps in app_state.product_statuses if ps.site == "tcgplayer")
    else:
        count = sum(1 for ps in app_state.product_statuses if ps.tags.get("category") == category)
    app_state.force_refresh_categories.add(category)
    await app_state.log("info", f"Force refresh queued for {count} {category} items", "system")
    return {"status": "ok", "count": count}

@app.get("/api/settings")
async def get_settings():
    config   = load_config()
    stealth  = config.get("stealth",   {})
    schedule = config.get("schedule",  {})
    research = config.get("research",  {})
    return {
        "check_interval_seconds": config.get("check_interval_seconds", 300),
        "jitter_pct":            stealth.get("jitter_pct", 20),
        "headless":              stealth.get("headless", True),
        "user_agent":            stealth.get("user_agent") or "",
        "browser_channel":       stealth.get("browser_channel") or "",
        "page_timeout_ms":       stealth.get("page_timeout_ms", 30000),
        "network_timeout_seconds": stealth.get("network_timeout_seconds", 15),
        "schedule_enabled":      schedule.get("enabled", False),
        "schedule_start":        schedule.get("start", "07:00"),
        "schedule_end":          schedule.get("end", "23:00"),
        "data_dir":              config.get("data_dir") or "",
        "graded_interval_hours": round(config.get("graded_interval_seconds", 7 * 86400) / 3600),
        "marketplace_enabled":     config.get("marketplace", {}).get("enabled", False),
        "marketplace_sell_channels": config.get("marketplace", {}).get("sell_channels", []),
        "marketplace_buy_channels":  config.get("marketplace", {}).get("buy_channels", []),
        "reddit_subreddits":    config.get("reddit", {}).get("subreddits", ["sealedmtgdeals"]),
        "reddit_poll_interval": config.get("reddit", {}).get("poll_interval_seconds", 60),
        "bot_token":            config.get("discord", {}).get("bot_token") or "",
        "dm_user_id":           config.get("discord", {}).get("dm_user_id") or "",
        "discord_email":        config.get("discord", {}).get("email") or "",
        "discord_password":     config.get("discord", {}).get("password") or "",
        "research_enabled":      research.get("enabled", False),
        "research_api_key":      research.get("api_key") or "",
        "research_interval_hours": research.get("interval_hours", 168),
        "research_lookback_days":  research.get("lookback_days", 7),
        "research_max_findings":   research.get("max_findings", 7),
    }

@app.put("/api/settings")
async def update_settings(body: dict):
    config = load_config()
    if "check_interval_seconds" in body:
        config["check_interval_seconds"] = max(60, int(body["check_interval_seconds"]))
    s = config.setdefault("stealth", {})
    if "jitter_pct" in body:
        s["jitter_pct"] = max(0, min(50, int(body["jitter_pct"])))
    if "headless" in body:
        s["headless"] = bool(body["headless"])
    if "user_agent" in body:
        s["user_agent"] = body["user_agent"].strip() or None
    if "browser_channel" in body:
        s["browser_channel"] = body["browser_channel"].strip() or None
    if "page_timeout_ms" in body:
        s["page_timeout_ms"] = max(5000, int(body["page_timeout_ms"]))
    if "network_timeout_seconds" in body:
        s["network_timeout_seconds"] = max(5, int(body["network_timeout_seconds"]))
    sc = config.setdefault("schedule", {})
    if "schedule_enabled" in body:
        sc["enabled"] = bool(body["schedule_enabled"])
    if "schedule_start" in body:
        sc["start"] = body["schedule_start"]
    if "schedule_end" in body:
        sc["end"] = body["schedule_end"]
    if "data_dir" in body:
        config["data_dir"] = body["data_dir"].strip() or None
    if "graded_interval_hours" in body:
        config["graded_interval_seconds"] = max(1, int(body["graded_interval_hours"])) * 3600
    if "marketplace_enabled" in body:
        config.setdefault("marketplace", {})["enabled"] = bool(body["marketplace_enabled"])
    if "marketplace_sell_channels" in body:
        val = body["marketplace_sell_channels"]
        if isinstance(val, str):
            val = [s.strip() for s in val.split(",") if s.strip()]
        config.setdefault("marketplace", {})["sell_channels"] = val
    if "marketplace_buy_channels" in body:
        val = body["marketplace_buy_channels"]
        if isinstance(val, str):
            val = [s.strip() for s in val.split(",") if s.strip()]
        config.setdefault("marketplace", {})["buy_channels"] = val
    if "reddit_subreddits" in body:
        subs = body["reddit_subreddits"]
        if isinstance(subs, str):
            subs = [s.strip() for s in subs.split(",") if s.strip()]
        config.setdefault("reddit", {})["subreddits"] = subs
    if "reddit_poll_interval" in body:
        config.setdefault("reddit", {})["poll_interval_seconds"] = max(30, min(300, int(body["reddit_poll_interval"])))
    if "bot_token" in body:
        config.setdefault("discord", {})["bot_token"] = body["bot_token"].strip() or None
    if "dm_user_id" in body:
        config.setdefault("discord", {})["dm_user_id"] = body["dm_user_id"].strip() or None
    if "discord_email" in body:
        config.setdefault("discord", {})["email"] = body["discord_email"].strip() or None
    if "discord_password" in body:
        config.setdefault("discord", {})["password"] = body["discord_password"].strip() or None
    # Research agent settings
    rc = config.setdefault("research", {})
    if "research_enabled" in body:
        rc["enabled"] = bool(body["research_enabled"])
    if "research_api_key" in body:
        rc["api_key"] = body["research_api_key"].strip() or None
    if "research_interval_hours" in body:
        rc["interval_hours"] = max(1, int(body["research_interval_hours"]))
    if "research_lookback_days" in body:
        rc["lookback_days"] = max(1, min(90, int(body["research_lookback_days"])))
    if "research_max_findings" in body:
        rc["max_findings"] = max(1, min(20, int(body["research_max_findings"])))
    save_config(config)
    await app_state.log("info", "Settings saved", "system")
    return {"status": "ok"}

@app.post("/api/products/{index}/resume")
async def resume_product(index: int):
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    name = app_state.product_statuses[index].name
    await app_state.update_product(index, error=None)
    await app_state.log("info", f"Resumed: {name}")
    return {"status": "ok"}

# ------------------------------------------------------------------
# Deals & History
# ------------------------------------------------------------------

@app.get("/api/deals")
async def get_deals():
    return [d.to_dict() for d in app_state.deals]

@app.delete("/api/deals")
async def clear_deals():
    app_state.deals.clear()
    app_state.save_to_disk()
    await app_state.ws.broadcast({"type": "state", "data": app_state.snapshot()["data"]})
    return {"status": "ok"}

@app.get("/api/history/{index}")
async def get_history(index: int, days: int = 30):
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps   = app_state.product_statuses[index]
    rows = await price_db.get_history(ps.url, days=days)
    return {"product": ps.name, "url": ps.url, "days": days, "points": rows}

@app.get("/api/history/{index}/stats")
async def get_stats(index: int):
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps    = app_state.product_statuses[index]
    stats = await price_db.get_stats(ps.url)
    return {"product": ps.name, **stats}

@app.get("/api/marketplace")
async def get_marketplace(intent: str = "", limit: int = 50):
    rows = await price_db.get_marketplace_listings(limit=limit, intent=intent)
    for r in rows:
        try:
            r["items"] = json.loads(r.get("items_json") or "[]")
            r["matched"] = json.loads(r.get("matched_json") or "[]")
        except Exception:
            r["items"] = []
            r["matched"] = []
    return {"listings": rows}

@app.get("/api/retailer-overview")
async def get_retailer_overview():
    """Return retailer intelligence overview data."""
    return await price_db.get_retailer_overview()

@app.get("/api/tcg-trends")
async def get_tcg_trends():
    """Return 7-day price/listing trends for all TCGPlayer products."""
    trends = await price_db.get_tcg_trends()
    # Map URL → trend data, then attach product index
    url_map = {t["url"]: t for t in trends}
    result = {}
    for ps in app_state.product_statuses:
        if ps.site != "tcgplayer":
            continue
        t = url_map.get(ps.url)
        if not t:
            continue
        price_1d = t.get("price_1d")
        price_7d = t.get("price_7d")
        listings_1d = t.get("listings_1d")
        listings_7d = t.get("listings_7d")
        result[ps.index] = {
            "price_1d": round(price_1d, 2) if price_1d else None,
            "price_7d": round(price_7d, 2) if price_7d else None,
            "price_trend": round(((price_1d - price_7d) / price_7d) * 100, 1) if price_1d and price_7d and price_7d > 0 else None,
            "listings_1d": round(listings_1d) if listings_1d else None,
            "listings_7d": round(listings_7d) if listings_7d else None,
            "listings_trend": round(((listings_1d - listings_7d) / listings_7d) * 100, 1) if listings_1d and listings_7d and listings_7d > 0 else None,
        }
    return {"trends": result}

@app.get("/api/tcg-history/{index}")
async def get_tcg_history(index: int, days: int = 30):
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps   = app_state.product_statuses[index]
    rows = await price_db.get_tcg_history(ps.url, days=days)
    return {"product": ps.name, "url": ps.url, "days": days, "points": rows}

@app.get("/api/tcg-history/{index}/stats")
async def get_tcg_stats(index: int):
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps    = app_state.product_statuses[index]
    stats = await price_db.get_tcg_stats(ps.url)
    return {"product": ps.name, **stats}

@app.get("/api/tcg-history/{index}/listing-snapshots")
async def get_listing_snapshots(index: int, days: int = 90):
    if index < 0 or index >= len(app_state.product_statuses):
        raise HTTPException(status_code=404, detail="Product not found")
    ps = app_state.product_statuses[index]
    rows = await price_db.get_tcg_listing_snapshots(ps.url, days=days)
    for r in rows:
        try:
            r["listing_prices"] = json.loads(r.pop("listing_prices_json"))
        except Exception:
            r["listing_prices"] = []
            r.pop("listing_prices_json", None)
    return {"product": ps.name, "snapshots": rows}

# ------------------------------------------------------------------
# Portfolio
# ------------------------------------------------------------------

PORTFOLIO_PATH = Path(__file__).parent.parent / "portfolio.json"

def _load_portfolio() -> dict:
    try:
        return json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}

def _save_portfolio(data: dict) -> None:
    PORTFOLIO_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

def _enrich_portfolio(portfolio: dict) -> dict:
    """Add current market values from monitored products."""
    # Build lookup: name+condition -> price data
    price_lookup = {}
    for ps in app_state.product_statuses:
        tags = ps.tags or {}
        cat = tags.get("category", "")
        key_parts = [ps.name.lower()]
        if tags.get("condition"):
            key_parts.append(tags["condition"].lower())
        if tags.get("printing"):
            key_parts.append(tags["printing"].lower())
        key = "|".join(key_parts)

        if cat == "single":
            price_lookup[key] = {
                "current_price": ps.tcg_low_price or ps.price,
                "source": "tcgplayer",
            }
        elif cat == "comic":
            price_lookup[key] = {
                "current_price": ps.ebay_median,
                "source": "ebay",
                "by_grade": ps.ebay_by_grade,
            }

    total_cost = 0
    total_value = 0

    for item in portfolio.get("items", []):
        # Try to find a matching monitored product
        ikey_parts = [item.get("name", "").lower()]
        if item.get("condition"):
            ikey_parts.append(item["condition"].lower())
        if item.get("printing"):
            ikey_parts.append(item["printing"].lower())
        ikey = "|".join(ikey_parts)

        lookup = price_lookup.get(ikey)

        # For comics with grades, look up grade-specific pricing
        if item.get("grade") and lookup and lookup.get("by_grade"):
            grade_num = ""
            import re as _re
            gm = _re.search(r'([\d.]+)', item["grade"])
            if gm:
                grade_num = gm.group(1)
            for gkey, gdata in lookup["by_grade"].items():
                if grade_num and grade_num in gkey:
                    item["current_price"] = gdata.get("median")
                    item["price_source"] = f"ebay ({gkey})"
                    break
            else:
                item["current_price"] = lookup.get("current_price")
                item["price_source"] = lookup.get("source", "")
        elif lookup:
            item["current_price"] = lookup.get("current_price")
            item["price_source"] = lookup.get("source", "")
        else:
            item["current_price"] = None
            item["price_source"] = ""

        # Calculate P&L per item
        qty = item.get("quantity", 1)
        cost_each = item.get("purchase_price", 0)
        item["total_cost"] = round(cost_each * qty, 2)
        if item.get("current_price"):
            item["total_value"] = round(item["current_price"] * qty, 2)
            item["gain_loss"] = round(item["total_value"] - item["total_cost"], 2)
            item["gain_pct"] = round((item["gain_loss"] / item["total_cost"]) * 100, 1) if item["total_cost"] > 0 else 0
        else:
            item["total_value"] = None
            item["gain_loss"] = None
            item["gain_pct"] = None

        total_cost += item["total_cost"]
        if item.get("total_value"):
            total_value += item["total_value"]

    portfolio["total_cost"] = round(total_cost, 2)
    portfolio["total_value"] = round(total_value, 2)
    portfolio["total_gain"] = round(total_value - total_cost, 2)
    portfolio["total_gain_pct"] = round(((total_value - total_cost) / total_cost) * 100, 1) if total_cost > 0 else 0

    return portfolio

@app.get("/api/portfolio")
async def get_portfolio():
    portfolio = _load_portfolio()
    return _enrich_portfolio(portfolio)

@app.post("/api/portfolio")
async def add_portfolio_item(body: dict):
    portfolio = _load_portfolio()
    item = {
        "id": len(portfolio["items"]),
        "name": body.get("name", ""),
        "category": body.get("category", "single"),  # single, comic, product
        "quantity": body.get("quantity", 1),
        "purchase_price": body.get("purchase_price", 0),
        "purchase_date": body.get("purchase_date", ""),
        "condition": body.get("condition", ""),
        "printing": body.get("printing", ""),
        "grade": body.get("grade", ""),
        "notes": body.get("notes", ""),
    }
    portfolio["items"].append(item)
    _save_portfolio(portfolio)
    return {"status": "ok", "item": item}

@app.put("/api/portfolio/{item_id}")
async def update_portfolio_item(item_id: int, body: dict):
    portfolio = _load_portfolio()
    items = portfolio.get("items", [])
    if item_id < 0 or item_id >= len(items):
        raise HTTPException(status_code=404, detail="Item not found")
    for k in ("name", "category", "quantity", "purchase_price", "purchase_date",
              "condition", "printing", "grade", "notes"):
        if k in body:
            items[item_id][k] = body[k]
    _save_portfolio(portfolio)
    return {"status": "ok"}

@app.delete("/api/portfolio/{item_id}")
async def delete_portfolio_item(item_id: int):
    portfolio = _load_portfolio()
    items = portfolio.get("items", [])
    if item_id < 0 or item_id >= len(items):
        raise HTTPException(status_code=404, detail="Item not found")
    items.pop(item_id)
    # Re-index
    for i, item in enumerate(items):
        item["id"] = i
    _save_portfolio(portfolio)
    return {"status": "ok"}

# ------------------------------------------------------------------
# Research Agent
# ------------------------------------------------------------------

_research_running = False

@app.get("/api/research/findings")
async def get_findings(status: str = "", limit: int = 50, offset: int = 0):
    return await price_db.get_research_findings(status=status, limit=limit, offset=offset)

@app.put("/api/research/findings/{finding_id}/status")
async def update_finding(finding_id: int, body: dict):
    new_status = body.get("status", "")
    ok = await price_db.update_finding_status(finding_id, new_status)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid status or finding not found")
    return {"status": "ok"}

@app.get("/api/research/runs")
async def get_runs(limit: int = 20):
    return await price_db.get_research_runs(limit=limit)

@app.post("/api/research/run")
async def trigger_research_run(body: dict = {}):
    global _research_running
    if _research_running:
        raise HTTPException(status_code=409, detail="Research agent is already running")

    config = load_config()
    research_cfg = config.get("research", {})
    db_path = str(price_db.DB_PATH)

    from research.agent import ResearchRunConfig, run_research_async

    cfg = ResearchRunConfig(
        db_path=db_path,
        lookback_days=body.get("lookback_days", research_cfg.get("lookback_days", 7)),
        max_findings=body.get("max_findings", research_cfg.get("max_findings", 7)),
        model=research_cfg.get("model", "claude-sonnet-4-5"),
        api_key=research_cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY"),
        codebase_root=Path(__file__).parent.parent,
    )

    async def _run():
        global _research_running
        _research_running = True
        try:
            summary = await run_research_async(cfg)
            print(f"  Research run complete: {summary.get('findings_written', 0)} findings")
            await app_state.ws.broadcast({"type": "research_complete", "summary": summary})
        except Exception as e:
            print(f"  Research run failed: {e}")
            await app_state.ws.broadcast({"type": "research_error", "error": str(e)})
        finally:
            _research_running = False

    asyncio.create_task(_run())
    return {"status": "started"}

# ------------------------------------------------------------------
# Discord Gateway
# ------------------------------------------------------------------

@app.get("/api/discord/gateway-status")
async def discord_gateway_status():
    return {
        "state": _discord_gw.login_state,
        "connected": _discord_gw.connected,
        "error": _discord_gw.error_message,
    }

@app.post("/api/discord/2fa")
async def discord_2fa(body: dict):
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="No code provided")
    _discord_gw.submit_2fa_code(code)
    return {"status": "ok"}

@app.get("/api/research/status")
async def research_status():
    return {"running": _research_running}


async def research_loop():
    """Background loop — runs research agent on a configurable schedule."""
    await asyncio.sleep(60)  # Let app fully start before first check
    while True:
        try:
            config = load_config()
            research_cfg = config.get("research", {})
            if not research_cfg.get("enabled", False):
                await asyncio.sleep(3600)
                continue

            interval_hours = research_cfg.get("interval_hours", 168)  # default weekly
            last_run = None

            # Check last run time
            runs = await price_db.get_research_runs(limit=1)
            if runs:
                from datetime import datetime as _dt
                try:
                    last_ts = runs[0].get("timestamp", "")
                    last_run = _dt.strptime(last_ts, "%Y-%m-%d_%H%M%S")
                except (ValueError, KeyError):
                    pass

            if last_run:
                from datetime import timedelta as _td
                next_run = last_run + _td(hours=interval_hours)
                now = _dt.utcnow()
                if now < next_run:
                    wait_secs = min((next_run - now).total_seconds(), 3600)
                    await asyncio.sleep(wait_secs)
                    continue

            # Time to run
            global _research_running
            if _research_running:
                await asyncio.sleep(300)
                continue

            db_path = str(price_db.DB_PATH)
            from research.agent import ResearchRunConfig, run_research_async

            cfg = ResearchRunConfig(
                db_path=db_path,
                lookback_days=research_cfg.get("lookback_days", 7),
                max_findings=research_cfg.get("max_findings", 7),
                model=research_cfg.get("model", "claude-sonnet-4-5"),
                api_key=research_cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY"),
                codebase_root=Path(__file__).parent.parent,
            )

            _research_running = True
            try:
                summary = await run_research_async(cfg)
                print(f"  [Research] Scheduled run complete: {summary.get('findings_written', 0)} findings")
                await app_state.ws.broadcast({"type": "research_complete", "summary": summary})
            except Exception as e:
                print(f"  [Research] Scheduled run failed: {e}")
            finally:
                _research_running = False

            await asyncio.sleep(3600)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"  [Research] Loop error: {e}")
            await asyncio.sleep(3600)
