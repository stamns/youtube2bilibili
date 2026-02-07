from __future__ import annotations

import argparse
import copy
import importlib.metadata
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml
import yt_dlp
from PIL import Image

from biliupr_installer import (
    BiliupInstallError,
    check_for_update,
    ensure_biliupr_installed,
    install_release_asset,
    select_biliupr_asset,
)

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


DEFAULT_CONFIG: Dict[str, Any] = {
    "paths": {
        "videos_dir": "videos",
        "url_list_file": "url_list.json",
        "playlist_dump_file": "output.json",
    },
    "network": {
        "proxy": None,
        "timeout_sec": 20,
        "startup_check_url": "https://www.youtube.com",
    },
    "startup": {
        "ask_proxy_on_youtube_check_fail": True,
        "auto_update_python_deps": True,
    },
    "youtube": {
        "proxy": None,
        "playlist_extract_flat": True,
        "cookies": {
            "enabled": False,
            "file": "",
        },
        "cookies_from_browser": {
            "enabled": False,
            "browser": "firefox",
            "profile": None,
            "keyring": None,
            "container": None,
        },
        "js_runtime": {
            "enabled": True,
            "remote_components": ["ejs:github"],
        },
        "ydl_opts": {
            "live_from_start": True,
            "concurrent_fragment_downloads": 3,
            "fragment_retries": 3,
            "retries": 3,
        },
    },
    "upload": {
        "owner_name": "username",
        "remove_file": True,
        "default_tid": 21,
        "retry_count": 2,
        "include_uploader_in_tags": True,
        "include_owner_in_tags": True,
        "max_title_chars": 80,
        "max_desc_chars": 250,
        "max_tags": 10,
        "max_tag_chars": 20,
        "title_template": "{title}",
        "desc_template": "原视频日期{release_date}",
        "source_template": "{url}",
        "title_rules": {
            "trim": True,
            "regex_replace": [],
        },
    },
    "biliupr": {
        "repo": "biliup/biliup",
        "install_dir": "deps/biliupR",
        "metadata_file": "installed.json",
        "user_cookie": "cookies.json",
        "line": "qn",
        "limit": 3,
        "submit": "App",
        "check_timeout_sec": 20,
        "update_check_on_start": True,
        "auto_update": True,
        "login_check_timeout_sec": 30,
    },
    "biliup_studio_defaults": {
        "copyright": 2,
        "dynamic": "",
        "dolby": 0,
        "lossless_music": 0,
        "charging_pay": 0,
        "no_reprint": 0,
    },
}


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_update(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_path(base_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def proxy_dict(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    if proxy and str(proxy).strip():
        return {"http": proxy, "https": proxy}
    return None


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_submit(value: Any) -> str:
    token = str(value or "App").strip().lower().replace("-", "").replace("_", "")
    mapping = {
        "app": "App",
        "web": "Web",
        "bcutandroid": "BCutAndroid",
    }
    return mapping.get(token, "App")


def escape_glob_literal(path: str) -> str:
    escaped = path
    escaped = escaped.replace("[", "[[]")
    escaped = escaped.replace("]", "[]]")
    escaped = escaped.replace("*", "[*]")
    escaped = escaped.replace("?", "[?]")
    return escaped


def format_release_date(raw_date: Optional[str]) -> str:
    if not raw_date or len(raw_date) != 8 or not raw_date.isdigit():
        return "未知日期"
    return f"{raw_date[:4]}年{raw_date[4:6]}月{raw_date[6:8]}日"


def parse_version_tuple(version_text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version_text)
    if not numbers:
        return (0,)
    return tuple(int(item) for item in numbers)


class App:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.base_dir = self.config_path.parent
        self.config = self._load_config()
        self.paths_cfg = self.config.get("paths") or {}
        self.videos_dir = resolve_path(self.base_dir, self.paths_cfg.get("videos_dir", "videos"))
        self.url_list_file = resolve_path(
            self.base_dir, self.paths_cfg.get("url_list_file", "url_list.json")
        )
        self.playlist_dump_file = resolve_path(
            self.base_dir, self.paths_cfg.get("playlist_dump_file", "output.json")
        )
        self.biliup_binary: Optional[Path] = None

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            example_config = self.base_dir / "config.example.yaml"
            if example_config.exists():
                shutil.copy2(example_config, self.config_path)
                print(f"[startup] 未找到配置，已自动生成: {self.config_path}")
                print("[startup] 请按提示继续，或手动编辑配置文件后重新运行。")
            else:
                raise FileNotFoundError(
                    f"Config not found: {self.config_path}, "
                    "and config.example.yaml is also missing."
                )
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"Config file is not a mapping: {self.config_path}")
        return deep_update(DEFAULT_CONFIG, payload)

    def get_proxy(self, section: str) -> Optional[str]:
        section_cfg = self.config.get(section) or {}
        section_proxy = section_cfg.get("proxy")
        if section_proxy not in (None, ""):
            return str(section_proxy).strip()
        network_cfg = self.config.get("network") or {}
        network_proxy = network_cfg.get("proxy")
        if network_proxy in (None, ""):
            return None
        return str(network_proxy).strip()

    def persist_config(self) -> None:
        self.config_path.write_text(
            yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def set_runtime_proxy(self, proxy: str, *, persist: bool = False) -> None:
        self.config.setdefault("network", {})["proxy"] = proxy
        self.config.setdefault("youtube", {})["proxy"] = proxy
        if persist:
            self.persist_config()
            print(f"[startup] 代理已写回配置文件: {self.config_path}")

    def _probe_youtube_connectivity(self, proxy: Optional[str]) -> tuple[bool, str]:
        network_cfg = self.config.get("network") or {}
        target = network_cfg.get("startup_check_url", "https://www.youtube.com")
        timeout = safe_int(network_cfg.get("timeout_sec"), 20)
        mode = f"proxy ({proxy})" if proxy else "direct"
        print(f"[startup] 正在检查 YouTube 连通性: {mode}")
        try:
            response = requests.get(
                target,
                timeout=timeout,
                proxies=proxy_dict(proxy),
                headers={"User-Agent": "youtube2bilibili/1.0"},
            )
        except requests.RequestException as exc:
            return False, f"请求失败: {exc}"
        if response.status_code >= 500:
            return False, f"HTTP {response.status_code}"
        return True, f"HTTP {response.status_code}"

    def verify_youtube_connectivity(self) -> None:
        startup_cfg = self.config.get("startup") or {}
        ask_on_fail = bool(startup_cfg.get("ask_proxy_on_youtube_check_fail", True))
        current_proxy = self.get_proxy("youtube")

        ok, message = self._probe_youtube_connectivity(current_proxy)
        if ok:
            mode = f"代理 {current_proxy}" if current_proxy else "直连"
            print(f"[startup] YouTube 连通性验证通过: {mode} ({message})")
            return

        mode = f"代理 {current_proxy}" if current_proxy else "直连"
        print(f"[warn] YouTube 连通性验证失败: {mode} ({message})")
        if not ask_on_fail:
            raise RuntimeError("YouTube 不可访问，已停止。请修复网络或代理配置。")

        while True:
            user_proxy = input(
                "请输入可访问 YouTube 的代理地址（示例 http://127.0.0.1:7890），直接回车退出: "
            ).strip()
            if not user_proxy:
                raise RuntimeError("未提供可用代理，已停止。")
            ok, detail = self._probe_youtube_connectivity(user_proxy)
            if ok:
                self.set_runtime_proxy(user_proxy, persist=True)
                print(f"[startup] 代理验证通过，已使用运行时代理: {user_proxy} ({detail})")
                return
            print(f"[warn] 该代理不可用: {detail}")

    def ensure_biliupr_binary(self) -> None:
        cfg = self.config.get("biliupr") or {}
        install_dir = resolve_path(self.base_dir, cfg.get("install_dir", "deps/biliupR"))
        repo = cfg.get("repo", "biliup/biliup")
        metadata_file = cfg.get("metadata_file", "installed.json")
        timeout = safe_int(cfg.get("check_timeout_sec"), 20)
        proxy = self.get_proxy("network")

        try:
            ensure_result = ensure_biliupr_installed(
                install_dir=install_dir,
                repo=repo,
                proxy=proxy,
                timeout=timeout,
                metadata_filename=metadata_file,
                update_if_outdated=False,
            )
        except BiliupInstallError as exc:
            raise RuntimeError(f"Unable to install biliupR: {exc}") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Unable to install biliupR: network error: {exc}") from exc

        self.biliup_binary = Path(str(ensure_result["binary_path"]))
        self.biliup_binary = self.biliup_binary.resolve()
        self.biliupr_install_dir = install_dir
        self.biliupr_metadata_file = metadata_file
        if ensure_result.get("installed"):
            print(
                f"[startup] Installed biliupR {ensure_result.get('tag_name', '')}: "
                f"{self.biliup_binary}"
            )
        else:
            print(f"[startup] Using biliupR: {self.biliup_binary}")

    def build_biliup_base_cmd(self, include_user_cookie: bool = True) -> List[str]:
        if not self.biliup_binary:
            raise RuntimeError("biliupR binary path not ready")
        cfg = self.config.get("biliupr") or {}
        cmd = [str(self.biliup_binary)]
        if include_user_cookie:
            cmd += ["-u", str(resolve_path(self.base_dir, cfg.get("user_cookie", "cookies.json")))]
        return cmd

    def check_bilibili_login(self) -> bool:
        cfg = self.config.get("biliupr") or {}
        cookie_path = resolve_path(self.base_dir, cfg.get("user_cookie", "cookies.json"))
        if not cookie_path.exists():
            print(f"[startup] 未找到登录凭据: {cookie_path}")
            return False

        timeout = safe_int(cfg.get("login_check_timeout_sec"), 30)
        cmd = self.build_biliup_base_cmd(include_user_cookie=True)
        cmd += ["renew"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"[startup] 登录校验超时（{timeout}s），视为未登录。")
            return False
        if proc.returncode == 0:
            return True
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if output:
            print(f"[startup] 登录校验输出:\n{output}")
        return False

    def ensure_bilibili_login(self) -> None:
        if self.check_bilibili_login():
            print("[startup] Bilibili 登录状态有效。")
            return

        cfg = self.config.get("biliupr") or {}
        cookie_path = resolve_path(self.base_dir, cfg.get("user_cookie", "cookies.json"))
        cookie_path.parent.mkdir(parents=True, exist_ok=True)

        print("[startup] 检测到 Bilibili 未登录，正在调用 biliupR 登录流程...")
        cmd = self.build_biliup_base_cmd(include_user_cookie=True)
        cmd += ["login"]
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"biliupR login 执行失败，退出码: {proc.returncode}")

        if not self.check_bilibili_login():
            raise RuntimeError("Bilibili 登录校验失败，请重试登录。")
        print("[startup] Bilibili 登录校验通过。")

    def fetch_pypi_latest_version(self, package_name: str) -> str:
        timeout = safe_int((self.config.get("network") or {}).get("timeout_sec"), 20)
        proxy = self.get_proxy("youtube")
        url = f"https://pypi.org/pypi/{package_name}/json"
        response = requests.get(
            url,
            timeout=timeout,
            proxies=proxy_dict(proxy),
            headers={"User-Agent": "youtube2bilibili/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        info = payload.get("info") if isinstance(payload, dict) else {}
        latest = str((info or {}).get("version") or "").strip()
        if not latest:
            raise RuntimeError(f"无法获取 {package_name} 最新版本信息")
        return latest

    @staticmethod
    def get_installed_package_version(package_name: str) -> Optional[str]:
        try:
            return importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def pip_upgrade_package(self, package_name: str) -> None:
        proxy = self.get_proxy("youtube")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            package_name,
            "--disable-pip-version-check",
        ]
        if proxy:
            cmd += ["--proxy", proxy]
        print(f"[startup] 正在更新 Python 依赖: {package_name}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            raise RuntimeError(f"更新 {package_name} 失败:\n{output}")

    def check_and_update_python_dependencies(self) -> None:
        startup_cfg = self.config.get("startup") or {}
        auto_update = bool(startup_cfg.get("auto_update_python_deps", True))
        targets = ["yt-dlp", "deno"]

        for package_name in targets:
            installed = self.get_installed_package_version(package_name)
            latest = self.fetch_pypi_latest_version(package_name)
            if not installed:
                print(f"[startup] 缺少依赖 {package_name}，最新版本 {latest}")
                if not auto_update:
                    raise RuntimeError(f"依赖缺失: {package_name}")
                self.pip_upgrade_package(package_name)
                updated = self.get_installed_package_version(package_name)
                if not updated:
                    raise RuntimeError(f"安装 {package_name} 后仍无法检测到版本")
                print(f"[startup] {package_name} 已安装: {updated}")
                continue

            if parse_version_tuple(latest) > parse_version_tuple(installed):
                print(f"[startup] {package_name} 可更新: {installed} -> {latest}")
                if auto_update:
                    self.pip_upgrade_package(package_name)
                    updated = self.get_installed_package_version(package_name) or installed
                    print(f"[startup] {package_name} 更新完成: {updated}")
                else:
                    print(f"[startup] 已跳过自动更新 {package_name}（配置关闭）")
            else:
                print(f"[startup] {package_name} 已是最新或无需更新: {installed}")

    def check_and_update_biliupr(self) -> None:
        cfg = self.config.get("biliupr") or {}
        do_check_update = bool(cfg.get("update_check_on_start", True))
        auto_update = bool(cfg.get("auto_update", True))
        timeout = safe_int(cfg.get("check_timeout_sec"), 20)
        proxy = self.get_proxy("network")

        if not do_check_update:
            print("[startup] 已跳过 biliupR 更新检查（配置关闭）")
            return

        install_dir = getattr(self, "biliupr_install_dir", None)
        metadata_file = getattr(
            self, "biliupr_metadata_file", cfg.get("metadata_file", "installed.json")
        )
        repo = cfg.get("repo", "biliup/biliup")
        if not isinstance(install_dir, Path):
            install_dir = resolve_path(self.base_dir, cfg.get("install_dir", "deps/biliupR"))

        try:
            update_state = check_for_update(
                install_dir=install_dir,
                repo=repo,
                proxy=proxy,
                timeout=timeout,
                metadata_filename=metadata_file,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"biliupR 更新检查失败: {exc}") from exc

        if not update_state.get("has_update"):
            print(f"[startup] biliupR 已是最新版本: {update_state.get('latest_tag', '')}")
            return

        current_tag = update_state.get("current_tag", "unknown")
        latest_tag = update_state.get("latest_tag", "unknown")
        print(f"[startup] 检测到 biliupR 更新: {current_tag} -> {latest_tag}")
        if not auto_update:
            print("[startup] biliupR 自动更新已关闭，仅提示新版本。")
            return

        release = update_state.get("release")
        if not isinstance(release, dict):
            raise RuntimeError("biliupR 自动更新失败: release 数据无效")

        try:
            asset = select_biliupr_asset(release=release)
            update_result = install_release_asset(
                release=release,
                asset=asset,
                install_dir=install_dir,
                proxy=proxy,
                timeout=timeout,
                metadata_filename=metadata_file,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"biliupR 自动更新失败: {exc}") from exc

        self.biliup_binary = Path(str(update_result["binary_path"]))
        print(f"[startup] biliupR 已更新到: {update_result.get('tag_name', '')}")

    def build_ydl_opts(
        self,
        *,
        outtmpl: Optional[str] = None,
        for_playlist: bool = False,
        quiet: bool = True,
    ) -> Dict[str, Any]:
        youtube_cfg = self.config.get("youtube") or {}
        opts = copy.deepcopy(youtube_cfg.get("ydl_opts") or {})

        proxy = self.get_proxy("youtube")
        if proxy:
            opts["proxy"] = proxy

        cookie_cfg = youtube_cfg.get("cookies") or {}
        browser_cfg = youtube_cfg.get("cookies_from_browser") or {}
        use_cookie_file = bool(cookie_cfg.get("enabled")) and bool(cookie_cfg.get("file"))
        use_cookie_browser = bool(browser_cfg.get("enabled")) and bool(browser_cfg.get("browser"))

        if use_cookie_file and use_cookie_browser:
            print("[config] cookies and cookies_from_browser are both enabled. Using cookies file.")
            use_cookie_browser = False

        if use_cookie_file:
            cookie_file = resolve_path(self.base_dir, cookie_cfg.get("file", ""))
            opts["cookiefile"] = str(cookie_file)

        if use_cookie_browser:
            values: List[Any] = [browser_cfg.get("browser")]
            for key in ("profile", "keyring", "container"):
                value = browser_cfg.get(key)
                if value not in (None, ""):
                    values.append(value)
            opts["cookiesfrombrowser"] = tuple(values)

        js_cfg = youtube_cfg.get("js_runtime") or {}
        if js_cfg.get("enabled", True):
            remote_components = js_cfg.get("remote_components") or []
            if remote_components:
                opts["remote_components"] = list(remote_components)

            # js_runtimes 改为可选，未配置时不主动注入。
            js_runtimes = js_cfg.get("js_runtimes") or []
            if js_runtimes:
                opts["js_runtimes"] = list(js_runtimes)

        if outtmpl:
            opts["outtmpl"] = outtmpl

        if for_playlist:
            opts["extract_flat"] = bool(youtube_cfg.get("playlist_extract_flat", True))
            opts["skip_download"] = True

        if quiet:
            opts.setdefault("quiet", True)

        return opts

    def get_video_info(self, url: str) -> Dict[str, Any]:
        opts = self.build_ydl_opts(quiet=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise RuntimeError("Unexpected yt-dlp info payload")
        return info

    def download_video(self, url: str, folder: Path) -> None:
        outtmpl = str(folder / "%(id)s.%(ext)s")
        opts = self.build_ydl_opts(outtmpl=outtmpl, quiet=False)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    def extract_playlist_urls(self, url: str) -> List[str]:
        opts = self.build_ydl_opts(for_playlist=True, quiet=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        self.playlist_dump_file.parent.mkdir(parents=True, exist_ok=True)
        self.playlist_dump_file.write_text(
            json.dumps(info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        entries = []
        if isinstance(info, dict):
            entries = info.get("entries") or []

        urls: List[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            normalized = self.normalize_playlist_url(entry)
            if normalized and normalized.startswith("https://www.youtube.com"):
                urls.append(normalized)

        # Preserve order and de-duplicate.
        unique_urls = list(dict.fromkeys(urls))
        return unique_urls

    @staticmethod
    def normalize_playlist_url(entry: Dict[str, Any]) -> str:
        direct_url = str(entry.get("url") or "")
        if direct_url.startswith("https://"):
            return direct_url
        if direct_url.startswith("http://"):
            return "https://" + direct_url[len("http://") :]
        if direct_url.startswith("/"):
            return "https://www.youtube.com" + direct_url

        webpage = str(entry.get("webpage_url") or "")
        if webpage.startswith("https://"):
            return webpage

        vid = str(entry.get("id") or "")
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
        return ""

    def find_video_file(self, work_dir: Path, video_id: str) -> Optional[Path]:
        files = sorted(work_dir.glob("*"))
        for file in files:
            if not file.is_file():
                continue
            if file.suffix.lower() in {".webp", ".jpg", ".jpeg", ".png", ".json"}:
                continue
            if ".part" in file.name:
                continue
            if video_id in file.name:
                return file
        return None

    def download_cover(self, url: str, target_webp: Path) -> None:
        timeout = safe_int((self.config.get("network") or {}).get("timeout_sec"), 20)
        proxy = self.get_proxy("youtube")
        with requests.get(
            url,
            stream=True,
            timeout=timeout,
            proxies=proxy_dict(proxy),
            headers={"User-Agent": "youtube2bilibili/1.0"},
        ) as response:
            response.raise_for_status()
            with target_webp.open("wb") as fp:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        fp.write(chunk)

    @staticmethod
    def convert_webp_to_jpg(webp_path: Path, jpg_path: Path) -> None:
        image = Image.open(webp_path).convert("RGB")
        image.save(jpg_path, "jpeg")
        image.close()

    def apply_title_rules(self, raw_title: str) -> str:
        cfg = (self.config.get("upload") or {}).get("title_rules") or {}
        title = raw_title
        for rule in cfg.get("regex_replace") or []:
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("pattern")
            if not pattern:
                continue
            replace = str(rule.get("replace", ""))
            flags = 0
            for item in str(rule.get("flags", "")).upper():
                if item == "I":
                    flags |= re.IGNORECASE
                elif item == "M":
                    flags |= re.MULTILINE
                elif item == "S":
                    flags |= re.DOTALL
            title = re.sub(pattern, replace, title, flags=flags)
        if cfg.get("trim", True):
            title = title.strip()
        return title

    @staticmethod
    def limit_chars(text: str, max_len: int) -> str:
        if max_len <= 0:
            return text
        if len(text) <= max_len:
            return text
        return text[:max_len]

    def sanitize_tags(self, tags: List[str]) -> List[str]:
        upload_cfg = self.config.get("upload") or {}
        max_tags = safe_int(upload_cfg.get("max_tags"), 10)
        max_tag_chars = safe_int(upload_cfg.get("max_tag_chars"), 20)
        clean: List[str] = []
        seen: set[str] = set()
        for tag in tags:
            token = str(tag or "").strip()
            if not token:
                continue
            token = self.limit_chars(token, max_tag_chars)
            if token in seen:
                continue
            seen.add(token)
            clean.append(token)
            if len(clean) >= max_tags:
                break
        return clean

    def build_studio_payload(
        self,
        *,
        title: str,
        desc: str,
        tags: List[str],
        source_url: str,
        cover_path: Optional[Path],
        tid: int,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        defaults = copy.deepcopy(self.config.get("biliup_studio_defaults") or {})
        if "hires" in defaults and "lossless_music" not in defaults:
            defaults["lossless_music"] = defaults.pop("hires")

        upload_cfg = self.config.get("upload") or {}
        source_template = str(upload_cfg.get("source_template", "{url}"))
        source = source_template.format_map(SafeDict(context))

        payload = defaults
        payload["title"] = title
        payload["desc"] = desc
        payload["tag"] = ",".join(tags)
        payload["tid"] = int(tid)
        payload["source"] = source
        payload["cover"] = str(cover_path) if cover_path else ""
        return payload

    def run_biliupr_upload(self, video_file: Path, studio_payload: Dict[str, Any]) -> bool:
        if not self.biliup_binary:
            raise RuntimeError("biliupR binary path not ready")
        if not self.biliup_binary.exists():
            raise RuntimeError(f"biliupR binary not found: {self.biliup_binary}")

        cfg = self.config.get("biliupr") or {}
        user_cookie = resolve_path(self.base_dir, cfg.get("user_cookie", "cookies.json"))
        if not user_cookie.exists():
            raise RuntimeError(
                f"Cookie file not found: {user_cookie}. "
                f"Run '{self.biliup_binary} login -u {user_cookie}' first."
            )

        submit = normalize_submit(cfg.get("submit", "App"))
        line = str(cfg.get("line", "qn"))
        limit = safe_int(cfg.get("limit"), 3)
        streamer_key = escape_glob_literal(str(video_file))
        upload_config = {
            "line": line,
            "limit": limit,
            "submit": submit,
            "streamers": {
                streamer_key: studio_payload,
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".yaml",
            delete=False,
        ) as fp:
            yaml.safe_dump(upload_config, fp, allow_unicode=True, sort_keys=False)
            temp_config = Path(fp.name)

        cmd = [str(self.biliup_binary)]
        cmd += ["-u", str(user_cookie), "upload", "--config", str(temp_config)]

        print(f"[upload] Running: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        temp_config.unlink(missing_ok=True)
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if output:
            print(output)

        success_keywords = ("投稿成功", "标题相同", "already exists", "duplicate")
        return proc.returncode == 0 or any(key in output for key in success_keywords)

    def process_video(self, url: str, tid: int) -> bool:
        try:
            info = self.get_video_info(url)
            video_id = str(info.get("id") or "")
            if not video_id:
                raise RuntimeError("yt-dlp info missing video id")

            work_dir = self.videos_dir / video_id
            if work_dir.exists():
                shutil.rmtree(work_dir)
            work_dir.mkdir(parents=True, exist_ok=True)

            self.download_video(url, work_dir)
            video_file = self.find_video_file(work_dir, video_id)
            if not video_file:
                raise RuntimeError(f"Downloaded video file not found under: {work_dir}")

            cover_jpg: Optional[Path] = None
            cover_url = str(info.get("thumbnail") or "").strip()
            if cover_url:
                cover_webp = work_dir / "cover.webp"
                cover_jpg = work_dir / "cover.jpg"
                try:
                    self.download_cover(cover_url, cover_webp)
                    self.convert_webp_to_jpg(cover_webp, cover_jpg)
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] cover download/convert failed: {exc}")
                    cover_jpg = None

            upload_cfg = self.config.get("upload") or {}
            uploader = str(info.get("uploader") or "")
            release_date = format_release_date(str(info.get("upload_date") or ""))
            raw_title = str(info.get("title") or video_id)
            title = self.apply_title_rules(raw_title)
            title_template = str(upload_cfg.get("title_template", "{title}"))
            context = {
                "title": title,
                "raw_title": raw_title,
                "uploader": uploader,
                "owner_name": str(upload_cfg.get("owner_name", "")),
                "url": url,
                "release_date": release_date,
                "video_id": video_id,
            }

            title = title_template.format_map(SafeDict(context))
            title = self.limit_chars(title, safe_int(upload_cfg.get("max_title_chars"), 80))

            desc_template = str(upload_cfg.get("desc_template", "原视频日期{release_date}"))
            desc = desc_template.format_map(SafeDict(context))
            desc = self.limit_chars(desc, safe_int(upload_cfg.get("max_desc_chars"), 250))

            tags = list(info.get("tags") or [])
            if upload_cfg.get("include_uploader_in_tags", True) and uploader:
                tags.append(uploader)
            owner_name = str(upload_cfg.get("owner_name") or "").strip()
            if upload_cfg.get("include_owner_in_tags", True) and owner_name:
                tags.append(owner_name)
            tags = self.sanitize_tags(tags)

            studio_payload = self.build_studio_payload(
                title=title,
                desc=desc,
                tags=tags,
                source_url=url,
                cover_path=cover_jpg,
                tid=tid,
                context=context,
            )

            print(f"[video] title: {title}")
            success = self.run_biliupr_upload(video_file=video_file, studio_payload=studio_payload)
            if success and upload_cfg.get("remove_file", True):
                shutil.rmtree(work_dir, ignore_errors=True)
            return success
        except Exception as exc:  # noqa: BLE001
            print(f"[error] processing failed for {url}: {exc}")
            return False

    def load_url_list(self) -> List[Dict[str, Any]]:
        if not self.url_list_file.exists():
            return []
        payload = json.loads(self.url_list_file.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return payload
        return []

    def save_url_list(self, url_list: List[Dict[str, Any]]) -> None:
        self.url_list_file.parent.mkdir(parents=True, exist_ok=True)
        self.url_list_file.write_text(
            json.dumps(url_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ask_tid(self) -> int:
        default_tid = safe_int((self.config.get("upload") or {}).get("default_tid"), 21)
        tid = input(f"请输入分区代码 (默认{default_tid}): ").strip()
        return safe_int(tid, default_tid)

    def run_mode_single(self) -> None:
        url = input("请输入视频URL: ").strip()
        if not url:
            print("URL不能为空。")
            return
        tid = self.ask_tid()
        self.process_video(url, tid)

    def run_mode_playlist(self) -> None:
        url = input("请输入视频列表或频道URL: ").strip()
        if not url:
            print("URL不能为空。")
            return

        urls = self.extract_playlist_urls(url)
        if not urls:
            print("没有从播放列表中解析到视频URL。")
            return

        records = [{"url": item, "status": "no", "count": 0} for item in urls]
        self.save_url_list(records)

        print("以下是需要上传的视频URL列表:")
        for item in records:
            print(item["url"])

        confirm = input("请确认以上URL是否正确 (yes/no): ").strip().lower()
        if confirm != "yes":
            print("操作已取消。")
            return

        tid = self.ask_tid()
        self.upload_pending(records, tid)

    def run_mode_resume(self) -> None:
        tid = self.ask_tid()
        records = self.load_url_list()
        if not records:
            print("未找到 url_list.json，无法断点续传。")
            return
        self.upload_pending(records, tid)

    def run_mode_manual(self) -> None:
        urls: List[Dict[str, Any]] = []
        while True:
            url = input("请输入视频URL (输入'完毕'结束): ").strip()
            if url.lower() == "完毕":
                break
            if url:
                urls.append({"url": url, "status": "no", "count": 0})

        if not urls:
            print("没有输入任何URL。")
            return

        self.save_url_list(urls)
        print("以下是需要上传的视频URL列表:")
        for item in urls:
            print(item["url"])

        confirm = input("请确认以上URL是否正确 (yes/no): ").strip().lower()
        if confirm != "yes":
            print("操作已取消。")
            return

        tid = self.ask_tid()
        self.upload_pending(urls, tid)

    def upload_pending(self, records: List[Dict[str, Any]], tid: int) -> None:
        retry_count = safe_int((self.config.get("upload") or {}).get("retry_count"), 2)
        success_count = 0
        failed_urls: List[str] = []
        for entry in records:
            if entry.get("status") == "yes":
                continue
            video_url = str(entry.get("url") or "")
            if not video_url:
                continue

            success = False
            for _ in range(retry_count):
                success = self.process_video(video_url, tid)
                if success:
                    break
                entry["count"] = safe_int(entry.get("count"), 0) + 1

            entry["status"] = "yes" if success else "no"
            if success:
                success_count += 1
            else:
                failed_urls.append(video_url)
            self.save_url_list(records)

        print(f"[result] 本轮完成。成功: {success_count}，失败: {len(failed_urls)}")
        if failed_urls:
            print("[result] 失败URL列表：")
            for item in failed_urls:
                print(item)

    def run(self) -> int:
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.verify_youtube_connectivity()
        self.ensure_biliupr_binary()
        self.ensure_bilibili_login()
        self.check_and_update_python_dependencies()
        self.check_and_update_biliupr()

        print("请选择模式:")
        print("1: 单视频上传模式")
        print("2: 视频列表或频道模式")
        print("3: 断点续传模式")
        print("4: 手动输入多个单视频链接模式")
        mode = input("请输入模式编号: ").strip()

        if mode == "1":
            self.run_mode_single()
        elif mode == "2":
            self.run_mode_playlist()
        elif mode == "3":
            self.run_mode_resume()
        elif mode == "4":
            self.run_mode_manual()
        else:
            print("无效的模式编号。")
        return 0


def parse_args(argv: Optional[List[str]], default_config_path: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube to Bilibili uploader")
    parser.add_argument(
        "--config",
        default=default_config_path,
        help="Path to config YAML file",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None, default_config_path: str = "config.yaml") -> int:
    args = parse_args(argv, default_config_path=default_config_path)
    app = App(config_path=Path(args.config))
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
