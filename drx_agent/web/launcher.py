"""Launch the DRX-Operator Web Dashboard.

Usage:
    python -m drx_agent.web.launcher [--port PORT] [--host HOST] [--no-agent]
"""

import argparse
import asyncio
import logging
import os
import sys
import threading
import webbrowser

logger = logging.getLogger(__name__)


def launch_web(*, host: str = "0.0.0.0", port: int = 7300,
               with_agent: bool = True, open_browser: bool = True) -> None:
    """Start the web dashboard server.

    Parameters
    ----------
    host, port:
        Bind address for uvicorn.
    with_agent:
        If True, boot a full DrxAgent and forward its EventBus.
    open_browser:
        Open the browser automatically.
    """
    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn is required for --web mode.")
        print("  pip install uvicorn fastapi websockets")
        sys.exit(1)

    drx_agent = None
    if with_agent:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            from drx_agent.main import DrxAgent
            drx_agent = DrxAgent()
            logger.info("Live agent connected to web dashboard")
        except Exception as exc:
            logger.warning("Could not start live agent: %s — running in replay-only mode", exc)
            drx_agent = None

    from drx_agent.web.server import create_app
    app = create_app(drx_agent=drx_agent)

    url = f"http://{'localhost' if host == '0.0.0.0' else host}:{port}"
    print(f"\n  ╔══════════════════════════════════════════════════╗")
    print(f"  ║  DRX-Operator Web Dashboard                     ║")
    print(f"  ║  {url:<47s} ║")
    print(f"  ║  Mode: {'Live Agent' if drx_agent else 'Replay Only':<40s} ║")
    print(f"  ╚══════════════════════════════════════════════════╝\n")

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    parser = argparse.ArgumentParser(description="DRX-Operator Web Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=7300, help="Bind port (default: 7300)")
    parser.add_argument("--no-agent", action="store_true",
                        help="Start in replay-only mode without a live agent")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open the browser automatically")
    args = parser.parse_args()

    launch_web(
        host=args.host,
        port=args.port,
        with_agent=not args.no_agent,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    main()
