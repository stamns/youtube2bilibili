from __future__ import annotations

from pathlib import Path

from upload import main


if __name__ == "__main__":
    preferred = Path("config.noproxy.yaml")
    default_config = "config.noproxy.yaml" if preferred.exists() else "config.yaml"
    raise SystemExit(main(default_config_path=default_config))
