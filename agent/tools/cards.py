"""结构化卡片工具（P1-18）—— 卡片的唯一解析/组装入口

────────────────────────────────────────────────────────────────
为什么要这个模块
────────────────────────────────────────────────────────────────
原实现用 ```xxx-card``` 代码块在**文本流**里传递结构化数据：

  产出端：coordinator._run_duyuan_image / _run_duyuan 里手工拼
          f"```vision-card\\n{json}\\n```"
  消费端：三处各写一遍正则去抠：
          - coordinator._llm_merge           re.findall(r"```[a-zA-Z\\-]*card\\n.*?\\n```", txt, re.S)
          - graph/runner._strip_cards        同一正则
          - agent_core._strip_cards          同一正则

问题：
  1) 重复实现 —— 三份正则，改一处漏两处；
  2) 脆弱 —— 依赖 JSON 内部不出现换行、依赖围栏格式完全一致；
  3) 语义丢失 —— 卡片与正文混在同一个字符串里，融合时得先拆再拼；
  4) 入库污染 —— 记忆沉淀必须额外记得剥卡片，忘一次就污染历史上下文。

本模块把这件事收敛成两个函数：
  extract_cards(text) -> (cards, plain_text)   从文本里分离卡片
  strip_cards(text)   -> plain_text            只要正文（入库/记忆用）
  build_card_block(kind, payload) -> str       产出端统一入口（兼容旧格式）

cards 元素形如 {"kind": "vision-card", "data": {...}}，可直接放进
AgentState.cards（LangGraph state 一等字段）。
"""
import json
import re
from typing import Any, Dict, List, Tuple

# 卡片围栏：```vision-card / ```compliance-card / ```anomaly-card / ```sensor-data
# 兼容 "card" 结尾与 sensor-data 这类自定义名
CARD_RE = re.compile(r"```([a-zA-Z][a-zA-Z0-9_\-]*card|[a-zA-Z][a-zA-Z0-9_\-]*-data)\n(.*?)\n```", re.S)

# 已知卡片类型（供前端/校验参考）
CARD_KINDS = ("vision-card", "compliance-card", "anomaly-card", "sensor-data")


def extract_cards(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """把文本中的卡片与正文分离。

    Args:
        text: 可能含 ```xxx-card``` 代码块的文本

    Returns:
        (cards, plain)
        cards: [{"kind": "vision-card", "data": {...} | None, "raw": "..."}]
               data 为解析成功的 JSON 对象；解析失败时为 None，原文保留在 raw
        plain: 去掉全部卡片后的正文（已 strip）
    """
    if not text:
        return [], ""
    cards: List[Dict[str, Any]] = []
    for m in CARD_RE.finditer(text):
        kind, body = m.group(1), m.group(2)
        data = None
        try:
            data = json.loads(body)
        except Exception:
            # 容错：截取首个 {...} / [...] 块再试一次
            mm = re.search(r"[\[{].*[\]}]", body, re.S)
            if mm:
                try:
                    data = json.loads(mm.group(0))
                except Exception:
                    data = None
        cards.append({"kind": kind, "data": data, "raw": body.strip()})
    plain = CARD_RE.sub("", text or "").strip()
    return cards, plain


def strip_cards(text: str) -> str:
    """只要正文（记忆沉淀 / 对话入库用），等价于原 _strip_cards 但只有一份实现"""
    return CARD_RE.sub("", text or "").strip()


def build_card_block(kind: str, payload: Any) -> str:
    """产出端统一入口：把结构化数据渲染成卡片代码块。

    保留 ```xxx-card``` 文本格式是为了**前端兼容**（前端按 marked 的
    code.language-xxx-card 选择器渲染）。同时调用方应把同样的数据放进
    AgentState.cards，这样服务端内部逻辑不必再解析文本。
    """
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload, ensure_ascii=False)
    else:
        body = str(payload)
    return "```%s\n%s\n```" % (kind, body)


def cards_to_state(text: str) -> Dict[str, Any]:
    """便捷函数：给节点用 —— 返回 {"cards": [...], "plain": "..."}"""
    cards, plain = extract_cards(text)
    return {"cards": cards, "plain": plain}


def attach_run_metadata(text: str, run_id: int, event_id: str = None) -> str:
    """Bind each card to its actual execution; never borrow the latest browser run."""
    def decorate(match):
        try:
            payload=json.loads(match.group(2))
            if not isinstance(payload,dict):
                return match.group(0)
            if event_id and payload.get("event_id") not in (None,event_id):
                payload.update(execution_status="invalid_input",verdict=None,in_spec=None,
                               error={"message":"结果事件与当前请求不一致"})
            elif event_id:
                payload["event_id"]=event_id
            payload["run_id"]=run_id
            return build_card_block(match.group(1),payload)
        except (TypeError,ValueError):
            return match.group(0)
    return CARD_RE.sub(decorate,text or "")


def merge_card_blocks(cards: List[Dict[str, Any]]) -> str:
    """把 state 里的 cards 还原成文本块（融合时前置到回答之前）"""
    out = []
    for c in (cards or []):
        kind = c.get("kind") or "card"
        if c.get("data") is not None:
            out.append(build_card_block(kind, c["data"]))
        elif c.get("raw"):
            out.append("```%s\n%s\n```" % (kind, c["raw"]))
    return "\n\n".join(out)
