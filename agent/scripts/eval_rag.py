# -*- coding: utf-8 -*-
r"""P4-39 RAG 检索质量自动回归评测（golden set + 基线对比）

────────────────────────────────────────────────────────────────
这个脚本要回答的问题
────────────────────────────────────────────────────────────────
"三路召回（编号正则 / 向量 / BM25+RRF）到底把该找的标准找回来了没有？"
在这之前，检索质量只有零散的线上个例（#408 说未收录、#426 又答对了），
没有任何可重复的量化口径，改一行正则没有人知道是变好还是变坏。

所以这里做三件事：
  1. 一份**固定的 golden set**（见 GOLDEN），覆盖四类查询：
     编号类 / 中文术语类 / 混合类 / 库外类；
  2. 只评**检索**（不调 LLM），指标 hit@1 / hit@3 / MRR + 编号类命中率；
  3. `--baseline` 存快照、`--compare` 输出回归差异（哪条 query 从命中变未命中）。

────────────────────────────────────────────────────────────────
为什么不走整张图
────────────────────────────────────────────────────────────────
检索质量与"LLM 怎么总结"是两件事。调用整张图既慢又贵，还会把检索问题
和生成问题混在一起无法定位。这里直接调 `tools.kb_tools.search_knowledge_base`
——它就是线上 retrieval 节点真正调用的那个函数。

────────────────────────────────────────────────────────────────
API 用量
────────────────────────────────────────────────────────────────
默认（不带 --with-llm）**不调用任何 LLM**，但每条 query 会走一次向量通道，
即一次 text-embedding-v4 调用（进程内有 LRU 缓存，重复 query 不重复计费）。
`--with-llm` 才会额外调 LLM，且用 --llm-sample 限流（默认 6 条）。

用法：
    <py> scripts/eval_rag.py                     # 只跑检索，出报告
    <py> scripts/eval_rag.py --baseline          # 同时存基线
    <py> scripts/eval_rag.py --compare           # 与基线比对，有回归则退出码 1
    <py> scripts/eval_rag.py --with-llm --llm-sample 6   # 额外评答案忠实度
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
MD_PATH = os.path.join(REPORT_DIR, "eval_rag.md")
JSON_PATH = os.path.join(REPORT_DIR, "eval_rag.json")
BASELINE_PATH = os.path.join(REPORT_DIR, "eval_rag_baseline.json")
REGRESSION_PATH = os.path.join(REPORT_DIR, "eval_rag_regression.json")
# 检索结果落盘缓存：让"改一行再跑一遍"不必重复付 embedding 的钱，
# 也让报告可复现（缓存里是原始检索输出，可人工核对打分是否正确）。
#
# 用 .tmp 后缀是刻意的：内容是 JSON，但**属于本机草稿、不进版本库**
# （约 470KB，且完整原始输出已经在 reports/eval_rag.json 里，
#  缓存只是为了避免重复的 embedding 计费）。想强制重跑用 --no-cache。
CACHE_PATH = os.path.join(REPORT_DIR, "eval_rag_cache.tmp")

TOP_K = 5          # 传给 search_knowledge_base；实际最多返回 top_k+2=7 条来源


# ══════════════════════════════════════════════════════════════
# Golden set
# ══════════════════════════════════════════════════════════════
# 字段说明：
#   id               稳定标识（回归对比靠它，不要改）
#   query            查询原文
#   category         id=编号类 / term=中文术语类 / mixed=混合类 / ood=库外
#   expected_sources 期望命中的 source 关键词（**任一命中即算命中**）。
#                    取关键词而非全名，是为了不让库里标题的小改动（如加"（详细）"）
#                    把评测变成"字符串全等测试"。
#   expect_no_answer ood 类：检索器应当表达"没有依据"
#   note             这条为什么存在（多为线上真实个例）
GOLDEN = [
    # ── 编号类（id）：编号通道本该一击必中 ────────────────────
    {"id": "id-001", "query": "JJG 21 是什么标准？", "category": "id",
     "expected_sources": ["JJG 21-2008"],
     "note": "线上故障：同一问题 #426 答对、#408（无问号）答未收录"},
    {"id": "id-002", "query": "JJG 21 是什么标准", "category": "id",
     "expected_sources": ["JJG 21-2008"], "note": "同上，去掉问号的稳定性对照"},
    {"id": "id-003", "query": "JJG-21-2008 千分尺检定规程", "category": "id",
     "expected_sources": ["JJG 21-2008"], "note": "连字符写法（P5-48 正则修正点）"},
    {"id": "id-004", "query": "JJG 30 通用卡尺检定规程", "category": "id",
     "expected_sources": ["JJG 30-2012"]},
    {"id": "id-005", "query": "JJG 34 指示表检定规程", "category": "id",
     "expected_sources": ["JJG 34-2022", "JJG 34-2008"]},
    {"id": "id-006", "query": "JJG 99 砝码检定规程", "category": "id",
     "expected_sources": ["JJG 99-2022", "JJG 99-2006"]},
    {"id": "id-007", "query": "JJG 124 电流表电压表功率表检定规程", "category": "id",
     "expected_sources": ["JJG 124-2005"]},
    {"id": "id-008", "query": "JJG 146 量块检定规程", "category": "id",
     "expected_sources": ["JJG 146-2011"]},
    {"id": "id-009", "query": "JJG 25-2004 螺纹千分尺", "category": "id",
     "expected_sources": ["JJG 25-2004"]},
    {"id": "id-010", "query": "JJG 31 高度卡尺检定规程", "category": "id",
     "expected_sources": ["JJG 31-2011"]},
    {"id": "id-011", "query": "JJG 52 弹性元件式一般压力表", "category": "id",
     "expected_sources": ["JJG 52-2013"]},
    {"id": "id-012", "query": "JJG 539 数字指示秤检定规程", "category": "id",
     "expected_sources": ["JJG 539-2016"]},
    {"id": "id-013", "query": "JJG 617 数字温度指示调节仪", "category": "id",
     "expected_sources": ["JJG 617-1996"]},
    {"id": "id-014", "query": "JJG 705 液相色谱仪检定规程", "category": "id",
     "expected_sources": ["JJG 705-2014"]},
    {"id": "id-015", "query": "JJG 882 压力表检定规程", "category": "id",
     "expected_sources": ["JJG 882-2019"]},
    {"id": "id-016", "query": "JJG 1036 电子天平检定规程", "category": "id",
     "expected_sources": ["JJG 1036-2008"]},
    {"id": "id-017", "query": "JJG 130 工作用玻璃液体温度计检定规程", "category": "id",
     "expected_sources": ["JJG 130-2011"]},
    {"id": "id-018", "query": "JJG 596 电子式电能表检定规程", "category": "id",
     "expected_sources": ["JJG 596-2012"]},
    {"id": "id-019", "query": "JJG 948 电动振动试验系统检定规程", "category": "id",
     "expected_sources": ["JJG 948-2018"]},
    {"id": "id-020", "query": "JJF 1001-2011 通用计量术语及定义", "category": "id",
     "expected_sources": ["JJF 1001-2011"]},
    {"id": "id-021", "query": "JJF 1033 计量标准考核规范", "category": "id",
     "expected_sources": ["JJF 1033-2023"]},
    {"id": "id-022", "query": "JJF 1059.1-2012 测量不确定度评定与表示", "category": "id",
     "expected_sources": ["JJF 1059.1-2012"]},
    {"id": "id-023", "query": "JJF 1059.2 MCM 蒙特卡洛法评定不确定度", "category": "id",
     "expected_sources": ["JJF 1059.2-2012"]},
    {"id": "id-024", "query": "JJF 1064 坐标测量机校准规范", "category": "id",
     "expected_sources": ["JJF 1064-2010"]},
    {"id": "id-025", "query": "JJF 1094 测量仪器特性评定", "category": "id",
     "expected_sources": ["JJF 1094-2002"]},
    {"id": "id-026", "query": "JJF 1101 温湿度标准箱校准", "category": "id",
     "expected_sources": ["JJF 1101-2019"]},
    {"id": "id-027", "query": "JJF 1139 计量器具检定周期确定原则", "category": "id",
     "expected_sources": ["JJF 1139-2005"]},
    {"id": "id-028", "query": "JJF 1587 数字多用表校准规范", "category": "id",
     "expected_sources": ["JJF 1587-2016"]},
    {"id": "id-029", "query": "JJF 1847 电子天平校准规范", "category": "id",
     "expected_sources": ["JJF 1847-2020"]},
    {"id": "id-030", "query": "GB/T 18779.1 合格判定规则", "category": "id",
     "expected_sources": ["GB/T 18779.1-2002"]},
    {"id": "id-031", "query": "GB/T 8170 数值修约规则", "category": "id",
     "expected_sources": ["GB/T 8170-2008"]},
    {"id": "id-032", "query": "GB/T 1958 几何公差检测与验证", "category": "id",
     "expected_sources": ["GB/T 1958-2017"]},
    {"id": "id-033", "query": "GB/T 3177 光滑工件尺寸检验", "category": "id",
     "expected_sources": ["GB/T 3177-2009"]},
    {"id": "id-034", "query": "GB/T 4249 公差原则", "category": "id",
     "expected_sources": ["GB/T 4249-2018"]},
    {"id": "id-035", "query": "GB/T 27418 测量不确定度评定和表示补充", "category": "id",
     "expected_sources": ["GB/T 27418-2017"]},
    {"id": "id-036", "query": "ISO/IEC 17025 检测和校准实验室能力通用要求", "category": "id",
     "expected_sources": ["ISO/IEC 17025-2017", "GB/T 27025-2019"]},
    {"id": "id-037", "query": "ISO 14253-2 合格评定中不确定度的应用", "category": "id",
     "expected_sources": ["ISO 14253-2"]},
    {"id": "id-038", "query": "VIM 3 国际计量学词汇", "category": "id",
     "expected_sources": ["VIM 3"]},
    {"id": "id-039", "query": "OIML R111 砝码国际建议", "category": "id",
     "expected_sources": ["OIML R111"]},
    {"id": "id-040", "query": "JJF 1318-2011 影像测量仪校准", "category": "id",
     "expected_sources": ["JJF1318-2011", "JJF 1318"]},

    # ── 中文术语类（term）：不出现编号，靠向量 + BM25 ──────────
    {"id": "term-001", "query": "怎么评定测量不确定度", "category": "term",
     "expected_sources": ["JJF 1059.1-2012"]},
    {"id": "term-002", "query": "A类评定和B类评定有什么区别", "category": "term",
     "expected_sources": ["JJF 1059.1-2012", "GUM"]},
    {"id": "term-003", "query": "什么是示值误差", "category": "term",
     "expected_sources": ["JJF 1001-2011", "VIM 3"]},
    {"id": "term-004", "query": "千分尺的检定项目有哪些", "category": "term",
     "expected_sources": ["JJG 21-2008"]},
    {"id": "term-005", "query": "怎么判断工件合格还是不合格", "category": "term",
     "expected_sources": ["GB/T 18779.1-2002"]},
    {"id": "term-006", "query": "数值修约的规则是什么", "category": "term",
     "expected_sources": ["GB/T 8170-2008"]},
    {"id": "term-007", "query": "期间核查怎么做", "category": "term",
     "expected_sources": ["期间核查"]},
    {"id": "term-008", "query": "计量标准的稳定性考核怎么做", "category": "term",
     "expected_sources": ["稳定性"]},
    {"id": "term-009", "query": "实验室间比对和能力验证是什么", "category": "term",
     "expected_sources": ["比对", "能力验证"]},
    {"id": "term-010", "query": "测量系统分析 MSA 怎么做", "category": "term",
     "expected_sources": ["MSA"]},
    {"id": "term-011", "query": "控制图怎么用于测量过程统计控制", "category": "term",
     "expected_sources": ["控制图"]},
    {"id": "term-012", "query": "不确定度预算表怎么制作", "category": "term",
     "expected_sources": ["不确定度预算表"]},
    {"id": "term-013", "query": "不确定度传播律和灵敏度系数", "category": "term",
     "expected_sources": ["传播律", "灵敏度系数"]},
    {"id": "term-014", "query": "正态分布和t分布在计量中怎么用", "category": "term",
     "expected_sources": ["正态分布"]},
    {"id": "term-015", "query": "温度对精密测量有什么影响", "category": "term",
     "expected_sources": ["温度效应"]},
    {"id": "term-016", "query": "双目视觉三维测量的原理", "category": "term",
     "expected_sources": ["双目视觉三维测量"]},
    {"id": "term-017", "query": "双目相机标定的步骤", "category": "term",
     "expected_sources": ["标定"]},
    {"id": "term-018", "query": "工业视觉缺陷检测怎么做", "category": "term",
     "expected_sources": ["缺陷检测"]},
    {"id": "term-019", "query": "多传感器数据融合有哪些策略", "category": "term",
     "expected_sources": ["数据融合"]},
    {"id": "term-020", "query": "计量器具ABC分类管理", "category": "term",
     "expected_sources": ["ABC分类"]},
    {"id": "term-021", "query": "计量标准命名与分类编码规则", "category": "term",
     "expected_sources": ["命名与分类编码"]},
    {"id": "term-022", "query": "国际计量体系是怎么组织的", "category": "term",
     "expected_sources": ["国际计量体系"]},
    {"id": "term-023", "query": "中国计量科学研究院是做什么的", "category": "term",
     "expected_sources": ["中国计量科学研究院"]},
    {"id": "term-024", "query": "检测和校准实验室认可准则", "category": "term",
     "expected_sources": ["CNAS-CL01"]},
    {"id": "term-025", "query": "测量管理体系的要求", "category": "term",
     "expected_sources": ["GB/T 19022"]},
    {"id": "term-026", "query": "电子天平怎么校准", "category": "term",
     "expected_sources": ["JJF 1847-2020", "JJG 1036-2008"]},
    {"id": "term-027", "query": "扭矩扳手怎么校准", "category": "term",
     "expected_sources": ["ISO 6789"]},
    {"id": "term-028", "query": "环境试验设备温湿度参数怎么校准", "category": "term",
     "expected_sources": ["JJF 1101-2019"]},
    {"id": "term-029", "query": "重点用能单位能源计量审查", "category": "term",
     "expected_sources": ["JJF 1356-2012"]},
    {"id": "term-030", "query": "标定和校准有什么区别（术语辨析）", "category": "term",
     "expected_sources": ["JJF 1001-2011", "VIM 3", "标定"]},
    # 这两条是**为了验证 term-003 的失败是不是偶发**而加的：术语定义类问题
    # 反复召回不到术语标准（JJF 1001 / VIM 3），则说明这是系统性缺陷而非个例。
    {"id": "term-031", "query": "测量误差的定义是什么", "category": "term",
     "expected_sources": ["JJF 1001-2011", "VIM 3"]},
    {"id": "term-032", "query": "计量学里『检定』和『校准』的定义区别", "category": "term",
     "expected_sources": ["JJF 1001-2011", "VIM 3"]},

    # ── 混合类（mixed）：编号 + 术语同时出现 ───────────────────
    {"id": "mix-001", "query": "JJG 21 千分尺检定规程的检定项目有哪些", "category": "mixed",
     "expected_sources": ["JJG 21-2008"]},
    {"id": "mix-002", "query": "JJF 1059.1 里 A 类不确定度怎么算", "category": "mixed",
     "expected_sources": ["JJF 1059.1-2012", "JJF1059.1"]},
    {"id": "mix-003", "query": "GB/T 18779.1 的合格判定规则怎么用", "category": "mixed",
     "expected_sources": ["GB/T 18779.1-2002"]},
    {"id": "mix-004", "query": "JJF 1033 计量标准考核要准备哪些材料", "category": "mixed",
     "expected_sources": ["JJF 1033-2023"]},
    {"id": "mix-005", "query": "JJG 1036 电子天平的检定项目", "category": "mixed",
     "expected_sources": ["JJG 1036-2008"]},
    {"id": "mix-006", "query": "ISO 14253-2 和 GB/T 18779.1 是什么关系", "category": "mixed",
     "expected_sources": ["ISO 14253-2", "GB/T 18779.1-2002"]},
    {"id": "mix-007", "query": "JJG 146 量块的检定方法", "category": "mixed",
     "expected_sources": ["JJG 146-2011"]},
    {"id": "mix-008", "query": "JJF 1064 坐标测量机校准项目", "category": "mixed",
     "expected_sources": ["JJF 1064-2010"]},
    {"id": "mix-009", "query": "JJG 882 压力表检定的示值误差要求", "category": "mixed",
     "expected_sources": ["JJG 882-2019"]},
    {"id": "mix-010", "query": "VDA 5 测量系统能力评定怎么做", "category": "mixed",
     "expected_sources": ["VDA 5"]},
    {"id": "mix-011", "query": "JJG 34 指示表的示值误差要求", "category": "mixed",
     "expected_sources": ["JJG 34-2022", "JJG 34-2008"]},
    {"id": "mix-012", "query": "JJF 1093 投影仪校准规范的内容", "category": "mixed",
     "expected_sources": ["JJF 1093-2015"]},

    # ── 库外类（ood）：库里没有依据，检索器应表达"无依据" ──────
    {"id": "ood-001", "query": "火星探测器怎么校准", "category": "ood",
     "expected_sources": [], "expect_no_answer": True,
     "note": "纯库外：计量库中不存在航天器校准内容"},
    {"id": "ood-002", "query": "怎么用Python爬取网页数据", "category": "ood",
     "expected_sources": [], "expect_no_answer": True, "note": "完全无关领域"},
    {"id": "ood-003", "query": "今天北京的天气怎么样", "category": "ood",
     "expected_sources": [], "expect_no_answer": True, "note": "实时信息，非知识库职责"},
    {"id": "ood-004", "query": "推荐几部科幻电影", "category": "ood",
     "expected_sources": [], "expect_no_answer": True, "note": "闲聊"},
    {"id": "ood-005", "query": "怎么做红烧肉", "category": "ood",
     "expected_sources": [], "expect_no_answer": True, "note": "闲聊"},
    {"id": "ood-006", "query": "公司2027年的营收预测是多少", "category": "ood",
     "expected_sources": [], "expect_no_answer": True, "note": "内部数据，库里没有"},
    {"id": "ood-007", "query": "怎么申请软件著作权", "category": "ood",
     "expected_sources": [], "expect_no_answer": True, "note": "流程类，非计量标准"},
    {"id": "ood-008", "query": "尼康NEXIV影像测量仪的报价是多少", "category": "ood",
     "expected_sources": [], "expect_no_answer": True,
     "note": "商业报价；注意库里**有**影像测量仪校准规范，这是易误召回项"},
]


# ══════════════════════════════════════════════════════════════
# 检索结果解析
# ══════════════════════════════════════════════════════════════
# search_knowledge_base 返回的是**给人看的文本**，这里必须把它还原成有序来源列表。
# 坑：来源名本身可能含 ASCII 括号（如 "ISO/IEC Guide 98-3:2008 (GUM) 测量不确定度表示指南"），
# 所以不能简单用 `来源: (.+?) \(` 去截——那样会在 (GUM) 处截断。
# 正确做法：只剥掉**位于行尾**的那个已知标记括号。
_TAG_TAIL_RE = re.compile(
    r"\s*\(\s*(?:编号精确匹配|编号出现在正文|编号模糊匹配|语义\+关键词融合命中"
    r"|相似度:\s*[\d.]+%|关键词命中率:\s*[\d.]+%)\s*\)\s*$"
)
_SRC_LINE_RE = re.compile(r"^【\d+】来源:\s*(.+)$")


def parse_sources(text: str) -> list:
    """从检索输出里按**名次顺序**抽出 source 列表"""
    out = []
    for line in (text or "").splitlines():
        m = _SRC_LINE_RE.match(line.strip())
        if not m:
            continue
        src = _TAG_TAIL_RE.sub("", m.group(1)).strip()
        if src and src not in out:
            out.append(src)
    return out


_NO_ANSWER_MARK = "知识库中未找到相关内容"


def is_no_answer(text: str) -> bool:
    """检索器是否明确表示"没有依据"（P5-48 之后的措辞）"""
    return _NO_ANSWER_MARK in (text or "")


# ══════════════════════════════════════════════════════════════
# 打分
# ══════════════════════════════════════════════════════════════

def _match_rank(sources, expected):
    """返回期望关键词首次出现的 1-based 名次；未命中返回 0"""
    for i, src in enumerate(sources, 1):
        for kw in expected:
            if kw.lower() in src.lower():
                return i
    return 0


def _lex_overlap(query, source):
    """查询与来源名的词元重合度（用与检索同一套 BM25 切分，避免自造口径）

    用来客观判断"这条召回是不是完全无依据"：若不共享任何词元，
    那么把它作为回答依据就是在**编造上下文**。
    """
    try:
        from tools.bm25 import tokenize
    except Exception:
        return None
    q = set(tokenize(query or ""))
    s = set(tokenize(source or ""))
    # 单字 bigram 之外的英文/数字整词更有判别力；但这里整体求交即可，
    # 因为中文 bigram 若重合说明确有共同用词。
    return len(q & s)


def judge(item, res):
    """单条 query 的打分结果"""
    if item.get("expect_no_answer"):
        # 库外类：只关心"检索器有没有承认没有依据"
        ov = _lex_overlap(item["query"], res["sources"][0]) if res["sources"] else None
        return {
            "id": item["id"], "category": item["category"], "query": item["query"],
            "expect_no_answer": True,
            "sources": res["sources"], "n_sources": len(res["sources"]),
            "no_answer": res["no_answer"],
            # 未拒答 且 首位来源与问题零词元重合 → 明确的"幻觉依据"
            "hallucinated_evidence": bool(res["sources"]) and ov == 0,
            "top1_lex_overlap": ov,
            "passed": bool(res["no_answer"]),
            "ms": res["ms"],
        }
    rank = _match_rank(res["sources"], item["expected_sources"])
    return {
        "id": item["id"], "category": item["category"], "query": item["query"],
        "expect_no_answer": False,
        "expected_sources": item["expected_sources"],
        "sources": res["sources"], "n_sources": len(res["sources"]),
        "rank": rank,
        "hit@1": rank == 1,
        "hit@3": 1 <= rank <= 3,
        "hit@all": rank >= 1,
        "rr": (1.0 / rank) if rank else 0.0,
        "no_answer": res["no_answer"],
        "passed": rank >= 1,
        "ms": res["ms"],
    }


def summarize(cases):
    """汇总指标（answerable 与 ood 分开算，混在一起看没有意义）"""
    ans = [c for c in cases if not c["expect_no_answer"]]
    ood = [c for c in cases if c["expect_no_answer"]]
    n = len(ans) or 1

    def _rate(pred, pool=None):
        pool = ans if pool is None else pool
        return round(sum(1 for c in pool if pred(c)) / (len(pool) or 1), 4)

    by_cat = {}
    for cat in ("id", "term", "mixed"):
        pool = [c for c in ans if c["category"] == cat]
        if not pool:
            continue
        by_cat[cat] = {
            "n": len(pool),
            "hit@1": _rate(lambda c: c["hit@1"], pool),
            "hit@3": _rate(lambda c: c["hit@3"], pool),
            "hit@all": _rate(lambda c: c["hit@all"], pool),
            "mrr": round(sum(c["rr"] for c in pool) / len(pool), 4),
        }

    id_pool = [c for c in ans if c["category"] == "id"]
    return {
        "n_answerable": len(ans),
        "n_ood": len(ood),
        "hit@1": _rate(lambda c: c["hit@1"]),
        "hit@3": _rate(lambda c: c["hit@3"]),
        "hit@all": _rate(lambda c: c["hit@all"]),
        "mrr": round(sum(c["rr"] for c in ans) / n, 4),
        # 「编号类查询命中率」——编号通道本该一击必中，所以口径取最严的 top1
        "id_query_hit_rate@1": _rate(lambda c: c["hit@1"], id_pool),
        "id_query_hit_rate@3": _rate(lambda c: c["hit@3"], id_pool),
        "by_category": by_cat,
        # 库外类：检索器"承认没有依据"的比例（注意：见报告中的实测缺陷）
        "ood_no_answer_rate": round(sum(1 for c in ood if c["no_answer"]) / (len(ood) or 1), 4),
        "ood_false_recall_rate": round(sum(1 for c in ood if c["sources"] and not c["no_answer"]) / (len(ood) or 1), 4),
        "ood_hallucinated_evidence_rate": round(
            sum(1 for c in ood if c.get("hallucinated_evidence")) / (len(ood) or 1), 4),
        "avg_ms": round(sum(c["ms"] for c in cases) / (len(cases) or 1), 1),
    }


# ══════════════════════════════════════════════════════════════
# 检索执行（带磁盘缓存）
# ══════════════════════════════════════════════════════════════

def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    os.makedirs(REPORT_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)


def retrieve_all(items, use_cache=True, top_k=TOP_K, quiet=False):
    """跑检索；返回 {id: {sources, no_answer, raw, ms}}"""
    from tools.kb_tools import search_knowledge_base

    cache = _load_cache() if use_cache else {}
    out, n_api, n_cached = {}, 0, 0
    for it in items:
        key = "%s||%d" % (it["query"], top_k)
        if use_cache and key in cache:
            out[it["id"]] = cache[key]
            n_cached += 1
            continue
        t0 = time.time()
        raw = search_knowledge_base(it["query"], top_k=top_k)
        ms = int((time.time() - t0) * 1000)
        rec = {"sources": parse_sources(raw), "no_answer": is_no_answer(raw),
               "raw": raw, "ms": ms}
        out[it["id"]] = rec
        cache[key] = rec
        n_api += 1
        if not quiet:
            print("  [%s] %-46s -> %d 条来源  %dms"
                  % (it["id"], it["query"][:46], len(rec["sources"]), ms))
    if use_cache:
        _save_cache(cache)
    return out, {"api_embeddings": n_api, "from_cache": n_cached}


# ══════════════════════════════════════════════════════════════
# 可选的答案层评测（--with-llm）
# ══════════════════════════════════════════════════════════════
# 设计取舍：**只发一次** LLM 调用/样本，但要求模型逐字摘录它答案所依据的
# 上下文原句；"这句话是否真的在上下文里"由本地代码做子串校验。
# 这样忠实度是**可验证**的，而不是"让模型给自己打分"。
_LLM_SYS = """你是 RAG 忠实度评审器。只依据给定的【检索上下文】回答【问题】。

