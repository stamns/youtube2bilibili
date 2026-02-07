from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


def _deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_update(merged[key], value)
        else:
            merged[key] = value
    return merged


DEFAULT_CONFIG: Dict[str, Any] = {
    "network": {
        "proxy": None,
    },
    "biliupr": {
        "repo": "biliup/biliup",
        "install_dir": "deps/biliupR",
        "metadata_file": "installed.json",
        "check_timeout_sec": 20,
    },
}


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return (base_dir / candidate).resolve()


def load_config(config_path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is not installed. Run install.py without --skip-pip once first."
        ) from exc

    cfg = DEFAULT_CONFIG
    if config_path.exists():
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Config file is not a mapping: {config_path}")
        cfg = _deep_update(cfg, payload)
    return cfg


def install_python_requirements(requirements_path: Path) -> None:
    if not requirements_path.exists():
        raise FileNotFoundError(f"Requirements file not found: {requirements_path}")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)]
    print(f"[install] Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install python deps + biliupR binary")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument(
        "--requirements",
        default="requirements.txt",
        help="Path to requirements file",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="Skip pip install step",
    )
    parser.add_argument(
        "--force-biliupr",
        action="store_true",
        help="Force re-download latest biliupR even if already latest",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    project_root = config_path.parent if config_path.exists() else Path.cwd()
    requirements_path = _resolve_path(project_root, args.requirements)

    if not args.skip_pip:
        install_python_requirements(requirements_path)

    cfg = load_config(config_path)
    network_proxy = (cfg.get("network") or {}).get("proxy")
    biliupr_cfg = cfg.get("biliupr") or {}
    proxy = biliupr_cfg.get("proxy") or network_proxy
    repo = biliupr_cfg.get("repo") or "biliup/biliup"
    timeout = int(biliupr_cfg.get("check_timeout_sec", 20))
    install_dir = _resolve_path(project_root, biliupr_cfg.get("install_dir", "deps/biliupR"))
    metadata_file = biliupr_cfg.get("metadata_file") or "installed.json"

    try:
        from biliupr_installer import BiliupInstallError, ensure_biliupr_installed
    except Exception as exc:  # noqa: BLE001
        print(f"[install] Failed to load biliup installer module: {exc}")
        return 1

    try:
        result = ensure_biliupr_installed(
            install_dir=install_dir,
            repo=repo,
            proxy=proxy,
            timeout=timeout,
            metadata_filename=metadata_file,
            force=args.force_biliupr,
        )
    except BiliupInstallError as exc:
        print(f"[install] Failed to install biliupR: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[install] Unexpected error while installing biliupR: {exc}")
        return 1

    action = "installed/updated" if result.get("installed") else "already latest"
    print(f"[install] biliupR {action}")
    print(f"[install] tag: {result.get('tag_name', 'unknown')}")
    print(f"[install] binary: {result.get('binary_path', '')}")
    print(f"[install] asset: {result.get('asset_name', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
