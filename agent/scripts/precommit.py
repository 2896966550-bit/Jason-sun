"""Local maintenance gate: 19 checks, including isolated Python and frontend tests.

Python suites run through scripts.run_local_test with temporary data, no .env,
no real credentials, and blocked network connections. Frontend checks require
Node.js (or EQUIS_NODE). Run: python -B scripts/precommit.py.
Network-backed RAG/agent evaluations remain separate in scripts/run_evals.py.
"""
import os
import subprocess
import sys
import time
import re

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)

PY = sys.executable

#: 纯本地、零额度的检查（模块名 + 说明）
FAST_CHECKS = [
    ("scripts.check_frontend", "前端静态检查"),
    ("omni_ad.uncertainty.test_compliance", "OOD×GUM 单元测试"),
    ("scripts.test_trace_migration", "trace 迁移"),
    ("scripts.test_data_gate", "出境 Gate"),
    ("scripts.test_cleanup_workspace", "清理脚本安全"),
    ("scripts.test_core_paths", "关键路径"),
    ("scripts.test_redaction", "人脸脱敏控件"),
    ("scripts.test_graph_e2e", "图编排端到端"),
    ("scripts.test_input_contract", "测量输入契约"),
    ("scripts.test_measurement_context", "测量事件与类别绑定"),
    ("scripts.test_sensor_protocol", "传感器协议与时效"),
    ("scripts.test_sensor_http", "传感器监控接口"),
    ("scripts.test_stereo_workflow", "双目采集与标定契约"),
    ("scripts.test_measurement_execution", "确定性测量执行"),
    ("scripts.test_json_output", "公共 JSON 解析"),
    ("scripts.test_http_contracts", "HTTP 参数与资源缓存"),
    ("scripts.test_chat_history", "历史对话配对"),
    ("scripts.test_graph_runtime", "图执行生命周期"),
    ("scripts.check_javascript", "前端语法与交互回归"),
]


def main() -> int:
    # 负向验证时踩到的坑：失败详情里可能含无法用 GBK 编码的字符
    # （subprocess 输出用 errors="replace" 解码会产生 U+FFFD），
    # print 时直接抛 UnicodeEncodeError，导致"门禁没报错、脚本自己崩了"。
    # 这里把 stdout 固定成 UTF-8 + replace，保证失败信息一定打得出来。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 70)
    print("提交前门禁（纯本地 · 零额度）")
    print("=" * 70)
    t_all = time.time()
    failed = []
    unit_total = 0
    for mod, label in FAST_CHECKS:
        t0 = time.time()
        # check_frontend 是脚本而非 unittest 模块，用文件路径跑
        if mod in ("scripts.check_frontend", "scripts.check_javascript"):
            cmd = [PY, "-B", "-m", mod]
        else:
            cmd = [PY, "-B", "-m", "scripts.run_local_test", mod]
        r = subprocess.run(cmd, cwd=_AGENT, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": os.path.dirname(_AGENT) + os.pathsep + _AGENT, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
        dt = time.time() - t0
        ok = (r.returncode == 0)
        detail = r.stdout.decode("utf-8", "replace")
        count = re.search(r"Ran (\d+) tests?", detail)
        if count:
            unit_total += int(count.group(1))
        print("  [%s] %-24s %.1fs" % ("OK " if ok else "FAIL", label, dt))
        if not ok:
            failed.append((label, detail))

    total = time.time() - t_all
    print("-" * 70)
    if failed:
        print("门禁未通过：%d 项失败（用时 %.1fs）" % (len(failed), total))
        for label, out in failed:
            print("\n### %s ###" % label)
            print("\n".join(out.strip().split("\n")[-18:]))
        print("\n修复后重试；确需跳过可用 git commit --no-verify")
        return 1

    print("门禁通过：%d 项，用时 %.1fs" % (len(FAST_CHECKS), total))
    print("Python 回归：%d 个用例；另含前端静态、JavaScript 语法与交互检查。" % unit_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
