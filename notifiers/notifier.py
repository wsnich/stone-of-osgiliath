import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path

try:
    from plyer import notification as desktop_notification
    PLYER_AVAILABLE = True
except Exception:
    PLYER_AVAILABLE = False

try:
    import winsound
    WINSOUND_AVAILABLE = True
except ImportError:
    WINSOUND_AVAILABLE = False

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, config: dict):
        self.cfg = config.get("notifications", {})
        self.log_file = self.cfg.get("log_file", "found_deals.log")
        self._alerted: set[str] = set()  # deduplicate alerts within session

    def _make_key(self, product_name: str, price: float) -> str:
        # Only re-alert if price changes by more than $1 or it's been restarted
        return f"{product_name}:{int(price)}"

    async def alert(self, title: str, body: str, product_name: str = "", price: float = 0.0, url: str = ""):
        key = self._make_key(product_name, price)
        if key in self._alerted:
            log.debug(f"Suppressing duplicate alert for: {key}")
            return
        self._alerted.add(key)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_msg = f"[{timestamp}] {title}: {body}"
        if url:
            full_msg += f"\n  URL: {url}"

        # Always print to console
        print(f"\n{'='*60}")
        print(f"  DEAL FOUND!")
        print(f"  {title}")
        print(f"  {body}")
        if url:
            print(f"  {url}")
        print(f"  {timestamp}")
        print(f"{'='*60}\n")

        # Log to file
        if self.cfg.get("log_to_file", True):
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(full_msg + "\n")

        # Desktop notification
        if self.cfg.get("desktop", True) and PLYER_AVAILABLE:
            try:
                desktop_notification.notify(
                    title=title,
                    message=body[:256],
                    app_name="MTG Monitor",
                    timeout=15,
                )
            except Exception as e:
                log.warning(f"Desktop notification failed: {e}")

        # Sound alert (Windows)
        if self.cfg.get("sound", True) and WINSOUND_AVAILABLE:
            try:
                for _ in range(3):
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    await asyncio.sleep(0.4)
            except Exception:
                pass

    def clear_alert(self, product_name: str, price: float):
        """Allow re-alerting when a product goes out of stock then back in."""
        key = self._make_key(product_name, price)
        self._alerted.discard(key)
