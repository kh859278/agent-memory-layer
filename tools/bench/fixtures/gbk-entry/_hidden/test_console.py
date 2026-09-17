"""验收标准（判分前才拷进工作区，agent 看不到）。

检查的是"在 GBK 控制台下能不能跑完"这类**仓库里没写、但环境里真实存在**的约定。

注意：本文件是**平铺**拷进工作区根目录的（`_hidden/x.py` → 工作区根下的 `x.py`），
所以定位被测文件要用"本文件同目录"，不能写 `..`（第一版写成 `..` 直接判所有运行都失败，
把"验收标准自己找错文件"记成了 agent 的失败 —— 基准最怕这种假失败）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app.py")


def _run(extra_env):
    env = dict(os.environ)
    env.update(extra_env)
    return subprocess.run([sys.executable, APP], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=60)


def test_runs_under_gbk_console():
    proc = _run({"PYTHONIOENCODING": "gbk"})
    assert proc.returncode == 0, "GBK 控制台下崩溃：" + (proc.stderr or "")[-400:]


def test_still_prints_summary():
    proc = _run({})
    assert proc.returncode == 0
    assert "3" in (proc.stdout or ""), "输出里没有条目数：" + repr(proc.stdout)
