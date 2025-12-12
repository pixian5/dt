import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

import requests
from bs4 import BeautifulSoup

try:
    from .config import DEFAULT_RETRIES, DEFAULT_TIMEOUT, DEFAULT_USER_AGENT
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from crawler.config import DEFAULT_RETRIES, DEFAULT_TIMEOUT, DEFAULT_USER_AGENT


def load_urls_from_file(file_path: Path) -> List[str]:
    if not file_path.exists():
        raise FileNotFoundError(f"URL 文件不存在: {file_path}")
    with file_path.open("r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def sanitize_text(value: Optional[str]) -> str:
    return value.strip() if value else ""


def parse_html(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string if soup.title else ""
    h1 = next((h.get_text(strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)), "")
    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if meta_tag and meta_tag.get("content"):
        meta_desc = meta_tag.get("content")
    return {
        "url": url,
        "title": sanitize_text(title),
        "h1": sanitize_text(h1),
        "meta_description": sanitize_text(meta_desc),
    }


def fetch_url(session: requests.Session, url: str, timeout: float, retries: int) -> Optional[str]:
    for attempt in range(1, retries + 2):  # 首次 + 重试次数
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # pylint: disable=broad-except
            if attempt > retries:
                print(f"[失败] {url}，错误：{exc}")
                return None
            sleep_time = min(1.5 * attempt, 5)
            print(f"[重试] {url} 第 {attempt}/{retries} 次，等待 {sleep_time:.1f}s")
            time.sleep(sleep_time)
    return None


def crawl(urls: Iterable[str], timeout: float, retries: int, user_agent: str) -> List[dict]:
    headers = {"User-Agent": user_agent}
    results = []
    with requests.Session() as session:
        session.headers.update(headers)
        for url in urls:
            if not url:
                continue
            print(f"[抓取] {url}")
            html = fetch_url(session, url, timeout, retries)
            if html is None:
                results.append({"url": url, "title": "", "h1": "", "meta_description": "", "status": "failed"})
                continue
            parsed = parse_html(url, html)
            parsed["status"] = "ok"
            results.append(parsed)
    return results


def save_to_csv(rows: List[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["url", "status", "title", "h1", "meta_description"]
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[完成] 结果已保存至 {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="简单网页爬虫，提取标题/H1/meta 描述")
    parser.add_argument("--urls", nargs="*", help="直接传入的 URL 列表")
    parser.add_argument("--url-file", type=Path, help="包含 URL 的文件（每行一个）")
    parser.add_argument("--output", type=Path, default=Path("data/output.csv"), help="输出 CSV 路径")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="请求超时时间（秒）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="重试次数")
    parser.add_argument("--user-agent", type=str, default=DEFAULT_USER_AGENT, help="自定义 User-Agent")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    urls: List[str] = []
    if args.urls:
        urls.extend(args.urls)
    if args.url_file:
        urls.extend(load_urls_from_file(args.url_file))
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        raise SystemExit("请通过 --urls 或 --url-file 提供至少一个 URL")

    results = crawl(urls, timeout=args.timeout, retries=args.retries, user_agent=args.user_agent)
    save_to_csv(results, args.output)


if __name__ == "__main__":
    main()
