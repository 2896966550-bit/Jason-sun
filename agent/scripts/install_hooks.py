"""安装 Git pre-commit 钩子（D2）

用 `python scripts/install_hooks.py` 一次性安装；
之后每次 `git commit` 都会先跑 scripts/precommit.py 的快速门禁。

说明：`.git/hooks/` 不进版本库，所以钩子本身需要这个安装脚本分发，
而不是把钩子文件直接提交（那样对别人无效）。
"""
import os
import stat
import sys

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOK = os.path.join(_AGENT, ".git", "hooks", "pre-commit")

CONTENT = """#!/bin/sh
# Equis pre-commit 门禁（由 scripts/install_hooks.py 安装）
# 跳过：git commit --no-verify
PY="{python}"
AGENT="{agent}"
"$PY" "$AGENT/scripts/precommit.py"
"""


def main() -> int:
    if not os.path.isdir(os.path.join(_AGENT, ".git")):
        print("未找到 .git 目录，跳过安装（该目录不是 git 仓库）")
        return 0
    os.makedirs(os.path.dirname(_HOOK), exist_ok=True)
    with open(_HOOK, "w", encoding="utf-8", newline="\n") as f:
        f.write(CONTENT.format(python=sys.executable.replace("\\", "/"),
                               agent=_AGENT.replace("\\", "/")))
    # 赋予可执行位（Windows 上无效但无害）
    try:
        st = os.stat(_HOOK)
        os.chmod(_HOOK, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    print("已安装 pre-commit 钩子：", _HOOK)
    print("  解释器：", sys.executable)
    print("  跳过方式：git commit --no-verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
