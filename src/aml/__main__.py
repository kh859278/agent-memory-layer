"""允许 `python -m aml ...`（等价于 `aml ...`）。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