输出 JSON（不要 markdown 代码块）：
{
  "found": true/false,          // 上下文是否足以回答问题
  "answer": "你的回答（found=false 时填空字符串）",
  "quote": "你答案所依据的上下文**原句**，必须逐字复制，不要改写、不要加省略号",
  "source": "该原句所属的标准名（来自上下文里的『来源:』行）"
}"""


def llm_grade(items, retr, sample=6):
    """对前 sample 条可回答 query 做"答案 + 引用可验证"评测"""
    from config import LLM_API_KEY, LLM_BASE_URL
    from openai import OpenAI

    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=60)
    cand = [it for it in items if not it.get("expect_no_answer")][:sample]
    rows, calls = [], 0
    for it in cand:
        rec = retr[it["id"]]
        ctx = rec["raw"][:4000]
        prompt = "【检索上下文】\n%s\n\n【问题】%s" % (ctx, it["query"])
        try:
            resp = client.chat.completions.create(
                model=os.getenv("EQUIS_EVAL_LLM_MODEL", "qwen-flash"),
                messages=[{"role": "system", "content": _LLM_SYS},
                          {"role": "user", "content": prompt}],
                temperature=0, max_tokens=1500,   # 实测 600 会把 quote 截断成非法 JSON
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
        except Exception as e:
            calls += 1
            obj = {"found": None, "answer": "", "quote": "", "source": "",
                   "_error": "request: %s: %s" % (type(e).__name__, str(e)[:120])}
        else:
            calls += 1
            raw = (resp.choices[0].message.content or "").strip()
            try:
                obj = json.loads(raw)
            except Exception as e:
                # 响应被截断/非法 JSON：记为**评测自身错误**，不当作忠实度失败
                obj = {"found": None, "answer": "", "quote": "", "source": "",
                       "_error": "parse: %s: %s | raw=%s"
                                % (type(e).__name__, str(e)[:80], raw[:120])}

        quote = str(obj.get("quote") or "").strip()
        # 本地校验：引文是否真的逐字出现在检索上下文里（含"未找到"则记为无法引用）
        quote_ok = bool(quote) and len(quote) >= 8 and quote[:60] in ctx
        # 引文是否落在**期望来源**那一段（更强的证据：不只是抄了上下文，
        # 而是抄了正确的那一段）
        src_ok = False
        if quote_ok:
            for kw in it.get("expected_sources", []):
                if kw.lower() in str(obj.get("source") or "").lower():
                    src_ok = True
                    break
        rows.append({
            "id": it["id"], "query": it["query"], "found": obj.get("found"),
            "answer": str(obj.get("answer") or "")[:400],
            "quote": quote[:200], "quote_verified": quote_ok,
            "quoted_expected_source": src_ok,
            "source": str(obj.get("source") or "")[:120],
            "error": obj.get("_error", ""),
        })
        print("  [llm %s] quote_verified=%s %s %s"
              % (it["id"], quote_ok, it["query"][:34],
                 ("ERR=" + obj["_error"][:60]) if obj.get("_error") else ""))
    # 忠实度只在**成功拿到结构化响应**的样本上计率；截断/网络错误单独报，
    # 否则一个评测自身的小毛病会被误读成"系统不忠实"。
    good = [r for r in rows if not r["error"]]
    n_all, n_good = len(rows) or 1, len(good) or 1
    return rows, {
        "samples": len(rows), "llm_calls": calls,
        "errors": len(rows) - len(good),
        "quote_verified_rate": round(sum(1 for r in good if r["quote_verified"]) / n_good, 4),
        "quoted_expected_source_rate": round(sum(1 for r in good if r["quoted_expected_source"]) / n_good, 4),
        "quote_verified_rate_all": round(sum(1 for r in rows if r["quote_verified"]) / n_all, 4),
    }


# ══════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════

def _pct(x):
    return "%.1f%%" % (100.0 * x)


def defect_analysis(payload):
    """把评测暴露出的**真实缺陷**写成可核对的段落。

    这一节存在的意义：评测的价值不在于「指标好看」，而在于把系统缺陷变成
    可复现、可核对、可回归的条目。所以这里不写「表现良好」，只写：
      - 现象（哪几条 query 出了什么问题）
      - 代码层面的根因（具体文件与行）
      - 修复方向（属于本次边界外的文件，故只给建议不落地）
    """
    cases = payload["cases"]
    ood = [c for c in cases if c["expect_no_answer"]]
    bad_ood = [c for c in ood if c["sources"] and not c["no_answer"]]
    hall = [c for c in ood if c.get("hallucinated_evidence")]
    term_fail = [c for c in cases if not c["expect_no_answer"]
                 and not c["passed"] and c["category"] == "term"]
    # 术语类名次分布（用来区分"召回不到"与"召回但排序靠后"）
    _t = [c for c in cases if not c["expect_no_answer"] and c["category"] == "term"]
    buckets = {"第1位": 0, "第2-3位": 0, "第4-7位": 0, "未命中(0)": 0}
    for c in _t:
        r = c.get("rank") or 0
        buckets["第1位" if r == 1 else
                "第2-3位" if 2 <= r <= 3 else
                "第4-7位" if r >= 4 else "未命中(0)"] += 1
    term_ranked = {"n": len(_t), "buckets": buckets}

    L = ["## 4. 缺陷分析（评测实测发现）\n"]
    if not bad_ood and not term_fail:
        L.append("本轮未发现缺陷。\n")
        return "\n".join(L)

    if bad_ood:
        L.append("### 4.1 检索层没有相关性下限，库外问题一律被『编造依据』\n")
        L.append("**现象**：%d/%d 条库外问题（天气、电影、菜谱、爬虫、报价…）"
                 "依然返回了计量标准来源，检索器**从不**说『未找到』。"
                 "其中 %d 条的首位来源与问题**没有任何共同词元**，"
                 "属于纯粹的编造依据。\n" % (len(bad_ood), len(ood), len(hall)))
        L.append("| id | 库外问题 | 返回条数 | 首位来源 | 词元重合 |")
        L.append("|---|---|---|---|---|")
        for c in bad_ood:
            L.append("| %s | %s | %d | %s | %s |"
                     % (c["id"], c["query"], c["n_sources"],
                        (c["sources"][0] if c["sources"] else "—")[:44],
                        "**0（编造）**" if c.get("hallucinated_evidence") else c.get("top1_lex_overlap")))
        L.append("")
        L.append("**根因（代码级）**：`tools/kb_tools.py` 的 `search_knowledge_base`：\n")
        L.append("1. 编号通道（约 L296）与 BM25 通道（约 L341）**没有最低分门槛**，"
                 "BM25 永远返回它的 top-N，与查询是否相关无关；")
        L.append("2. 融合后的追加循环（约 L372-385）以 `len(found_srcs) >= top_k + 2` 为唯一终止条件，"
                 "因此只要 BM25 有输出，`found_srcs` 就**不可能为空**；")
        L.append("3. 结论：L387 的 `if not found_srcs:`（『知识库中未找到相关内容』）"
                 "在 BM25 通道可用时是**死代码**，P5-48 精心写的『检索词回报』永远不会执行；")
        L.append("4. `min_sim=0.35` 这个相似度下限**只作用于向量通道**，"
                 "对编号通道与 BM25 通道无效，而 BM25 恰好是最后兜底的那一路。\n")
        L.append("**影响**：这个缺陷不只是『多给几条无关结果』，它直接把 RAG 的幻觉防线打穿了——"
                 "上层拿到的是格式规整、带『来源:』标注、看起来像权威依据的文本，"
                 "模型据此作答时没有任何信号提示『这些其实都不相关』。"
                 "评测中 ood-004『推荐几部科幻电影』拿到的依据是"
                 "《期间核查的实施方法》，ood-002『怎么用Python爬取网页数据』"
                 "拿到的是《LabVIEW虚拟仪器集成方案》。\n")
        L.append("**修复方向（本次未落地：`tools/kb_tools.py` 在文件边界之外）**：")
        L.append("- 给 BM25 通道加**绝对分数下限**（如 `score >= 2.0`）后再进 RRF；")
        L.append("- 或对『编号/术语/向量三通道全空且 BM25 最高分低于阈值』的情况直接走未找到分支；")
        L.append("- 并把 ood 类指标接入 CI：`ood_false_recall_rate` 应当趋近 0。\n")

    if term_fail or term_ranked:
        L.append("### 4.2 术语定义类查询：权威术语标准普遍排在第 2-5 位，且有一条完全脱靶\n")
        L.append("**先纠正一个容易得出的错误结论**：只看 term-003 会以为"
                 "『检索器召不回术语标准』，于是加了 term-031/term-032 两条对照——"
                 "结果它们都把《VIM 3》和《JJF 1001-2011》召回了（第 3、2 位）。"
                 "所以**不是召回缺失，而是排序问题**。\n")
        L.append("术语类查询的名次分布（n=%d）：\n" % term_ranked["n"])
        L.append("| 名次 | 条数 |")
        L.append("|---|---|")
        for k in ("第1位", "第2-3位", "第4-7位", "未命中(0)"):
            L.append("| %s | %d |" % (k, term_ranked["buckets"].get(k, 0)))
        L.append("")
        L.append("**规律**：问『X 的定义/区别』时，排第一的往往是**围绕 X 展开的规程**"
                 "（《JJF 1071 校准规范编写规则》《JJF 1094 测量仪器特性评定》"
                 "《JJF 1064 坐标测量机校准规范》），而不是**定义 X 的术语标准**"
                 "（《JJF 1001 通用计量术语及定义》《VIM 3》）。")
        L.append("原因在于术语标准的正文是**术语罗列**，同一个词只出现一两次，"
                 "词元密度被大量其他术语摊薄，BM25 分数天然拼不过整篇围绕该概念的文档；"
                 "向量通道也分不清『解释这个词的文档』和『使用这个词的文档』。\n")
        if term_fail:
            L.append("**完全脱靶的 1 条**：\n")
            L.append("| id | 问题 | 期望 | 实际前 3 名 |")
            L.append("|---|---|---|---|")
            for c in term_fail:
                L.append("| %s | %s | %s | %s |"
                         % (c["id"], c["query"], "；".join(c["expected_sources"]),
                            "、".join(c["sources"][:3])))
            L.append("")
            L.append("**对 term-003 的诚实注解**：它返回的《JJF 1094-2002 测量仪器特性评定》"
                     "**本身就在讲解示值误差**，作为回答依据并不算答非所问；"
                     "真正的问题是『权威出处』（术语标准）没有被排到前面。"
                     "换句话说，这条的严重程度低于 4.1 节的库外误召回——"
                     "**4.1 是会引发幻觉的，4.2 只是引用出处不够权威**。\n")
    return "\n".join(L)


def render_md(payload, regression=None):
    s = payload["summary"]
    L = []
    L.append("# RAG 检索质量评测报告（P4-39）\n")
    L.append("> 自动生成，请勿手改。复现：`python scripts/eval_rag.py`\n")
    L.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["ts"])))
    L.append("- 检索器：`tools.kb_tools.search_knowledge_base(query, top_k=%d)`（编号正则 + 向量 + BM25/RRF 三路）"
             % payload["top_k"])
    L.append("- golden set：**%d** 条（可回答 %d + 库外 %d）"
             % (s["n_answerable"] + s["n_ood"], s["n_answerable"], s["n_ood"]))
    L.append("- 本轮 embedding 调用：**%d** 次（缓存命中 %d 条）；LLM 调用：**%d** 次\n"
             % (payload["api"]["api_embeddings"], payload["api"]["from_cache"],
                payload["api"].get("llm_calls", 0)))

    L.append("## 1. 总指标\n")
    L.append("| 指标 | 值 | 口径 |")
    L.append("|---|---|---|")
    L.append("| hit@1 | **%s** | 期望来源排在第 1 位 |" % _pct(s["hit@1"]))
    L.append("| hit@3 | **%s** | 期望来源出现在前 3 位 |" % _pct(s["hit@3"]))
    L.append("| hit@all | %s | 出现在返回的任何名次 |" % _pct(s["hit@all"]))
    L.append("| MRR | **%.4f** | 平均倒数名次 |" % s["mrr"])
    L.append("| 编号类查询命中率 @1 | **%s** | 仅 id 类，编号通道应当一击必中 |" % _pct(s["id_query_hit_rate@1"]))
    L.append("| 编号类查询命中率 @3 | %s | 仅 id 类 |" % _pct(s["id_query_hit_rate@3"]))
    L.append("| 库外问题「未找到」率 | %s | ood 类，检索器承认无依据的比例 |" % _pct(s["ood_no_answer_rate"]))
    L.append("| 库外问题**误召回**率 | %s | ood 类，仍返回了来源的比例 |" % _pct(s["ood_false_recall_rate"]))
    L.append("| 其中「零词元重合」的编造依据率 | %s | 首位来源与问题无任何共同词元 |"
             % _pct(s["ood_hallucinated_evidence_rate"]))
    L.append("| 平均单次检索耗时 | %.1f ms | 不含进程首次加载 |" % s["avg_ms"])
    L.append("")

    L.append("## 2. 分类别\n")
    L.append("| 类别 | 条数 | hit@1 | hit@3 | hit@all | MRR |")
    L.append("|---|---|---|---|---|---|")
    _cn = {"id": "编号类", "term": "中文术语类", "mixed": "混合类"}
    for cat in ("id", "term", "mixed"):
        if cat not in s["by_category"]:
            continue
        c = s["by_category"][cat]
        L.append("| %s | %d | %s | %s | %s | %.4f |"
                 % (_cn[cat], c["n"], _pct(c["hit@1"]), _pct(c["hit@3"]), _pct(c["hit@all"]), c["mrr"]))
    L.append("")

    fails = [c for c in payload["cases"] if not c["passed"]]
    L.append("## 3. 失败案例（%d 条）\n" % len(fails))
    if not fails:
        L.append("无。\n")
    else:
        for c in fails:
            if c["expect_no_answer"]:
                L.append("- **%s** `%s`（库外）→ 未返回「未找到」，而是给了 %d 条来源：%s"
                         % (c["id"], c["query"], c["n_sources"],
                            "、".join(c["sources"][:3]) or "（空）"))
            else:
                L.append("- **%s** `%s` → 期望含 %s，实际返回 %d 条：%s"
                         % (c["id"], c["query"], c["expected_sources"], c["n_sources"],
                            "、".join(c["sources"][:3]) or "（空）"))
        L.append("")

    L.append(defect_analysis(payload))

    if payload.get("llm"):
        g = payload["llm"]["metrics"]
        L.append("## 5. 答案层抽样评测（--with-llm，%d 条 / %d 次调用）\n"
                 % (g["samples"], g["llm_calls"]))
        L.append("忠实度口径：要求模型**逐字摘录**其答案所依据的上下文原句，"
                 "再由本地代码做子串校验（不是让模型给自己打分）。\n")
        L.append("| 指标 | 值 |")
        L.append("|---|---|")
        L.append("| 引文可验证率 | **%s** |" % _pct(g["quote_verified_rate"]))
        L.append("| 引文落在期望来源上的比例 | %s |" % _pct(g["quoted_expected_source_rate"]))
        L.append("| 评测自身错误（截断/网络，不计入上式分母） | %d |" % g.get("errors", 0))
        L.append("")
        L.append("| id | 问题 | found | 引文校验 | 引用来源 |")
        L.append("|---|---|---|---|---|")
        for r in payload["llm"]["rows"]:
            L.append("| %s | %s | %s | %s | %s |"
                     % (r["id"], r["query"][:30], r["found"],
                        "✅" if r["quote_verified"] else "❌", r["source"][:40]))
        L.append("")

    if regression:
        L.append("## 6. 与基线的回归差异\n")
        L.append("- 基线：`%s`（%s）" % (regression["baseline_ts_h"], regression["baseline_ts"]))
        L.append("- 退化（命中→未命中）：**%d** 条；改善（未命中→命中）：%d 条；"
                 "名次下降：%d 条；名次提升：%d 条\n"
                 % (len(regression["regressed"]), len(regression["improved"]),
                    len(regression["rank_down"]), len(regression["rank_up"])))
        if regression["regressed"]:
            L.append("### ⚠️ 退化清单\n")
            L.append("| id | 问题 | 基线名次 | 本轮名次 | 本轮返回 |")
            L.append("|---|---|---|---|---|")
            for r in regression["regressed"]:
                L.append("| %s | %s | %s | %s | %s |"
                         % (r["id"], r["query"][:34], r["base_rank"] or "未命中",
                            r["now_rank"] or "未命中", "、".join(r["now_sources"][:2]) or "（空）"))
            L.append("")
        if regression["rank_down"]:
            L.append("### 名次下降（仍在前列，但排序变差）\n")
            L.append("| id | 问题 | 基线 | 本轮 |")
            L.append("|---|---|---|---|")
            for r in regression["rank_down"]:
                L.append("| %s | %s | %s | %s |" % (r["id"], r["query"][:34], r["base_rank"], r["now_rank"]))
            L.append("")
        if regression["metric_delta"]:
            L.append("### 指标变化\n")
            L.append("| 指标 | 基线 | 本轮 | Δ |")
            L.append("|---|---|---|---|")
            for k, v in regression["metric_delta"].items():
                L.append("| %s | %s | %s | %s |" % (k, v["base"], v["now"], v["delta"]))
            L.append("")

    L.append("## 7. 全部条目明细\n")
    L.append("| id | 类别 | 问题 | 期望 | 名次 | 返回来源（前 3） |")
    L.append("|---|---|---|---|---|---|")
    for c in payload["cases"]:
        if c["expect_no_answer"]:
            L.append("| %s | 库外 | %s | — | %s | %s |"
                     % (c["id"], c["query"][:34], "未找到" if c["no_answer"] else "误召回",
                        "、".join(c["sources"][:3])))
        else:
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (c["id"], c["category"], c["query"][:34],
                        "；".join(c["expected_sources"])[:30],
                        c["rank"] or "未命中", "、".join(c["sources"][:3])))
    L.append("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
# 基线对比
# ══════════════════════════════════════════════════════════════

_METRIC_KEYS = ("hit@1", "hit@3", "hit@all", "mrr",
                "id_query_hit_rate@1", "ood_no_answer_rate")


def compare(payload, baseline):
    base_by_id = {c["id"]: c for c in baseline["cases"]}
    regressed, improved, rank_down, rank_up = [], [], [], []
    for c in payload["cases"]:
        b = base_by_id.get(c["id"])
        if not b:
            continue
        if c["expect_no_answer"]:
            if b.get("passed") and not c["passed"]:
                regressed.append({"id": c["id"], "query": c["query"],
                                  "base_rank": "未找到", "now_rank": "误召回",
                                  "now_sources": c["sources"]})
            elif not b.get("passed") and c["passed"]:
                improved.append({"id": c["id"], "query": c["query"]})
            continue
        br, nr = b.get("rank") or 0, c.get("rank") or 0
        row = {"id": c["id"], "query": c["query"], "base_rank": br, "now_rank": nr,
               "now_sources": c["sources"]}
        if br >= 1 and nr == 0:
            regressed.append(row)
        elif br == 0 and nr >= 1:
            improved.append(row)
        elif br and nr and nr > br:
            rank_down.append(row)
        elif br and nr and nr < br:
            rank_up.append(row)
    delta = {}
    for k in _METRIC_KEYS:
        bv, nv = baseline["summary"].get(k), payload["summary"].get(k)
        if bv is None or nv is None:
            continue
        d = round(nv - bv, 4)
        if d:
            delta[k] = {"base": bv, "now": nv, "delta": ("+" if d > 0 else "") + str(d)}
    return {
        "baseline_ts": baseline.get("ts"),
        "baseline_ts_h": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(baseline.get("ts", 0))),
        "regressed": regressed, "improved": improved,
        "rank_down": rank_down, "rank_up": rank_up,
        "metric_delta": delta,
    }


# ══════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════

def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-39 RAG 检索质量评测")
    ap.add_argument("--baseline", action="store_true", help="把本轮结果存为基线")
    ap.add_argument("--compare", action="store_true", help="与基线比对，有退化则退出码 1")
    ap.add_argument("--with-llm", action="store_true", help="额外评答案忠实度（会消耗 LLM 额度）")
    ap.add_argument("--llm-sample", type=int, default=6, help="答案层抽样条数（默认 6）")
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument("--no-cache", action="store_true", help="忽略磁盘缓存，重新检索")
    ap.add_argument("--only", default="", help="只跑某类：id/term/mixed/ood")
    args = ap.parse_args(argv)

    items = GOLDEN
    if args.only:
        items = [g for g in GOLDEN if g["category"] == args.only]
        if not items:
            print("no items for category %s" % args.only)
            return 2

    print("=" * 78)
    print("P4-39 RAG 检索质量评测 | golden set %d 条 | top_k=%d" % (len(items), args.top_k))
    print("=" * 78)
    retr, apistat = retrieve_all(items, use_cache=not args.no_cache,
                                 top_k=args.top_k, quiet=False)
    cases = [judge(it, retr[it["id"]]) for it in items]
    summary = summarize(cases)

    payload = {"ts": time.time(), "top_k": args.top_k, "summary": summary,
               "cases": cases, "api": apistat}

    if args.with_llm:
        print("\n-- 答案层抽样（%d 条）--" % args.llm_sample)
        rows, metrics = llm_grade(items, retr, sample=args.llm_sample)
        payload["llm"] = {"rows": rows, "metrics": metrics}
        payload["api"]["llm_calls"] = metrics["llm_calls"]
    else:
        payload["api"]["llm_calls"] = 0

    regression = None
    if args.compare:
        if not os.path.exists(BASELINE_PATH):
            print("\n[compare] 找不到基线 %s，先跑一次 --baseline" % BASELINE_PATH)
        else:
            with open(BASELINE_PATH, encoding="utf-8") as f:
                baseline = json.load(f)
            regression = compare(payload, baseline)
            payload["regression"] = regression

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write(render_md(payload, regression))
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    if args.baseline:
        # 基线只存"打分所需的最小信息"，避免把原始文本一起冻结
        snap = {"ts": payload["ts"], "top_k": payload["top_k"], "summary": summary,
                "cases": [{k: v for k, v in c.items() if k != "raw"} for c in cases]}
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        print("\n[baseline] 已写入 %s" % BASELINE_PATH)

    if regression is not None:
        with open(REGRESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(regression, f, ensure_ascii=False, indent=1)
        print("[regression] 已写入 %s" % REGRESSION_PATH)

    # ── 控制台总表 ──
    print("\n" + "-" * 78)
    print("hit@1=%.1f%%  hit@3=%.1f%%  hit@all=%.1f%%  MRR=%.4f"
          % (100 * summary["hit@1"], 100 * summary["hit@3"],
             100 * summary["hit@all"], summary["mrr"]))
    print("编号类命中率: @1=%.1f%%  @3=%.1f%%   库外『未找到』率=%.1f%%   平均 %.1f ms"
          % (100 * summary["id_query_hit_rate@1"], 100 * summary["id_query_hit_rate@3"],
             100 * summary["ood_no_answer_rate"], summary["avg_ms"]))
    for cat, c in summary["by_category"].items():
        print("  %-6s n=%-3d hit@1=%.1f%%  hit@3=%.1f%%  MRR=%.4f"
              % (cat, c["n"], 100 * c["hit@1"], 100 * c["hit@3"], c["mrr"]))
    fails = [c for c in cases if not c["passed"]]
    print("失败 %d 条：%s" % (len(fails), ", ".join(c["id"] for c in fails) or "无"))
    print("报告：%s\n      %s" % (MD_PATH, JSON_PATH))
    if regression is not None:
        print("回归：退化 %d 条，改善 %d 条" % (len(regression["regressed"]), len(regression["improved"])))
        return 1 if regression["regressed"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
