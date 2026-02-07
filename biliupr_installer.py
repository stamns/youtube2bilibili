from __future__ import annotations

import datetime as dt
import json
import platform
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import requests

DEFAULT_REPO = "biliup/biliup"
DEFAULT_TIMEOUT = 20
DEFAULT_METADATA_FILE = "installed.json"
USER_AGENT = "youtube2bilibili-installer"


class BiliupInstallError(RuntimeError):
    pass


def _proxy_dict(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    if proxy and str(proxy).strip():
        return {"http": proxy, "https": proxy}
    return None


def _request_json(url: str, proxy: Optional[str], timeout: int) -> Dict[str, Any]:
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
        proxies=_proxy_dict(proxy),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise BiliupInstallError(f"Unexpected JSON payload type: {type(payload)}")
    return payload


def get_latest_release(
    repo: str = DEFAULT_REPO,
    proxy: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    return _request_json(url, proxy=proxy, timeout=timeout)


def binary_name_for_os(system: Optional[str] = None) -> str:
    normalized = (system or platform.system()).lower()
    return "biliup.exe" if normalized == "windows" else "biliup"


def get_binary_path(install_dir: Path, system: Optional[str] = None) -> Path:
    return install_dir / binary_name_for_os(system=system)


def _platform_suffix_priority(
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> list[str]:
    normalized_system = (system or platform.system()).lower()
    normalized_machine = (machine or platform.machine()).lower()

    if normalized_system == "windows":
        # biliup release only contains x86_64 windows package for now.
        return ["-x86_64-windows.zip"]

    if normalized_system == "darwin":
        if normalized_machine in ("arm64", "aarch64"):
            return ["-aarch64-macos.tar.xz"]
        return ["-x86_64-macos.tar.xz"]

    if normalized_system == "linux":
        if normalized_machine in ("x86_64", "amd64"):
            return ["-x86_64-linux.tar.xz", "-x86_64-linux-musl.tar.xz"]
        if normalized_machine in ("aarch64", "arm64"):
            return ["-aarch64-linux.tar.xz"]
        if normalized_machine.startswith("arm"):
            return ["-arm-linux.tar.xz"]

    raise BiliupInstallError(
        f"Unsupported platform for biliupR: system={normalized_system}, machine={normalized_machine}"
    )


def select_biliupr_asset(
    release: Dict[str, Any],
    system: Optional[str] = None,
    machine: Optional[str] = None,
) -> Dict[str, Any]:
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        raise BiliupInstallError("Release payload is missing a valid assets list")

    # Important: filter by biliupR prefix only and ignore bbup assets.
    biliupr_assets = [
        asset
        for asset in assets
        if isinstance(asset, dict) and str(asset.get("name", "")).startswith("biliupR-")
    ]
    if not biliupr_assets:
        raise BiliupInstallError("No biliupR assets found in latest release")

    for suffix in _platform_suffix_priority(system=system, machine=machine):
        for asset in biliupr_assets:
            name = str(asset.get("name", ""))
            if name.endswith(suffix):
                return asset

    available = ", ".join(str(asset.get("name", "")) for asset in biliupr_assets)
    raise BiliupInstallError(
        "No matching biliupR asset for current platform. "
        f"Available assets: {available}"
    )


def _extract_archive(archive_path: Path, extract_dir: Path) -> None:
    suffixes = archive_path.suffixes
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)
        return

    if suffixes[-2:] == [".tar", ".xz"] or archive_path.suffix == ".tar":
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(extract_dir)
        return

    raise BiliupInstallError(f"Unsupported archive format: {archive_path.name}")


def _find_extracted_binary(extract_dir: Path, target_name: str) -> Path:
    candidates = list(extract_dir.rglob(target_name))
    if candidates:
        return candidates[0]

    # Fallback in case release structure changes.
    if target_name == "biliup.exe":
        fallback = list(extract_dir.rglob("biliup"))
    else:
        fallback = list(extract_dir.rglob("biliup.exe"))
    if fallback:
        return fallback[0]

    raise BiliupInstallError(
        f"Unable to locate extracted biliup binary in archive under {extract_dir}"
    )


def read_install_metadata(
    install_dir: Path,
    metadata_filename: str = DEFAULT_METADATA_FILE,
) -> Dict[str, Any]:
    metadata_path = install_dir / metadata_filename
    if not metadata_path.exists():
        return {}

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _write_install_metadata(
    install_dir: Path,
    payload: Dict[str, Any],
    metadata_filename: str = DEFAULT_METADATA_FILE,
) -> None:
    metadata_path = install_dir / metadata_filename
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def install_release_asset(
    release: Dict[str, Any],
    asset: Dict[str, Any],
    install_dir: Path,
    proxy: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    metadata_filename: str = DEFAULT_METADATA_FILE,
) -> Dict[str, Any]:
    install_dir.mkdir(parents=True, exist_ok=True)

    asset_name = str(asset.get("name", ""))
    download_url = str(asset.get("browser_download_url", ""))
    if not asset_name or not download_url:
        raise BiliupInstallError("Invalid release asset payload")

    with tempfile.TemporaryDirectory(prefix="biliupr_", dir=str(install_dir)) as tmp_root:
        tmp_root_path = Path(tmp_root)
        archive_path = tmp_root_path / asset_name
        extract_dir = tmp_root_path / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        with requests.get(
            download_url,
            stream=True,
            headers={"User-Agent": USER_AGENT},
            proxies=_proxy_dict(proxy),
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            with archive_path.open("wb") as fp:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        fp.write(chunk)

        _extract_archive(archive_path, extract_dir)

        target_name = binary_name_for_os()
        extracted_binary = _find_extracted_binary(extract_dir, target_name=target_name)
        destination = install_dir / target_name
        shutil.copy2(extracted_binary, destination)

    if destination.suffix != ".exe":
        destination.chmod(0o755)

    metadata = {
        "repo": release.get("html_url", ""),
        "tag_name": release.get("tag_name", ""),
        "asset_name": asset_name,
        "download_url": download_url,
        "installed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "binary": str(destination),
    }
    _write_install_metadata(
        install_dir=install_dir,
        payload=metadata,
        metadata_filename=metadata_filename,
    )

    return {
        "binary_path": str(destination),
        "tag_name": metadata["tag_name"],
        "asset_name": asset_name,
        "download_url": download_url,
        "installed": True,
    }


def ensure_biliupr_installed(
    install_dir: Path,
    repo: str = DEFAULT_REPO,
    proxy: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    metadata_filename: str = DEFAULT_METADATA_FILE,
    force: bool = False,
    update_if_outdated: bool = True,
) -> Dict[str, Any]:
    binary_path = get_binary_path(install_dir=install_dir)
    metadata = read_install_metadata(install_dir=install_dir, metadata_filename=metadata_filename)
    current_tag = str(metadata.get("tag_name", ""))

    if binary_path.exists() and not force and not update_if_outdated:
        return {
            "binary_path": str(binary_path),
            "tag_name": current_tag,
            "asset_name": metadata.get("asset_name", ""),
            "download_url": metadata.get("download_url", ""),
            "installed": False,
        }

    release = get_latest_release(repo=repo, proxy=proxy, timeout=timeout)
    latest_tag = str(release.get("tag_name", ""))

    if binary_path.exists() and current_tag == latest_tag and not force:
        return {
            "binary_path": str(binary_path),
            "tag_name": current_tag,
            "asset_name": metadata.get("asset_name", ""),
            "download_url": metadata.get("download_url", ""),
            "installed": False,
        }

    asset = select_biliupr_asset(release=release)
    return install_release_asset(
        release=release,
        asset=asset,
        install_dir=install_dir,
        proxy=proxy,
        timeout=timeout,
        metadata_filename=metadata_filename,
    )


def check_for_update(
    install_dir: Path,
    repo: str = DEFAULT_REPO,
    proxy: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    metadata_filename: str = DEFAULT_METADATA_FILE,
) -> Dict[str, Any]:
    metadata = read_install_metadata(install_dir=install_dir, metadata_filename=metadata_filename)
    current_tag = str(metadata.get("tag_name", ""))
    release = get_latest_release(repo=repo, proxy=proxy, timeout=timeout)
    latest_tag = str(release.get("tag_name", ""))
    has_update = bool(latest_tag) and current_tag != latest_tag

    return {
        "current_tag": current_tag,
        "latest_tag": latest_tag,
        "has_update": has_update,
        "release": release,
    }
