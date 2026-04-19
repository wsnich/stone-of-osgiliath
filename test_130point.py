"""Test script for 130point.com scraping."""
import asyncio
import sys
import re

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def test():
    from patchright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel="chrome")
        page = await browser.new_page(viewport={"width": 1280, "height": 800})

        await page.goto("https://130point.com/comics/", wait_until="domcontentloaded", timeout=30000)
        for _ in range(10):
            await asyncio.sleep(2)
            t = await page.evaluate("() => document.title")
            if "moment" not in t.lower():
                break
        await asyncio.sleep(2)

        await page.fill("input[placeholder*='Search']", "Black Cat 1 Le Chat Noir Campbell")
        await page.keyboard.press("Enter")
        await asyncio.sleep(6)

        # Click Sold tab
        try:
            sold_btn = page.locator("text=Sold")
            if await sold_btn.first.is_visible(timeout=3000):
                await sold_btn.first.click()
                await asyncio.sleep(3)
        except Exception:
            pass

        # Get full page text and parse it
        text = await page.evaluate("() => document.body.innerText")

        # Find price lines
        lines = text.split("\n")
        current_title = ""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            price_match = re.search(r"\$([\d,]+\.?\d{0,2})\s*USD", line)
            if price_match:
                print(f"  ${price_match.group(1):>10}  {current_title[:70]}")
            elif len(line) > 20 and "Sort" not in line and "Marketplace" not in line:
                current_title = line

        await browser.close()


asyncio.run(test())
