"""
Discord Gateway monitor via browser WebSocket interception.

Instead of polling the REST API with a user token (which Discord flags
as suspicious), this launches a headless browser, logs into Discord web,
and intercepts the Gateway WebSocket frames the client naturally opens.

This looks like a normal user with Discord open in a browser tab.
Messages arrive in real-time via MESSAGE_CREATE events.
"""

import asyncio
import json
import logging
import re
import time
import zlib
from pathlib import Path
from typing import Optional

from monitors.defaults import get_user_agent, get_browser_channel

log = logging.getLogger("discord_gw")

# Gateway opcodes
_OP_DISPATCH = 0
_OP_HEARTBEAT_ACK = 11

# Session file for cookie/localStorage persistence
_SESSION_FILE = "discord_session.json"


class DiscordGatewayMonitor:
    """Monitors Discord via browser Gateway WebSocket interception."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        # Message queues for consumers
        self.discord_queue: asyncio.Queue = asyncio.Queue()
        self.marketplace_queue: asyncio.Queue = asyncio.Queue()

        # Channel routing sets (populated from config)
        self._discord_channels: set[str] = set()
        self._marketplace_sell_channels: set[str] = set()
        self._marketplace_buy_channels: set[str] = set()

        # Channel name cache (populated from Gateway READY/GUILD_CREATE)
        self.channel_names: dict[str, str] = {}

        # State
        self._running = False
        self._connected = False
        self._login_state = "not_started"  # not_started|logging_in|awaiting_2fa|logged_in|error
        self._last_ws_activity = 0.0
        self._ws_ref = None  # Reference to the Gateway WebSocket
        self._inflator = None  # zlib decompressor for Gateway frames
        self._error_message: str = ""

        # 2FA code (set via API when user provides it)
        self._2fa_code: Optional[str] = None
        self._2fa_event: asyncio.Event = asyncio.Event()

        # Data dir for session persistence
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

    def update_channels(self, config: dict):
        """Update channel routing from config."""
        dc = config.get("discord", {})
        self._discord_channels = set(str(c) for c in dc.get("channels_to_monitor", []))
        mp = config.get("marketplace", {})
        self._marketplace_sell_channels = set(str(c) for c in mp.get("sell_channels", []))
        self._marketplace_buy_channels = set(str(c) for c in mp.get("buy_channels", []))

    async def start(self, config: dict, stealth_cfg: dict | None = None) -> bool:
        """Launch browser, log in to Discord, begin WebSocket interception."""
        dc = config.get("discord", {})
        email = dc.get("email", "")
        password = dc.get("password", "")

        if not email or not password:
            self._login_state = "error"
            self._error_message = "Discord email/password not configured"
            log.error(self._error_message)
            return False

        self.update_channels(config)

        try:
            from patchright.async_api import async_playwright as _playwright
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _playwright
            except ImportError:
                self._login_state = "error"
                self._error_message = "Neither patchright nor playwright installed"
                log.error(self._error_message)
                return False

        self._login_state = "logging_in"
        self._running = True

        try:
            self._pw = await _playwright().start()
            launch_kw = {"headless": True}
            channel = get_browser_channel(stealth_cfg)
            if channel:
                launch_kw["channel"] = channel
            self._browser = await self._pw.chromium.launch(**launch_kw)

            # Try to restore session from saved cookies
            session_path = self._data_dir / _SESSION_FILE
            context_kw = {
                "user_agent": get_user_agent(stealth_cfg),
                "viewport": {"width": 1280, "height": 800},
            }
            if session_path.exists():
                try:
                    context_kw["storage_state"] = str(session_path)
                    log.info("Restoring Discord session from saved state")
                except Exception:
                    pass

            self._context = await self._browser.new_context(**context_kw)
            self._page = await self._context.new_page()

            # Set up WebSocket interception BEFORE navigating
            self._page.on("websocket", self._on_websocket)

            # Try navigating to channels (may already be logged in from session)
            await self._page.goto("https://discord.com/channels/@me", timeout=30000)
            await asyncio.sleep(3)

            # Check if we're logged in or need to log in
            if "/login" in self._page.url:
                log.info("Session expired or first login, entering credentials")
                success = await self._do_login(email, password)
                if not success:
                    return False
            else:
                log.info("Restored Discord session — already logged in")

            # Save session for next time
            try:
                await self._context.storage_state(path=str(session_path))
            except Exception as e:
                log.debug(f"Could not save session state: {e}")

            self._login_state = "logged_in"
            self._connected = True
            log.info("Discord Gateway monitor started")
            return True

        except Exception as e:
            self._login_state = "error"
            self._error_message = str(e)
            log.error(f"Failed to start Discord Gateway monitor: {e}")
            await self.stop()
            return False

    async def _do_login(self, email: str, password: str) -> bool:
        """Fill login form and handle 2FA if needed."""
        page = self._page
        try:
            # Navigate to login page if not already there
            if "/login" not in page.url:
                await page.goto("https://discord.com/login", timeout=20000)
                await asyncio.sleep(2)

            # Fill email
            email_input = await page.wait_for_selector('input[name="email"]', timeout=10000)
            await email_input.fill(email)

            # Fill password
            password_input = await page.wait_for_selector('input[name="password"]', timeout=5000)
            await password_input.fill(password)

            # Click login button
            await page.click('button[type="submit"]')

            # Wait for navigation or 2FA prompt
            for _ in range(30):
                await asyncio.sleep(1)
                url = page.url

                # Success — redirected to channels
                if "/channels" in url and "/login" not in url:
                    log.info("Discord login successful")
                    return True

                # Check for 2FA/MFA prompt
                mfa_input = await page.query_selector('input[placeholder*="code" i], input[aria-label*="code" i], input[autocomplete="one-time-code"]')
                if mfa_input:
                    return await self._handle_2fa(mfa_input)

                # Check for error message
                error_el = await page.query_selector('[class*="error" i]')
                if error_el:
                    error_text = await error_el.text_content()
                    if error_text and "password" in error_text.lower():
                        self._login_state = "error"
                        self._error_message = "Invalid email or password"
                        log.error(self._error_message)
                        return False

            self._login_state = "error"
            self._error_message = "Login timed out"
            log.error("Discord login timed out after 30 seconds")
            return False

        except Exception as e:
            self._login_state = "error"
            self._error_message = f"Login failed: {e}"
            log.error(self._error_message)
            return False

    async def _handle_2fa(self, mfa_input) -> bool:
        """Handle 2FA prompt — wait for code from user via API."""
        log.info("2FA required — waiting for code from user")
        self._login_state = "awaiting_2fa"
        self._2fa_event.clear()
        self._2fa_code = None

        # Wait up to 5 minutes for user to provide code
        try:
            await asyncio.wait_for(self._2fa_event.wait(), timeout=300)
        except asyncio.TimeoutError:
            self._login_state = "error"
            self._error_message = "2FA code not provided within 5 minutes"
            log.error(self._error_message)
            return False

        if not self._2fa_code:
            self._login_state = "error"
            self._error_message = "No 2FA code provided"
            return False

        # Enter the code
        try:
            await mfa_input.fill(self._2fa_code)
            # Try pressing Enter or clicking submit
            await mfa_input.press("Enter")

            # Wait for redirect to channels
            for _ in range(15):
                await asyncio.sleep(1)
                if "/channels" in self._page.url and "/login" not in self._page.url:
                    log.info("2FA login successful")
                    self._login_state = "logged_in"
                    return True

            self._login_state = "error"
            self._error_message = "2FA code may be incorrect"
            log.error("2FA login did not redirect to channels")
            return False

        except Exception as e:
            self._login_state = "error"
            self._error_message = f"2FA entry failed: {e}"
            log.error(self._error_message)
            return False

    def submit_2fa_code(self, code: str):
        """Called from the API endpoint when user provides 2FA code."""
        self._2fa_code = code.strip()
        self._2fa_event.set()

    def _on_websocket(self, ws):
        """Called when a WebSocket connection is opened by the page."""
        url = ws.url
        # Only intercept the Discord Gateway WebSocket
        if "gateway.discord.gg" not in url and "gateway-us-east" not in url and "gateway" not in url:
            return
        if "discord" not in url:
            return

        log.info(f"Gateway WebSocket detected: {url[:80]}")
        self._ws_ref = ws
        self._inflator = zlib.decompressobj()
        self._connected = True
        self._last_ws_activity = time.time()

        ws.on("framereceived", lambda payload: self._on_ws_frame(payload))
        ws.on("close", lambda _=None: self._on_ws_close())

    def _on_ws_frame(self, payload):
        """Handle a received WebSocket frame."""
        self._last_ws_activity = time.time()

        data = payload
        # payload may be a string (text frame) or bytes (binary frame)
        if isinstance(data, bytes):
            # Try zlib decompression (Discord uses zlib-stream)
            try:
                data = self._inflator.decompress(data)
                data = data.decode("utf-8")
            except Exception:
                try:
                    data = data.decode("utf-8")
                except Exception:
                    return
        elif hasattr(data, 'body'):
            # Playwright wraps frames in an object
            data = data.body if isinstance(data.body, str) else data.body.decode("utf-8", errors="replace")

        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return

        op = msg.get("op")
        event_type = msg.get("t")

        if op == _OP_DISPATCH:
            if event_type == "MESSAGE_CREATE":
                self._handle_message_create(msg.get("d", {}))
            elif event_type == "READY":
                self._handle_ready(msg.get("d", {}))
            elif event_type == "GUILD_CREATE":
                self._handle_guild_create(msg.get("d", {}))

    def _on_ws_close(self):
        """Gateway WebSocket closed."""
        log.warning("Gateway WebSocket closed")
        self._connected = False
        self._inflator = None

    def _handle_ready(self, data: dict):
        """Extract channel names from READY event."""
        log.info("Gateway READY received")
        for guild in data.get("guilds", []):
            for ch in guild.get("channels", []):
                cid = str(ch.get("id", ""))
                name = ch.get("name", "")
                if cid and name:
                    self.channel_names[cid] = name

    def _handle_guild_create(self, data: dict):
        """Extract channel names from GUILD_CREATE event."""
        for ch in data.get("channels", []):
            cid = str(ch.get("id", ""))
            name = ch.get("name", "")
            if cid and name:
                self.channel_names[cid] = name

    def _handle_message_create(self, data: dict):
        """Route MESSAGE_CREATE to the appropriate queue(s)."""
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
        """Drain all pending messages from the discord queue."""
        messages = []
        while not self.discord_queue.empty():
            try:
                messages.append(self.discord_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def drain_marketplace_queue(self) -> list[dict]:
        """Drain all pending messages from the marketplace queue."""
        messages = []
        while not self.marketplace_queue.empty():
            try:
                messages.append(self.marketplace_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    async def stop(self):
        """Shut down the browser and clean up."""
        self._running = False
        self._connected = False
        try:
            if self._context:
                # Save session before closing
                try:
                    session_path = self._data_dir / _SESSION_FILE
                    await self._context.storage_state(path=str(session_path))
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
        log.info("Discord Gateway monitor stopped")

    async def check_health(self) -> bool:
        """Check if the monitor is healthy. Returns False if restart needed."""
        if not self._running or not self._browser:
            return False

        # Check if page is still alive and on Discord
        try:
            url = self._page.url
            if "/login" in url:
                log.warning("Discord session expired (redirected to login)")
                return False
        except Exception:
            log.warning("Browser page not responsive")
            return False

        # Check WebSocket activity (Gateway heartbeats happen every ~40s)
        if self._last_ws_activity and (time.time() - self._last_ws_activity > 120):
            log.warning("No Gateway WebSocket activity for 120s")
            return False

        return True

    async def restart(self, config: dict, stealth_cfg: dict | None = None) -> bool:
        """Stop and restart the monitor."""
        log.info("Restarting Discord Gateway monitor...")
        await self.stop()
        await asyncio.sleep(2)
        return await self.start(config, stealth_cfg)
