"""一键评测入口（P4 统一入口）

把项目的四类验证串成一条命令，便于交付与 CI：

  1. 单元测试     omni_ad/uncertainty/test_compliance.py          (68 条)
  2. trace 迁移   scripts/test_trace_migration.py                 (8 条)
  3. 出境 Gate    scripts/test_data_gate.py                       (20 条)
  4. 检索评测     scripts/eval_rag.py                             （只跑检索，不耗额度）
  5. 路由评测     scripts/eval_agent.py --keyword-only            （不耗额度）

用法：
    python scripts/run_evals.py               # 全部（不含联网/LLM）
    python scripts/run_evals.py --with-llm    # 额外跑 LLM 路径的路由评测（耗额度）

退出码：全部通过 0；任一失败非 0。
"""
import argparse
import io
import os
import subprocess
import sys
import time
import unittest

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_AGENT)
for p in (_AGENT, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

PY = sys.executable
results = []


def _run_unittest(label: str, mods) -> bool:
    print("\n" + "=" * 78)
    print("▶ %s" % label)
    print("=" * 78)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    ok = True
    for m in mods:
        try:
            suite.addTests(loader.loadTestsFromName(m))
        except Exception as e:
            print("  [加载失败] %s -> %s: %s" % (m, type(e).__name__, e))
            ok = False
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    res = runner.run(suite)
    passed = res.wasSuccessful()
    print("  -> %s：%d 条，失败 %d，错误 %d"
          % ("通过" if passed else "不通过", res.testsRun, len(res.failures), len(res.errors)))
    results.append((label, passed, "%d 条" % res.testsRun))
    return passed and ok


def _run_script(label: str, args) -> bool:
    print("\n" + "=" * 78)
    print("▶ %s" % label)
    print("=" * 78)
    t0 = time.time()
    try:
        r = subprocess.run([PY] + args, cwd=_AGENT, timeout=1800,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace")
        for line in out.strip().split("\n")[-25:]:
            print("  " + line)
        passed = (r.returncode == 0)
    except Exception as e:
        print("  [异常] %s: %s" % (type(e).__name__, e))
        passed = False
    print("  -> %s（%.1fs）" % ("通过" if passed else "不通过", time.time() - t0))
    results.append((label, passed, ""))
    return passed


def main() -> int:
    ap = argparse.ArgumentParser(description="Equis 一键评测")
    ap.add_argument("--with-llm", action="store_true",
                    help="额外跑 LLM 路径的路由评测（消耗 API 额度）")
    args = ap.parse_args()

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    print("Equis 一键评测")
    print("解释器: %s" % PY)
    print("项目根: %s" % _ROOT)

    _run_unittest("1/10 OOD×GUM 单元测试", ["omni_ad.uncertainty.test_compliance"])
    _run_unittest("2/10 trace 表迁移测试", ["scripts.test_trace_migration"])
    _run_unittest("3/10 数据出境 Gate 测试", ["scripts.test_data_gate"])
    _run_unittest("4/10 清理脚本安全测试", ["scripts.test_cleanup_workspace"])
    _run_unittest("5/10 关键路径测试（路由/拒算/依据）", ["scripts.test_core_paths"])
    _run_unittest("6/10 人脸脱敏合规控件测试", ["scripts.test_redaction"])
    _run_unittest("7/10 图编排端到端测试（mock LLM）", ["scripts.test_graph_e2e"])

    _run_script("8/10 RAG 检索评测（不耗额度）",
                [os.path.join("scripts", "eval_rag.py")])

    _run_script("9/10 前端静态检查（标签/资源/域名/emoji）",
                [os.path.join("scripts", "check_frontend.py")])

    kw_ok = _run_script("10/10 Agent 路由评测（关键词降级路径）",
                        [os.path.join("scripts", "eval_agent.py"), "--keyword-only"])
    if args.with_llm:
        _run_script("10b Agent 路由评测（真实 LLM 路径）",
                    [os.path.join("scripts", "eval_agent.py")])

    print("\n" + "=" * 78)
    print("总结")
    print("=" * 78)
    n_ok = sum(1 for _l, p, _d in results if p)
    for label, passed, detail in results:
        print("  [%s] %-42s %s" % ("OK " if passed else "FAIL", label, detail))
    print("\n  %d/%d 项通过" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
