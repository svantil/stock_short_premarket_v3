"""Parse the scanner's date followed by symbols, with optional CSV separators."""

from .config import BacktestConfig, iso_date, symbol
from .models import Candidate, DataError


def read_candidates(config: BacktestConfig, *, content: bytes | None = None) -> list[Candidate]:
    path = config.input_file
    try:
        lines = (path.read_bytes() if content is None else content).decode("utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DataError(f"Cannot read input file {path}: {exc}") from exc
    candidates: dict[tuple, Candidate] = {}
    for line_number, raw in enumerate(lines, 1):
        parts = raw.split("#", 1)[0].replace(",", " ").split()
        if not parts:
            continue
        try:
            day = iso_date(parts[0])
            if len(parts) < 2:
                raise DataError("Expected YYYY-MM-DD and at least one symbol")
            symbols = [symbol(item) for item in parts[1:]]
        except DataError as exc:
            raise DataError(f"{path}:{line_number}: {exc}") from exc
        if (config.from_date and day < config.from_date) or (config.to_date and day > config.to_date):
            continue
        for ticker in symbols:
            if config.symbols and ticker not in config.symbols:
                continue
            candidates[day, ticker] = Candidate(day, ticker)
    if not candidates:
        raise DataError("No date/symbol pairs remain after the input filters")
    return sorted(candidates.values(), key=lambda item: (item.trading_date, item.symbol))
