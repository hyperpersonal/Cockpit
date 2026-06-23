"""Claude API call. Sonnet daily / Opus biweekly. The system prompt hard-codes the red lines
so the model never emits buy/sell orders, flags uncertainty, and respects the config 宪法."""
from __future__ import annotations
import os, logging
from anthropic import Anthropic
log = logging.getLogger("cockpit.llm")

SYSTEM = (
 "你是用户的私人美股投研助手（动量主线⊕Serenity供应链卡点策略）。"
 "硬规则：① 只产出信息与操作【提示】，绝不下单/转账，最终由用户手动执行；"
 "② 非投资建议；任何未被数据交叉验证的关键数字必须标注「待验证」，不得编造；"
 "③ 按给定的 phase 守则措辞（盘中=未收盘快照，勿当收盘复盘）；"
 "④ 操作提示给：建议+理由+不确定性，并附满足/注意/不满足检查清单；"
 "⑤ 不追高（乖离率>5%且非强趋势则提示）。严格基于提供的数据作答，不要用训练知识臆测当前行情。"
)

def run(prompt: str, model: str, max_tokens: int = 3000) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return "[LLM 跳过：未配置 ANTHROPIC_API_KEY]"
    try:
        client = Anthropic(api_key=key)
        msg = client.messages.create(model=model, max_tokens=max_tokens, system=SYSTEM,
                                      messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    except Exception as e:                       # fail-open: deliver data even if LLM fails
        log.error("LLM failed: %s", e)
        return f"[LLM 调用失败：{e}。下方为原始数据，请人工判读。]"
