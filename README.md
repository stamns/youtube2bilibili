# youtube2bilibili（依赖升级重置版）

本项目因上游依赖变化做了一次重置升级：

- 从旧 `biliup-rs` 迁移到 [biliup/biliup](https://github.com/biliup/biliup) Release 的 **`biliupR`**
- `yt-dlp` 全部改为 Python API（`ydl_opts`）
- 启动流程改为“先检查再执行”，减少中途失败

## 主要功能

- 一键搬运 YouTube 到 B 站（下载 + 投稿）
- 外部配置 `config.yaml`（不再写死在代码里）
- 支持代理、cookies 文件、cookies-from-browser
- 标题正则清洗、标签裁剪、投稿默认项覆盖
- 自动下载并更新 `biliupR`（严格匹配 `biliupR-*`，不会误下 `bbup`）
- 启动时自动检查并更新 `yt-dlp` / `deno` / `biliupR`（可配置）

## 目录说明

- `D:\youtube2bilibili\upload.py`：主脚本
- `D:\youtube2bilibili\noproxy_upload.py`：无代理入口（优先读 `config.noproxy.yaml`）
- `D:\youtube2bilibili\install.py`：安装 Python 依赖 + 初始化 `biliupR`
- `D:\youtube2bilibili\biliupr_installer.py`：`biliupR` 下载/更新模块
- `D:\youtube2bilibili\config.example.yaml`：配置模板

## 安装流程（PowerShell 7）

1. 复制配置模板

```powershell
Copy-Item .\config.example.yaml .\config.yaml
```

2. 安装依赖并下载 `biliupR`

```powershell
python .\install.py --config .\config.yaml
```

3. 启动脚本

```powershell
python .\upload.py --config .\config.yaml
```

## 启动顺序（已固定）

脚本每次启动都会按以下顺序执行：

1. 验证 YouTube 连通性  
   - 优先使用配置代理  
   - 失败时可在终端直接输入代理地址（例如 `http://127.0.0.1:7890`）重试  
   - 代理验证通过后会自动回写到 `config.yaml`  
   - 仍失败则停止
2. 验证 B 站登录状态  
   - 使用 `biliup renew` 校验  
   - 未登录时自动调用 `biliup login`（方向键选择登录方式，可扫码）  
   - 登录成功后再次 `renew` 验证
3. 检查并更新依赖  
   - `yt-dlp` / `deno`（PyPI）  
   - `biliupR`（GitHub Release）
4. 进入四种运行模式

## 四种模式

1. 单视频上传  
输入一个 YouTube URL，立即下载并投稿。

2. 列表/频道模式  
输入播放列表或频道 URL，自动解析链接并批量上传；状态写入 `url_list.json`。

3. 断点续传模式  
读取 `url_list.json` 中状态为 `no` 的条目继续上传。

4. 手动多链接模式  
逐条输入 URL，输入“完毕”结束，随后批量上传。

补充：批量模式结束后会输出本轮成功/失败统计和失败 URL 列表。

## 配置重点

请直接编辑 `D:\youtube2bilibili\config.yaml`。

- 代理  
  - `network.proxy`
  - `youtube.proxy` / `biliupr.proxy`（可覆盖全局）
- 启动控制  
  - `startup.ask_proxy_on_youtube_check_fail`
  - `startup.auto_update_python_deps`
- YouTube 鉴权  
  - `youtube.cookies.enabled + youtube.cookies.file`
  - `youtube.cookies_from_browser.enabled + browser/profile/...`
- yt-dlp 新版 JS 配置  
  - `youtube.js_runtime.js_runtimes: ["deno"]`
  - `youtube.js_runtime.remote_components: ["ejs:github"]`
- 投稿默认项（映射到 biliupR studio）  
  - `biliup_studio_defaults`
- 标题正则规则  
  - `upload.title_rules.regex_replace`

## 常用命令

- 仅更新 `biliupR`（跳过 pip）

```powershell
python .\install.py --config .\config.yaml --skip-pip
```

- 强制重装最新版 `biliupR`

```powershell
python .\install.py --config .\config.yaml --force-biliupr
```
