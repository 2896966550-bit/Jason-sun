"""工作区清理（P4-43）—— 默认 dry-run，加 --apply 才真删

────────────────────────────────────────────────────────────────
设计原则：白名单 + 拒绝清单，绝不误删
────────────────────────────────────────────────────────────────
清理脚本本身有风险，因此：
  1) **默认 dry-run**：只打印将删除的内容与释放空间，不做任何改动；
  2) **白名单**：只有明确列出的目录/文件模式才会被考虑删除；
  3) **拒绝清单**（硬性）：数据源、密钥、知识库主库文件永不删除 ——
     即使被误写进白名单也会被拒；
  4) 删除前逐项确认其仍在项目根之下，防止路径穿越。

用法：
    python scripts/cleanup_workspace.py            # 预览
    python scripts/cleanup_workspace.py --apply    # 执行
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_AGENT)

# ── 拒绝清单：任何情况下都不删 ──────────────────────────
NEVER_DELETE = (
    "agent/data/chat_history.db",
    "agent/data/trace.db",
    "agent/data/audit.db",
    "agent/data/graph_checkpoints.db",
    "agent/data/chroma_db/chroma.sqlite3",
    "agent/.env",
    "agent/omni_ad/weights/student_best.pth",
    "agent/omni_ad/weights/memory_bank.pkl",
)

# ── 白名单：允许清理的路径（相对项目根）──────────────────
# 用 None 表示"整目录删除"；用 pattern 表示"目录下按模式匹配"
WHITELIST_DIRS = (
    "agent_backup_0912",          # 改造前的全量旧副本（含同一份 .env）
    "%PROGRAMDATA%",              # 未展开的环境变量垃圾目录
    "agent/NVIDIA Corporation",   # 空垃圾目录
    "output",                     # 临时输出（非 outputs/）
    "_reportfetch",               # 一次性抓取残留
    "_reportfetch2",
)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _is_protected(path: str) -> bool:
    """拒绝清单检查：把绝对路径转成相对项目根的规范形式再比对

    除枚举清单外，另加**通配规则**（由 P4-43 核验测试发现的纵深防御缺口）：
    凡是 `agent/data/**` 下的数据库文件（.db / .sqlite3，含 -wal / -shm）
    一律保护，避免将来新增数据库（如 metrics.db）时因忘记登记而被清理。

    注意：**不要**把整个 `agent/data/chroma_db/` 目录前缀列为受保护 ——
    那会挡住"清理 chroma 孤儿 segment 目录"这个功能本身
    （孤儿 segment 正是该目录下的子目录）。只需保护其中的主库文件，
    而它已被下面的通配规则与枚举清单覆盖。
    """
    rel = os.path.relpath(os.path.abspath(path), _ROOT).replace("\\", "/")

    # 通配规则 1：agent/data/ 下的任何数据库文件
    if re.match(r"^agent/data/.*\.(db|sqlite3)(-wal|-shm)?$", rel):
        return True
    # 通配规则 2：上传件与抓帧目录（用户数据，永不清理）
    if rel.startswith("agent/data/uploads") or rel.startswith("agent/data/captures"):
        return True

    # 显式枚举清单
    for p in NEVER_DELETE:
        if rel == p or rel.startswith(p.rstrip("/") + "/"):
            return True
    return False


def collect_targets() -> list:
    """收集待清理项 [(kind, path, size, note)]"""
    out = []

    # 1. 白名单目录
    for d in WHITELIST_DIRS:
        p = os.path.join(_ROOT, d)
        if os.path.isdir(p) and not _is_protected(p):
            out.append(("dir", p, _dir_size(p), "白名单目录"))

    # 2. __pycache__
    for root, dirs, _files in os.walk(_ROOT):
        if any(x in root for x in (os.sep + ".git", os.sep + "node_modules")):
            continue
        for d in list(dirs):
            if d == "__pycache__":
                p = os.path.join(root, d)
                if not _is_protected(p):
                    out.append(("dir", p, _dir_size(p), "__pycache__"))
                dirs.remove(d)

    # 3. ChromaDB 孤儿 segment 目录（不在 segments 表里的）
    cb = os.path.join(_AGENT, "data", "chroma_db")
    if os.path.isdir(cb):
        live = set()
        try:
            c = sqlite3.connect("file:%s?mode=ro" % os.path.join(cb, "chroma.sqlite3").replace("\\", "/"),
                                uri=True)
            live = {r[0] for r in c.execute("SELECT id FROM segments")}
            c.close()
        except Exception as e:
            print("[清理] 警告：无法读取 segments 表，跳过孤儿目录检测（%s）" % e)
        if live:
            for name in os.listdir(cb):
                p = os.path.join(cb, name)
                if os.path.isdir(p) and name not in live:
                    if not _is_protected(p):
                        out.append(("dir", p, _dir_size(p),
                                    "chroma 孤儿 segment（不在 segments 表）"))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Equis 工作区清理（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    print("=" * 78)
    print("Equis 工作区清理  —— %s" % ("执行模式（将真删）" if args.apply else "预览模式（dry-run，不改动）"))
    print("=" * 78)
    print("项目根: %s" % _ROOT)
    print("拒绝清单（永不删除）:")
    for p in NEVER_DELETE:
        print("   - %s" % p)
    print()

    targets = collect_targets()
    if not targets:
        print("没有需要清理的内容。")
        return 0

    total = 0
    for kind, path, size, note in sorted(targets, key=lambda x: -x[2]):
        rel = os.path.relpath(path, _ROOT)
        print("  [%s] %-58s %10s   %s" % (kind, rel[:58], _human(size), note))
        total += size

    print()
    print("共 %d 项，预计释放 %s" % (len(targets), _human(total)))

    if not args.apply:
        print()
        print("（预览模式，未做任何改动。确认无误后加 --apply 执行）")
        return 0

    if not args.yes:
        try:
            ans = input("\n确认删除以上 %d 项？输入 yes 继续: " % len(targets))
        except EOFError:
            ans = ""
        if ans.strip().lower() != "yes":
            print("已取消。")
            return 1

    ok = skip = fail = 0
    for kind, path, _size, _note in targets:
        # 父目录已被删除时，其下嵌套项会直接消失 —— 这不是错误，正常跳过。
        # （实测：删掉 agent_backup_0912/ 后，它内部的 __pycache__ 自然不存在了）
        if not os.path.exists(path):
            skip += 1
            continue
        # 最后一道防线：再检查一次拒绝清单与路径归属
        if _is_protected(path):
            print("  [跳过] 命中拒绝清单: %s" % path)
            skip += 1
            continue
        real = os.path.abspath(path)
        if not real.startswith(os.path.abspath(_ROOT) + os.sep):
            print("  [跳过] 路径不在项目根之下: %s" % real)
            skip += 1
            continue
        try:
            shutil.rmtree(real)
            ok += 1
        except Exception as e:
            print("  [失败] %s -> %s: %s" % (os.path.relpath(real, _ROOT), type(e).__name__, e))
            fail += 1

    print()
    print("完成：删除 %d 项，跳过 %d 项（父目录已删/命中拒绝清单），失败 %d 项，释放约 %s"
          % (ok, skip, fail, _human(total)))
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
