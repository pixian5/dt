import sys
from pathlib import Path
from typing import List

try:
    from .login import main as login_main
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from crawler.login import main as login_main


def main(argv: List[str] | None = None) -> None:
    original_argv = sys.argv
    try:
        sys.argv = argv if argv is not None else [original_argv[0]]
        login_main([])
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    main()
