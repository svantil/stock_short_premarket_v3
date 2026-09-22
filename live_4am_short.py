#!/usr/bin/env python3
"""Run 4am short without a dashboard; new entries require explicit --start."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import signal
import sys
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent


async def supervise(settings, *, start: bool, interval: float) -> None:
    if settings.demo:
        from four_am_short.live.demo import DemoEngine
        engine = DemoEngine(settings)
    else:
        from four_am_short.live.engine import LiveEngine
        engine = LiveEngine(settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        except (NotImplementedError, RuntimeError):
            # asyncio.run still handles Ctrl+C on platforms without these hooks.
            pass

    try:
        await engine.open()
        if start:
            await engine.start()
        else:
            print("New entries paused. Restored exposure is supervised; use --start to enable discovery/entries.", flush=True)
        while not stop.is_set():
            state = engine.snapshot()
            counts = state.get("counts", {})
            data = state.get("data", {})
            broker = state.get("broker", {})
            clock = datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
            print(
                f"{clock} | {state.get('mode', 'monitor')} | running={state.get('running', False)} "
                f"entries={state.get('entries_enabled', False)} | SIP={data.get('ready', False)} "
                f"DAS={broker.get('connected', False)} | gaps={counts.get('qualified', 0)} "
                f"entered={counts.get('entered', 0)} open={counts.get('open', 0)} "
                f"closed={counts.get('closed', 0)}",
                flush=True,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        state = engine.snapshot()
        open_count = state.get("counts", {}).get("open", 0)
        if open_count and not settings.demo:
            print(f"Graceful shutdown: {open_count} tracked open positions; requesting cancels/covers before disconnecting. Verify DAS if exposure remains unresolved.", file=sys.stderr, flush=True)
        await engine.close()
        for sig in registered:
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="4am short headless supervisor. Orders use DAS; Alpaca supplies SIP data only.")
    parser.add_argument("--config", type=Path, default=PROJECT / "live_4am_short.json", help="Live JSON configuration (default: live_4am_short.json).")
    parser.add_argument("--start", action="store_true", help="Explicitly enable discovery and new entries in the configured mode.")
    parser.add_argument("--demo", action="store_true", help="Use sample data only; no market or broker connections.")
    parser.add_argument("--status-interval", type=float, default=30.0, help="Seconds between terminal status lines (default: 30).")
    parser.add_argument("--validate-only", action="store_true", help="Validate settings without opening state or connections.")
    args = parser.parse_args(argv)
    import math
    if not math.isfinite(args.status_interval) or args.status_interval <= 0:
        parser.error("--status-interval must be a positive finite number")

    from four_am_short.live.config import load_live_settings
    try:
        settings = load_live_settings(args.config)
        if args.demo:
            settings = replace(settings, demo=True, mode="monitor")
        print(f"4am short | {'DEMO — sample data only' if settings.demo else settings.mode}")
        print(f"Shared rules: {settings.strategy_config_path}")
        if args.validate_only:
            print("Configuration valid. No market or broker connections opened.")
            return 0
        asyncio.run(supervise(settings, start=args.start, interval=args.status_interval))
    except KeyboardInterrupt:
        return 130
    except ImportError:
        print("Install live dependencies: python3 -m pip install -r requirements.txt", file=sys.stderr)
        return 2
    except Exception as exc:
        # Exception messages from external transports can contain credentials.
        print(f"4am short could not continue ({type(exc).__name__}). Check configuration and the saved activity log.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
