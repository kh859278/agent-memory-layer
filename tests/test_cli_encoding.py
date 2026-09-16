"""回归测试：CLI 在中文 Windows 的 GBK 控制台下不许崩。

真实故障（2026-09-16，装完第一条命令就复现）：
`aml doctor` 打印 ✅/⚠️ 与中文时，Python 默认按 GBK 编码 stdout →
`UnicodeEncodeError: 'gbk' codec can't encode character` → 满屏 Traceback。
修法：可执行入口调 `text.ensure_utf8_stdio()`。
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_doctor_survives_gbk_console(tmp_path):
    env = dict(os.environ)
    env.pop("AML_CONFIG", None)
    env["AML_HOME"] = str(tmp_path / "home")
    # 逼出 GBK stdout：PYTHONIOENCODING 优先于 PYTHONUTF8
    env["PYTHONIOENCODING"] = "gbk"
    env["PYTHONUTF8"] = "0"

    proc = subprocess.run([sys.executable, "-m", "aml", "doctor"],
                          capture_output=True, env=env, cwd=str(tmp_path), timeout=180)
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    stdout = (proc.stdout or b"").decode("utf-8", "replace")

    assert "Traceback" not in stderr, stderr
    assert "UnicodeEncodeError" not in stderr, stderr
    assert "记忆层体检" in stdout          # 中文正常输出，而不是乱码或空
    assert proc.returncode in (0, 1)       # 0 = 全好，1 = 有 ❌ 项（体检发现问题是正常结果）
