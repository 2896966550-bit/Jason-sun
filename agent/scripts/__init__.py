"""Equis 脚本包

加这个 __init__.py 是为了让测试可用「模块名」方式运行，便于 CI 与交付复现：

    python -m unittest scripts.test_data_gate
    python -m unittest scripts.test_trace_migration

（此前 scripts/ 不是包，只能用文件路径运行，`-m unittest scripts.xxx` 会
报 ModuleNotFoundError: No module named 'scripts.xxx'）
"""
