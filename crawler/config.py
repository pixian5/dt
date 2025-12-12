import os

DEFAULT_TIMEOUT = float(os.getenv("CRAWLER_TIMEOUT", "10"))
DEFAULT_RETRIES = int(os.getenv("CRAWLER_RETRIES", "2"))
DEFAULT_USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
)
