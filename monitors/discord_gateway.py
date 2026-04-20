"""
Discord Gateway monitor via browser WebSocket interception.

Launches a visible browser window, lets the user log into Discord manually,
then intercepts Gateway WebSocket messages via a send() hook injected into
the page and Worker contexts.

This looks like a normal user with Discord open in a browser — because it is.
Messages arrive in real-time via MESSAGE_CREATE events.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from monitors.defaults import get_user_agent, get_browser_channel

log = logging.getLogger("discord_gw")

_OP_DISPATCH = 0
_SESSION_FILE = "discord_session.json"

_SEND_HOOK_JS = """() => {
    var ctx = (typeof window !== 'undefined') ? window : self;
    ctx._gwBuffer = [];
    ctx._gwConnected = false;
    ctx._gwDebug = [];
    ctx._gwSocket = null;

    var _origSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function(data) {
        if (!ctx._gwSocket && this.url &&
            this.url.indexOf('gateway') !== -1 &&
            this.url.indexOf('discord') !== -1) {
            ctx._gwSocket = this;
            ctx._gwConnected = true;
            ctx._gwDebug.push('HOOKED:' + this.url.substring(0, 60));

            this.addEventListener('message', function(e) {
                try {
                    if (typeof e.data === 'string') {
                        ctx._gwBuffer.push(e.data);
                        if (ctx._gwBuffer.length > 500)
                            ctx._gwBuffer = ctx._gwBuffer.slice(-250);
                    } else {
                        ctx._gwDebug.push('BINARY:' + typeof e.data);
                    }
                } catch(x) { ctx._gwDebug.push('ERR:' + x); }
            });

            this.addEventListener('close', function() {
                ctx._gwConnected = false;
                ctx._gwSocket = null;
                ctx._gwDebug.push('CLOSED');
            });
        }
        return _origSend.apply(this, arguments);
    };
    ctx._gwDebug.push('HOOK_INSTALLED');
}"""

_DRAIN_JS = """() => {
    var ctx = (typeof window !== 'undefined') ? window : self;
    var msgs = ctx._gwBuffer || [];
    var connected = ctx._gwConnected || false;
    var debug = ctx._gwDebug || [];
    ctx._gwBuffer = [];
    ctx._gwDebug = [];
    return { msgs: msgs, connected: connected, debug: debug };
}"""


class DiscordGatewayMonitor:
    """Monitors Discord via browser Gateway WebSocket interception."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        self.discord_queue: asyncio.Queue = asyncio.Queue()
        self.marketplace_queue: asyncio.Queue = asyncio.Queue()

        self._discord_channels: set[str] = set()
        self._marketplace_sell_channels: set[str] = set()
        self._marketplace_buy_channels: set[str] = set()

        self.channel_names: dict[str, str] = {}

        self._running = False
        self._connected = False
        self._login_state = "not_started"
        self._last_ws_activity = 0.0
        self._error_message: str = ""
        self._frame_count = 0
        self._worker_handle = None
        self._hooks_injected = False
        self._channel_pages: dict[str, any] = {}  # channel_id -> page

        self._data_dir: Path = Path(".")

    @property
    def login_state(self) -> str:
        return self._login_state

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def error_message(self) -> str:
        return self._error_message

    def set_data_dir(self, path: Path):
        self._data_dir = path

    @staticmethod
    def _parse_channel(value: str) -> tuple[str, str]:
        """Parse a channel config value into (guild_id, channel_id).

        Accepts:
          - Full URL: https://discord.com/channels/1234/5678
          - Slash pair: 1234/5678
          - Plain channel ID: 5678 (guild_id will be empty)
        """
        value = str(value).strip()
        if "discord.com/channels/" in value:
            parts = value.split("/channels/")[1].split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
        if "/" in value:
            parts = value.split("/")
            return parts[0], parts[1]
        return "", value

    def update_channels(self, config: dict):
        dc = config.get("discord", {})
        self._discord_channels = set()
        self._channel_urls: dict[str, str] = {}  # channel_id -> full URL
        for raw in dc.get("channels_to_monitor", []):
            guild_id, ch_id = self._parse_channel(raw)
            self._discord_channels.add(ch_id)
            if guild_id:
                self._channel_urls[ch_id] = f"https://discord.com/channels/{guild_id}/{ch_id}"

        mp = config.get("marketplace", {})
        self._marketplace_sell_channels = set()
        self._marketplace_buy_channels = set()
        for raw in mp.get("sell_channels", []):
            guild_id, ch_id = self._parse_channel(raw)
            self._marketplace_sell_channels.add(ch_id)
            if guild_id:
                self._channel_urls[ch_id] = f"https://discord.com/channels/{guild_id}/{ch_id}"
        for raw in mp.get("buy_channels", []):
            guild_id, ch_id = self._parse_channel(raw)
            self._marketplace_buy_channels.add(ch_id)
            if guild_id:
                self._channel_urls[ch_id] = f"https://discord.com/channels/{guild_id}/{ch_id}"

    async def start(self, config: dict, stealth_cfg: dict | None = None) -> bool:
        """Launch a visible browser and wait for the user to log into Discord."""
        self.update_channels(config)

        try:
            from patchright.async_api import async_playwright as _playwright
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _playwright
            except ImportError:
                self._login_state = "error"
                self._error_message = "Neither patchright nor playwright installed"
                return False

        self._login_state = "logging_in"
        self._running = True

        try:
            self._pw = await _playwright().start()
            launch_kw = {"headless": False}  # Visible browser for manual login
            channel = get_browser_channel(stealth_cfg)
            if channel:
                launch_kw["channel"] = channel
            self._browser = await self._pw.chromium.launch(**launch_kw)

            # Try restoring session
            session_path = self._data_dir / _SESSION_FILE
            context_kw = {
                "user_agent": get_user_agent(stealth_cfg),
                "viewport": {"width": 1280, "height": 800},
            }
            if session_path.exists():
                try:
                    context_kw["storage_state"] = str(session_path)
                    print("  [Discord GW] Restoring saved session...")
                except Exception:
                    pass

            self._context = await self._browser.new_context(**context_kw)
            self._page = await self._context.new_page()

            # Listen for Workers
            self._page.on("worker", self._on_worker)

            # Navigate to Discord
            await self._page.goto("https://discord.com/channels/@me", timeout=30000)
            await asyncio.sleep(3)

            # Check if we need to log in
            if "/login" in self._page.url or "/register" in self._page.url:
                print("  [Discord GW] *** Please log into Discord in the browser window ***")
                self._login_state = "awaiting_login"

                # Wait for user to complete login (up to 5 minutes)
                for _ in range(300):
                    await asyncio.sleep(1)
                    try:
                        url = self._page.url
                        if "/channels" in url and "/login" not in url:
                            print("  [Discord GW] Login detected!")
                            break
                    except Exception:
                        pass
                else:
                    self._login_state = "error"
                    self._error_message = "Login timed out (5 minutes)"
                    print("  [Discord GW] Login timed out")
                    return False
            else:
                print("  [Discord GW] Already logged in from saved session")

            # Save session for next time
            try:
                await self._context.storage_state(path=str(session_path))
                print("  [Discord GW] Session saved for next restart")
            except Exception:
                pass

            self._login_state = "logged_in"

            # Wait for Discord to fully load, then inject hooks
            print("  [Discord GW] Waiting for Discord to fully load...")
            await asyncio.sleep(5)

            # Open a tab for each monitored channel
            all_channels = self._discord_channels | self._marketplace_sell_channels | self._marketplace_buy_channels
            if all_channels:
                print(f"  [Discord GW] Opening tabs for {len(all_channels)} monitored channel(s)...")
                await self._open_channel_tabs(all_channels)

            self._hooks_injected = True
            self._connected = len(self._channel_pages) > 0

            if self._connected:
                log.info(f"Discord Gateway started — monitoring {len(self._channel_pages)} channels")
            else:
                print("  [Discord GW] WARNING: No channel tabs opened — navigate to your server manually")

            return True

        except Exception as e:
            self._login_state = "error"
            self._error_message = str(e)
            log.error(f"Failed to start: {e}")
            await self.stop()
            return False

    async def _open_channel_tabs(self, channel_ids: set[str]):
        """Open a browser tab for each monitored channel."""
        # Use pre-built URLs from config (guild_id/channel_id format)
        channel_urls = {ch: url for ch, url in self._channel_urls.items() if ch in channel_ids}

        # For channels without a guild_id, try finding them in the sidebar
        missing = channel_ids - set(channel_urls.keys())
        if missing:
            found = await self._page.evaluate("""(chIds) => {
                let result = {};
                let links = document.querySelectorAll('a[href*="/channels/"]');
                for (let a of links) {
                    for (let chId of chIds) {
                        if (a.href.endsWith('/' + chId)) result[chId] = a.href;
                    }
                }
                return result;
            }""", list(missing))
            channel_urls.update(found or {})

        still_missing = channel_ids - set(channel_urls.keys())
        if still_missing:
            print(f"  [Discord GW] Channels missing guild ID (use server_id/channel_id format): {list(still_missing)}")

        print(f"  [Discord GW] Opening {len(channel_urls)} channel tab(s):")
        for ch_id, url in channel_urls.items():
            print(f"  [Discord GW]   {ch_id} -> {url}")

        if not channel_urls:
            print("  [Discord GW] No channels to open — check your channel config format")
            print("  [Discord GW] Use: https://discord.com/channels/SERVER_ID/CHANNEL_ID")
            return

        # Navigate the main page to the first channel
        first_id = next(iter(channel_urls))
        first_url = channel_urls[first_id]
        await self._page.goto(first_url, timeout=15000)
        await asyncio.sleep(3)
        await self._init_page_polling(self._page, first_id)
        self._channel_pages[first_id] = self._page

        # Open new tabs for remaining channels
        for ch_id, url in channel_urls.items():
            if ch_id == first_id:
                continue
            try:
                page = await self._context.new_page()
                await page.goto(url, timeout=15000)
                await asyncio.sleep(2)
                await self._init_page_polling(page, ch_id)
                self._channel_pages[ch_id] = page
                ch_name = self.channel_names.get(ch_id, ch_id[-4:])
                print(f"  [Discord GW] Opened tab for #{ch_name} ({ch_id})")
            except Exception as e:
                print(f"  [Discord GW] Failed to open tab for {ch_id}: {e}")

    async def _init_page_polling(self, page, channel_id: str):
        """Initialize the seen-message set on a page and extract channel name."""
        try:
            result = await page.evaluate("""(chId) => {
                window._gwSeenIds = new Set();
                window._gwDebug = [];
                window._gwChannelId = chId;
                let msgEls = document.querySelectorAll('[id^="chat-messages-"]');
                for (let el of msgEls) {
                    let id = el.id.replace('chat-messages-', '');
                    if (id) window._gwSeenIds.add(id);
                }
                // Extract channel name from the header
                let nameEl = document.querySelector('h1[class*="title_"]') ||
                             document.querySelector('[class*="channelName_"]') ||
                             document.querySelector('h1');
                let name = nameEl ? nameEl.textContent.trim().replace(/^#/, '') : '';
                return { name: name };
            }""", channel_id)
            if result and result.get("name"):
                self.channel_names[channel_id] = result["name"]
        except Exception:
            pass

    async def _navigate_to_channel(self, channel_id: str):
        """Navigate to a Discord channel by finding its full URL in the page."""
        try:
            # Search all links in the page for one ending with this channel ID
            url = await self._page.evaluate("""(chId) => {
                // Search all links for one containing the channel ID
                let links = document.querySelectorAll('a[href*="/channels/"]');
                for (let a of links) {
                    if (a.href.endsWith('/' + chId)) return a.href;
                }
                // Also check data attributes
                let el = document.querySelector('[data-list-item-id="channels___' + chId + '"]');
                if (el) {
                    let a = el.closest('a') || el.querySelector('a');
                    if (a) return a.href;
                }
                return null;
            }""", channel_id)

            if url:
                await self._page.goto(url, timeout=15000)
                await asyncio.sleep(3)
                print(f"  [Discord GW] Navigated to channel {channel_id}")
                return

            # Channel not found in DOM — the user may need to switch to the right server first
            print(f"  [Discord GW] Channel {channel_id} not found — navigate to it manually in the browser")
        except Exception as e:
            print(f"  [Discord GW] Channel navigation failed: {e}")

    def _on_worker(self, worker):
        print(f"  [Discord GW] Worker created: {worker.url[:80]}")
        self._worker_handle = worker

    async def _inject_hooks(self) -> bool:
        """No longer needed — tabs are initialized in _open_channel_tabs."""
        self._hooks_injected = True
        return len(self._channel_pages) > 0

    async def poll_gateway_messages(self):
        """Scan all channel tabs for new messages."""
        pages = list(self._channel_pages.items())
        if not pages:
            return
        for ch_id, page in pages:
            await self._poll_page(page, ch_id)

    async def _poll_page(self, page, channel_id: str):
        """Scan a single page tab for new messages."""
        try:
            result = await page.evaluate("""(chId) => {
                // Initialize seen set if needed
                if (!window._gwSeenIds) window._gwSeenIds = new Set();
                if (!window._gwDebug) window._gwDebug = [];

                // Use passed channel ID, fallback to URL
                let currentChannel = chId || '';
                if (!currentChannel) {
                    let urlParts = location.pathname.split('/');
                    currentChannel = urlParts[urlParts.length - 1] || '';
                }

                // Find all message elements currently in the DOM
                let msgEls = document.querySelectorAll('[id^="chat-messages-"]');
                let newMsgs = [];

                for (let el of msgEls) {
                    let msgId = el.id.replace('chat-messages-', '');
                    if (!msgId || window._gwSeenIds.has(msgId)) continue;
                    window._gwSeenIds.add(msgId);

                    try {
                        // Extract author
                        let authorEl = el.querySelector('[class*="username_"]') ||
                                       el.querySelector('[class*="headerText_"] [class*="username"]') ||
                                       el.querySelector('h3 span');
                        let author = authorEl ? authorEl.textContent.trim() : '';

                        // For grouped messages (no header), inherit from previous
                        if (!author) {
                            let prev = el.previousElementSibling;
                            while (prev && !author) {
                                let prevAuthor = prev.querySelector('[class*="username_"]') ||
                                                 prev.querySelector('h3 span');
                                if (prevAuthor) author = prevAuthor.textContent.trim();
                                prev = prev.previousElementSibling;
                            }
                        }

                        // Extract message content (preserve links as markdown)
                        let contentEl = el.querySelector('[class*="messageContent_"]') ||
                                        el.querySelector('[class*="messageContent"]');
                        let content = '';
                        if (contentEl) {
                            // Replace <a> tags with markdown links before getting text
                            let clone = contentEl.cloneNode(true);
                            clone.querySelectorAll('a[href]').forEach(a => {
                                let md = '[' + a.textContent + '](' + a.href + ')';
                                a.replaceWith(md);
                            });
                            content = clone.textContent.trim();
                        }

                        // Extract timestamp
                        let timeEl = el.querySelector('time');
                        let timestamp = timeEl ? (timeEl.getAttribute('datetime') || '') : '';

                        // Extract embeds
                        let embeds = [];
                        let embedEls = el.querySelectorAll('[class*="embedWrapper_"], [class*="embed_"]');
                        for (let embedEl of embedEls) {
                            let title = '';
                            let description = '';
                            let url = '';
                            let fields = {};
                            let image = '';

                            let tEl = embedEl.querySelector('[class*="embedTitle_"], [class*="embedAuthorName"]');
                            if (tEl) title = tEl.textContent.trim();

                            let dEl = embedEl.querySelector('[class*="embedDescription_"]');
                            if (dEl) description = dEl.textContent.trim();

                            let titleLink = (tEl && tEl.closest('a')) || embedEl.querySelector('[class*="embedTitle"] a[href]');
                            if (titleLink) url = titleLink.href;
                            if (!url) {
                                let aEl = embedEl.querySelector('a[href]');
                                if (aEl) url = aEl.href;
                            }

                            let imgEl = embedEl.querySelector('img[src]');
                            if (imgEl) image = imgEl.src;

                            let fieldsList = [];
                            embedEl.querySelectorAll('[class*="embedField_"]').forEach(fEl => {
                                let nEl = fEl.querySelector('[class*="embedFieldName"]');
                                let vEl = fEl.querySelector('[class*="embedFieldValue"]');
                                if (nEl && vEl) {
                                    // Preserve markdown-style links from <a> tags
                                    let val = '';
                                    let links = vEl.querySelectorAll('a[href]');
                                    if (links.length > 0) {
                                        let parts = [];
                                        links.forEach(a => {
                                            parts.push('[' + a.textContent.trim() + '](' + a.href + ')');
                                        });
                                        val = parts.join(' | ');
                                    } else {
                                        val = vEl.textContent.trim();
                                    }
                                    fieldsList.push({ name: nEl.textContent.trim(), value: val });
                                }
                            });

                            if (title || description) {
                                let e = { title, description, url };
                                if (image) e.thumbnail = { url: image };
                                if (fieldsList.length) e.fields = fieldsList;
                                embeds.push(e);
                            }
                        }

                        if (!content && embeds.length === 0) continue;

                        newMsgs.push(JSON.stringify({
                            op: 0, t: 'MESSAGE_CREATE',
                            d: {
                                id: msgId,
                                channel_id: currentChannel,
                                author: { username: author, id: '', avatar: '' },
                                content: content,
                                timestamp: timestamp,
                                embeds: embeds,
                            }
                        }));
                    } catch(e) {
                        window._gwDebug.push('EXTRACT_ERR:' + e.message);
                    }
                }

                return {
                    msgs: newMsgs,
                    connected: true,
                    debug: window._gwDebug.splice(0),
                    channel: currentChannel,
                    totalSeen: window._gwSeenIds.size,
                };
            }""", channel_id)

            for dbg in result.get("debug", []):
                print(f"  [Discord GW] {dbg}")

            self._connected = True
            msgs = result.get("msgs", [])
            if msgs:
                self._last_ws_activity = time.time()
            for raw in msgs:
                self._process_raw_message(raw)
        except Exception:
            pass  # Tab might be loading or navigating

    def _process_raw_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        op = msg.get("op")
        event_type = msg.get("t")
        self._frame_count += 1
        if op == _OP_DISPATCH:
            if event_type == "MESSAGE_CREATE":
                self._handle_message_create(msg.get("d", {}))
            elif event_type == "READY":
                self._handle_ready(msg.get("d", {}))
            elif event_type == "GUILD_CREATE":
                self._handle_guild_create(msg.get("d", {}))

    def _handle_ready(self, data: dict):
        count = 0
        for guild in data.get("guilds", []):
            for ch in guild.get("channels", []):
                cid = str(ch.get("id", ""))
                name = ch.get("name", "")
                if cid and name:
                    self.channel_names[cid] = name
                    count += 1
        log.info(f"Gateway READY — cached {count} channel names")
        print(f"  [Discord GW] READY: {count} channel names from {len(data.get('guilds', []))} guilds")

    def _handle_guild_create(self, data: dict):
        for ch in data.get("channels", []):
            cid = str(ch.get("id", ""))
            name = ch.get("name", "")
            if cid and name:
                self.channel_names[cid] = name

    def _handle_message_create(self, data: dict):
        channel_id = str(data.get("channel_id", ""))
        if channel_id in self._discord_channels:
            try:
                self.discord_queue.put_nowait(data)
            except asyncio.QueueFull:
                pass
        if channel_id in self._marketplace_sell_channels or channel_id in self._marketplace_buy_channels:
            try:
                self.marketplace_queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def drain_discord_queue(self) -> list[dict]:
        messages = []
        while not self.discord_queue.empty():
            try:
                messages.append(self.discord_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def drain_marketplace_queue(self) -> list[dict]:
        messages = []
        while not self.marketplace_queue.empty():
            try:
                messages.append(self.marketplace_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def stop(self):
        self._running = False
        self._connected = False
        try:
            if self._context:
                try:
                    await self._context.storage_state(
                        path=str(self._data_dir / _SESSION_FILE))
                except Exception:
                    pass
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None

    async def check_health(self) -> bool:
        if not self._running or not self._browser:
            return False
        try:
            if "/login" in self._page.url:
                return False
        except Exception:
            return False
        if self._last_ws_activity and (time.time() - self._last_ws_activity > 120):
            return False
        return True

    async def restart(self, config: dict, stealth_cfg: dict | None = None) -> bool:
        await self.stop()
        await asyncio.sleep(2)
        return await self.start(config, stealth_cfg)
