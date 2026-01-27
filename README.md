# 灯塔自动看-山东干部网络学院

## 功能简介
- 使用 Playwright 通过 CDP 连接本机 Chrome。
- 登录并保存登录态（`storage_state.json`，会先校验有效性）。
- 扫描“无随堂测验(否)”课程并输出 URL 列表（`url.txt`）。
- 按 `url.txt` 逐课播放并自动检查个人中心进度（播放完成/跳过会自动从 `url.txt` 删除）。

## 快速开始（Python 3.10+）
1. 创建虚拟环境并安装依赖：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m playwright install
   ```
2. 启动可被 Playwright 连接的 Chrome（若 53333 未启动，程序会自动启动一个带 CDP 的独立实例）：
   - 默认会从系统 Chrome 配置复制插件/配置到 `chrome-cdp-53333` 目录（首次复制后不再重复）。
   - 首次复制并启动后会自动打开 `chrome://extensions`，并在新标签打开登录页。
   - 如需手动启动（示例）：
     ```bash
     # macOS
     open -na "Google Chrome" --args --remote-debugging-port=53333 --user-data-dir="$HOME/chrome-cdp-53333"
     # Windows
     "%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=53333 --user-data-dir="%LOCALAPPDATA%\\chrome-cdp-53333"
     # Ubuntu
     google-chrome --remote-debugging-port=53333 --user-data-dir="$HOME/.config/chrome-cdp-53333"
     ```

## 使用方式
### 1) 登录（保存登录态）
```bash
python login.py
```
程序会自动填写账号密码；验证码需手动在浏览器输入并提交（或在终端输入后自动提交）。
账号密码请在环境变量或 `secrets.local.env` 中提供：
```bash
DT_CRAWLER_USERNAME=你的账号
DT_CRAWLER_PASSWORD=你的密码
```

### 2) 获取课程 URL（写入 url.txt）
```bash
python get_no_test_urls.py --page 1-3
```

### 3) 按 url.txt 观看课程
```bash
python watch.py
```
或指定文件/行号范围：
```bash
python watch.py --url-file url.txt --lines 32-40
```

## 目录结构
- `login.py`：登录并保存登录态
- `get_no_test_urls.py`：扫描课程详情链接（输出 `url.txt`）
- `watch.py`：按 URL 列表观看课程并检查进度（看完/跳过会删除 URL）
- `url.txt`：课程 URL 列表
- `storage_state.json`：登录态缓存
- `secrets.local.env`：本地账号配置（不提交）
- `data/`：输出目录

## 环境变量
- `DT_CRAWLER_USERNAME` / `DT_CRAWLER_PASSWORD`：登录账号密码
- `PLAYWRIGHT_CDP_ENDPOINT`：CDP 地址（默认 `http://127.0.0.1:53333`）
- `CHROME_CDP_USER_DATA_DIR`：自定义 Chrome 用户数据目录
- `CRAWLER_TIMEOUT` / `CRAWLER_RETRIES` / `CRAWLER_USER_AGENT`：请求参数（见 `config.py`）

## 运行逻辑简述
- 启动时若发现 `storage_state.json`，会先访问个人中心验证是否仍然有效；失效则删除并走正常登录流程。
- 若 53333 端口未启动，程序会启动独立的 CDP 实例，优先复用复制的插件/配置目录。
- 观看过程中若 60 秒播放时间无变化，会关闭当前标签并新标签重播（最多 3 次），仍无变化则跳过并删除该 URL。
- 接近播放结束且确认 Replay 状态后，判定课程完成并删除对应 URL。
- 登录会一直等待直到成功登录为止，观看课程会一直运行直到 `url.txt` 为空
