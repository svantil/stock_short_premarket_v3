#!/usr/bin/env python3
"""Compatibility launcher; the named entry point is backtest_4am_short.py."""

from four_am_short.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
