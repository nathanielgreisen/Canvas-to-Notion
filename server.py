from config import ConfigError, load_settings
from bridge import run_server

if __name__ == "__main__":
    try:
        run_server(load_settings())
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")
