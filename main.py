#!/usr/bin/env python3
"""
The Stone of Osgiliath — Web UI launcher
Open http://localhost:8888 in Chrome after running this.

Usage:
    python main.py
    python main.py --port 8888
    python main.py --no-browser
"""

import argparse
import sys
import webbrowser
import threading
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    parser = argparse.ArgumentParser(description="The Stone of Osgiliath web UI")
    parser.add_argument("--port",       type=int, default=8888, help="Port to listen on (default 8888)")
    parser.add_argument("--host",       default="127.0.0.1",   help="Host to bind to")
    parser.add_argument("--no-browser", action="store_true",   help="Don't open browser automatically")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"

    if not args.no_browser:
        def open_browser():
            time.sleep(1.5)   # give uvicorn a moment to start
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    print(f"\n  The Stone of Osgiliath")
    print(f"  Open in Chrome: {url}")
    print(f"  Press Ctrl+C to stop\n")

    import uvicorn
    uvicorn.run(
        "web.app:app",
        host=args.host,
        port=args.port,
        log_level="warning",   # keep console clean; logs appear in the UI
    )


if __name__ == "__main__":
    main()
