"""Select historical market data without constructing a trading client."""

from .alpaca import AlpacaClient
from .config import DataConfig, read_alpaca_credentials, read_api_key
from .massive import MassiveClient
from .models import DataError


def provider_label(config: DataConfig) -> str:
    return "Alpaca SIP" if config.provider == "alpaca" else "Massive"


def create_client(config: DataConfig, *, offline: bool = False,
                  refresh_cache: bool = False) -> MassiveClient | AlpacaClient:
    if config.provider == "alpaca":
        key, secret = (None, None) if offline else read_alpaca_credentials(config)
        if not offline and (not key or not secret):
            raise DataError(
                f"Set {config.alpaca_api_key_env} and {config.alpaca_secret_key_env} "
                f"in your environment or {config.env_file} for Alpaca SIP data"
            )
        return AlpacaClient(config, key, secret, offline=offline, refresh_cache=refresh_cache)
    if config.provider != "massive":
        raise DataError("data.provider must be massive or alpaca")
    key = None if offline else read_api_key(config)
    if not offline and not key:
        raise DataError(f"Set {config.api_key_env} in your environment or {config.env_file}")
    return MassiveClient(config, key, offline=offline, refresh_cache=refresh_cache)
