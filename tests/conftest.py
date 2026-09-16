"""测试隔离：任何测试都不许受本机真实环境（AML_CONFIG / AML_HOME / 工作目录）影响。

踩过的坑：本机把 `AML_CONFIG` 指向了线上的 `config.local.yaml`，
于是 `maintenance` 的测试会去数**真实备份目录**、恢复**真实数据库**，结果随机失败。
所以这里统一清掉环境变量并把工作目录切到临时目录。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch, tmp_path):
    for name in ("AML_CONFIG", "AML_HOME", "AML_API", "DISTILL_API_KEY", "DISTILL_INCR_MIN",
                 "DISTILL_INCR_RATIO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)      # 也别让 ./config.yaml 被误读
    yield
