# Python 爬虫项目

## 功能简介
- 输入 URL 列表，逐个请求页面并解析标题、H1 及 meta description。
- 将结果保存为 CSV，默认输出到 `data/output.csv`。
- 简单的重试与超时控制。

## 快速开始
### 本地运行（需 Python 3.10+）
1. 安装依赖：
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. 运行示例：
   ```bash
   python -m crawler.main --urls https://example.com https://httpbin.org/html
   ```
   或使用文件：
   ```bash
   python -m crawler.main --url-file urls.txt
   ```

### Docker 运行
```bash
docker build -t python-crawler .
docker run --rm -v "$(pwd)/data:/app/data" python-crawler \
  python -m crawler.main --urls https://example.com
```

## 目录结构
- `crawler/`：核心代码
- `data/`：输出目录（CSV）
- `urls.txt`：示例 URL 列表

## 环境变量
- `CRAWLER_TIMEOUT`：请求超时（秒），默认 10
- `CRAWLER_RETRIES`：重试次数，默认 2
- `CRAWLER_USER_AGENT`：自定义 UA，默认内置 UA

## 注意
- 请遵守目标站点的 robots.txt 与使用条款，不要过高频率请求。
