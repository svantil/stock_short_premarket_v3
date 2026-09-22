#!/usr/bin/env python3
"""Launch the independent, local 4am short dashboard."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="4am short dashboard: Alpaca SIP data and DAS execution.")
    parser.add_argument("--config", type=Path, default=PROJECT / "live_4am_short.json", help="Live JSON configuration (default: live_4am_short.json).")
    parser.add_argument("--demo", action="store_true", help="Preview sample data; no Alpaca, DAS, or historical requests.")
    parser.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], help="Override the loopback dashboard address.")
    parser.add_argument("--port", type=int, help="Override the dashboard port (default: config value, 8003).")
    parser.add_argument("--validate-only", action="store_true", help="Validate configuration and print mode/rules without connecting.")
    args = parser.parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    from four_am_short.live.config import load_live_settings

    try:
        settings = load_live_settings(args.config)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Cannot load the live configuration: {exc}", file=sys.stderr)
        return 2
    settings = replace(settings, host=args.host or settings.host, port=args.port or settings.port)
    if args.demo:
        settings = replace(settings, demo=True, mode="monitor")
    print(f"4am short | {'DEMO — sample data only' if settings.demo else settings.mode}")
    print(f"Shared rules: {settings.strategy_config_path}")
    print(f"{settings.strategy.shares:,} shares | early {settings.strategy.early_start}–{settings.strategy.early_end} ET | entry before {settings.strategy.entry_deadline} | exit {settings.strategy.time_exit}")
    if args.validate_only:
        print("Configuration valid. No market or broker connections opened.")
        return 0

    try:
        import uvicorn
        from four_am_short.live.app import create_app

        if settings.demo:
            from four_am_short.live.demo import DemoEngine
            app = create_app(settings=settings, engine=DemoEngine(settings))
        else:
            app = create_app(settings=settings)
    except ImportError:
        print("Install live dependencies: python3 -m pip install -r requirements.txt", file=sys.stderr)
        return 2
    host = f"[{settings.host}]" if settings.host == "::1" else settings.host
    print(f"Dashboard: http://{host}:{settings.port}")
    print("New entries start only from the dashboard control. Saved exposure resumes exit supervision.")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
