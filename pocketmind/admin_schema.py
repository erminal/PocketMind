"""管理台的表单元数据：分组、字段说明、供应商预设。

这些文字会直接显示在 UI 的「?」提示里；改这里即可改说明，不用改模板。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 供应商预设（OpenAI 兼容；模型列表可在线自动拉取，失败时用这里的候选）
# ---------------------------------------------------------------------------

PROVIDER_PRESETS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "models": ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"],
        "key_hint": "DeepSeek 控制台创建的 API Key",
        "thinking": ["disabled", "enabled"],
        "embedding": False,
    },
    "qwen": {
        "label": "通义千问 / DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
        "key_hint": "阿里云百炼 / DashScope API Key",
        "thinking": ["disabled", "enabled"],
        "embedding": False,
    },
    "siliconflow": {
        "label": "SiliconFlow 硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3.2",
        "models": ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen3-235B-A22B-Instruct"],
        "key_hint": "SiliconFlow API Key",
        "thinking": ["disabled", "enabled"],
        "embedding": False,
    },
    "custom": {
        "label": "自定义 / 其他 OpenAI 兼容服务",
        "base_url": "",
        "model": "",
        "models": [],
        "key_hint": "服务商 API Key",
        "thinking": ["disabled", "enabled"],
        "embedding": False,
    },
}

EMBEDDING_PRESETS = {
    "qwen": {
        "label": "通义千问 Embedding（DashScope）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "text-embedding-v4",
        "models": ["text-embedding-v4", "text-embedding-v3"],
        "key_hint": "DashScope API Key（可与主对话不同）",
    },
    "siliconflow": {
        "label": "SiliconFlow Embedding",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "models": ["Qwen/Qwen3-Embedding-0.6B", "BAAI/bge-m3"],
        "key_hint": "SiliconFlow API Key",
    },
    "custom": {
        "label": "自定义 / 其他 OpenAI 兼容 Embedding",
        "base_url": "",
        "model": "",
        "models": [],
        "key_hint": "服务商 API Key",
    },
}

THINKING_OPTIONS = [
    {"value": "disabled", "label": "关闭（快速档，默认）", "desc": "不启用 reasoning；延迟低、语气稳，适合日常聊天。"},
    {"value": "enabled", "label": "开启（深思考档）", "desc": "让模型先思考再回答；适合复杂推理，延迟和成本更高。"},
]

# ---------------------------------------------------------------------------
# 记忆 / 检索参数：按大标题分组；每项配「?」说明
# ---------------------------------------------------------------------------

SCORING_GROUPS = [
    {
        "title": "检索与重排",
        "desc": "决定「哪些记忆会被找出来、按什么分数排序、注入多少条」。",
        "fields": [
            {"name": "w_sem", "label": "语义相似权重", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "向量语义相似度在最终分里的权重。调高更依赖「意思相近」，调低更依赖关键词/情绪。"},
            {"name": "w_aff", "label": "情绪同频权重", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "记忆情绪与当前情绪的匹配度权重。调高会更容易想起「同情绪」的旧事。"},
            {"name": "w_imp", "label": "重要度权重", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "记忆重要度权重。调高会让里程碑/高情绪事件更容易被优先想起。"},
            {"name": "w_rec", "label": "新近度权重", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "越近的记忆分数越高。调高会让她更常提最近的事。"},
            {"name": "w_rel", "label": "关系相关权重", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "与当前功能类型（self/social/directive）相关的权重；影响「关系类」记忆的排序。"},
            {"name": "recall_limit", "label": "显式线索最多注入条数", "type": "int", "min": 1, "max": 12, "step": 1,
             "desc": "用户明确问「记得/上次/答应」时最多注入几条记忆。太大容易像背书。"},
            {"name": "candidate_limit", "label": "检索日志候选数", "type": "int", "min": 5, "max": 100, "step": 1,
             "desc": "写入 retrieval_logs 的候选数量；只影响日志/调参，不影响注入。"},
            {"name": "vector_min_score", "label": "向量召回最低分", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "余弦低于此值不算语义命中。换 embedding 模型后需要重标。"},
            {"name": "final_min_score", "label": "注入最低综合分", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "综合分低于此值不注入。调高更保守、少提前事；调低更容易带出旧记忆。"},
            {"name": "keyvec_dim", "label": "Mock 向量维度", "type": "int", "min": 8, "max": 512, "step": 8,
             "desc": "仅 mock 模式使用的哈希向量维度；真实 API 由服务商决定，改了要重建已有记忆向量。"},
            {"name": "context_budget_chars", "label": "注入字符预算", "type": "int", "min": 500, "max": 6000, "step": 100,
             "desc": "系统提示词最多字符数（中文约 1 字≈1 token 量级）。太小会截断人格/记忆。"},
            {"name": "context_safety_tail_chars", "label": "安全尾段保留字符", "type": "int", "min": 200, "max": 4000, "step": 100,
             "desc": "超预算时优先保留末尾的安全/对话规则；该段不会被截断（除非预算本身小于它）。"},
            {"name": "recent_messages", "label": "携带最近消息条数", "type": "int", "min": 2, "max": 30, "step": 1,
             "desc": "每轮带给模型的最近对话条数；太大占预算、太小丢上下文。"},
            {"name": "recent_context_budget_chars", "label": "最近消息字符预算", "type": "int", "min": 300, "max": 10000, "step": 100,
             "desc": "最近对话最多注入的字符数；超出时从最早的消息开始删/截，当前用户消息始终保留。"},
            {"name": "candidate_recent_limit", "label": "候选池：最近N条", "type": "int", "min": 50, "max": 2000, "step": 50,
             "desc": "候选池先取最近 N 条 active 记忆；调大可覆盖更多近期内容，但排序成本上升。"},
            {"name": "candidate_priority_limit", "label": "候选池：高优先N条", "type": "int", "min": 20, "max": 1000, "step": 20,
             "desc": "额外取高重要度/高情绪/pin 的各 N 条，与最近 N 合并；防止远期重要记忆被挤出候选。"},
            {"name": "recall_half_life_gain", "label": "回忆加固增益", "type": "float", "min": 0, "max": 0.2, "step": 0.005,
             "desc": "记忆被选中使用后，每次向最大半衰期靠近的比例×重要度；0=不加固，0.01≈每次小幅延长。"},
            {"name": "recall_half_life_max", "label": "回忆加固上限(天)", "type": "float", "min": 30, "max": 36500, "step": 30,
             "desc": "半衰期延长上限，防止反复回忆无限增长；默认 3650 天（约 10 年）。"},
        ],
    },
    {
        "title": "记忆门（闲聊少查 / 显式线索必查）",
        "desc": "决定这一轮要不要翻旧账：闲聊少查、明确问起才查。",
        "fields": [
            {"name": "gate_casual_limit", "label": "闲聊最多注入条数", "type": "int", "min": 0, "max": 5, "step": 1,
             "desc": "短句/打招呼时最多带几条记忆。0=完全不提旧事（只保留核心事实）。"},
            {"name": "gate_normal_limit", "label": "普通陈述最多注入条数", "type": "int", "min": 0, "max": 8, "step": 1,
             "desc": "普通聊天/陈述时最多带几条记忆。太高会显得总在翻旧账。"},
            {"name": "gate_casual_max_len", "label": "短句判定长度", "type": "int", "min": 2, "max": 30, "step": 1,
             "desc": "不超过这个字数的消息视为「闲聊」，走少查库。调高会让更多话被当闲聊。"},
            {"name": "gate_explicit_cues", "label": "显式线索词", "type": "list",
             "desc": "出现这些词就强制走完整记忆检索（如：记得、上次、答应）。用逗号分隔；改这里影响「必查」触发。"},
        ],
    },
    {
        "title": "去重与抽取",
        "desc": "决定一条消息能抽出多少记忆、哪些算重复/噪音。",
        "fields": [
            {"name": "dedup_threshold", "label": "去重相似阈值", "type": "float", "min": 0.2, "max": 0.95, "step": 0.01,
             "desc": "本地 token Jaccard 高于此值视为重复。调低更容易判重（可能漏记），调高更容易重复存。"},
            {"name": "noise_min_len", "label": "最短记忆长度", "type": "int", "min": 1, "max": 20, "step": 1,
             "desc": "短于此长度的句子不抽取（过滤「嗯/好的」）。"},
            {"name": "noise_max_len", "label": "最长记忆长度", "type": "int", "min": 50, "max": 2000, "step": 10,
             "desc": "长于此长度不抽（防长文整段入库）；长文可在对话里保留原文。"},
            {"name": "max_new_per_turn", "label": "每轮最多新记忆", "type": "int", "min": 1, "max": 30, "step": 1,
             "desc": "单轮抽取上限；太高会让记忆碎片化。"},
        ],
    },
    {
        "title": "事件打分",
        "desc": "把「开不开心/多严重/多重要」变成记忆分数；影响关系与半衰期。",
        "fields": [
            {"name": "base_score_scale", "label": "事件基准分倍率", "type": "float", "min": 1, "max": 20, "step": 0.5,
             "desc": "R_base = 倍率 × 效价 × 严重度权重；影响关系变化与印象强度。"},
            {"name": "severity_weights", "label": "严重度权重（JSON）", "type": "json",
             "desc": "1/2/3 级严重度对应的权重，例如 {\"1\":0.3,\"2\":0.6,\"3\":1.0}。"},
        ],
    },
    {
        "title": "半衰期（多久变淡）",
        "desc": "不同类别记忆的「半衰期」；越大越难忘。核心事实/技能永远不会被遗忘（后置）。",
        "fields": [
            {"name": "half_life_core", "label": "核心事实", "type": "float", "min": 1, "max": 100000, "step": 100,
             "desc": "pin 住的姓名/城市/过敏等；通常设为极大值（≈永不衰减）。"},
            {"name": "half_life_constraint", "label": "约束/偏好", "type": "float", "min": 1, "max": 100000, "step": 10,
             "desc": "「不吃香菜」「怕狗」等边界/偏好；建议很长，改口时用 superseded 而不是忘记。"},
            {"name": "half_life_anchor", "label": "人生锚点", "type": "float", "min": 1, "max": 100000, "step": 100,
             "desc": "第一次/里程碑/身份相关；建议很长，细节可淡、要点保留。"},
            {"name": "half_life_important_positive", "label": "重要正面事件", "type": "float", "min": 1, "max": 10000, "step": 10,
             "desc": "高重要度正面事件；越大越久。"},
            {"name": "half_life_episodic", "label": "普通情节", "type": "float", "min": 1, "max": 3650, "step": 5,
             "desc": "日常聊天/事件的默认半衰期；30 天约 30 天后剩一半。"},
            {"name": "half_life_minor_negative", "label": "轻微负面", "type": "float", "min": 1, "max": 3650, "step": 2,
             "desc": "小摩擦/小失望；建议比普通情节短，但不留疤。"},
            {"name": "half_life_severe_negative", "label": "严重负面（细节）", "type": "float", "min": 1, "max": 10000, "step": 10,
             "desc": "严重事件的细节半衰期；疤痕标签另存、极慢衰减。"},
            {"name": "half_life_low_info", "label": "低信息寒暄", "type": "float", "min": 1, "max": 365, "step": 1,
             "desc": "「吃了吗」这类；建议很短，快速淡出。"},
            {"name": "half_life_impression", "label": "印象/疤痕强度", "type": "float", "min": 1, "max": 100000, "step": 100,
             "desc": "印象标签强度的衰减半衰期；标签不存细节，只作态度偏置。"},
            {"name": "impression_floor", "label": "疤痕最低强度", "type": "float", "min": 0, "max": 1, "step": 0.05,
             "desc": "疤痕不会低于此强度；避免完全消失，也避免永久极端。"},
        ],
    },
    {
        "title": "遗忘降级",
        "desc": "保持率低于阈值时，从全文→摘要→gist→归档；不物理删除。",
        "fields": [
            {"name": "forget_summary_below", "label": "降为摘要阈值", "type": "float", "min": 0.1, "max": 1, "step": 0.05,
             "desc": "保持率低于此值，正文优先换成摘要。"},
            {"name": "forget_gist_below", "label": "降为 gist 阈值", "type": "float", "min": 0.05, "max": 1, "step": 0.05,
             "desc": "低于此值只留要旨/标签。"},
            {"name": "forget_archive_below", "label": "归档阈值", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "低于此值归档/遗忘（仍留记录，不物理删除）。"},
        ],
    },
    {
        "title": "关系变化",
        "desc": "一次事件对好感/信任的影响；太灵敏会情绪化，太迟钝会没人味。",
        "fields": [
            {"name": "favorability_alpha", "label": "好感步长 α", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "单次事件对好感度的更新幅度；0.1 表示基准分 5×重要度 1 时约 +0.5。"},
            {"name": "favorability_event_cap", "label": "单次好感上限", "type": "float", "min": 0.1, "max": 20, "step": 0.5,
             "desc": "单次事件最多改变多少好感，防一次事件翻盘。"},
            {"name": "trust_alpha", "label": "信任步长 β", "type": "float", "min": 0, "max": 0.5, "step": 0.01,
             "desc": "承诺/秘密/冲突修复对信任的更新幅度。"},
        ],
    },
    {
        "title": "回忆/预期奖励",
        "desc": "想起旧事、期待约定时的临时情绪增益；只影响本次会话，不写回长期基线。",
        "fields": [
            {"name": "recall_k", "label": "回忆奖励系数 k", "type": "float", "min": 0, "max": 2, "step": 0.05,
             "desc": "R_recall = k × 基准分 × e^(−λ×触发次数)。"},
            {"name": "recall_lambda", "label": "脱敏系数 λ", "type": "float", "min": 0.01, "max": 2, "step": 0.01,
             "desc": "同一记忆反复被想起时奖励递减的速度。"},
            {"name": "recall_tau_minutes", "label": "临时值衰减 τ（分钟）", "type": "float", "min": 1, "max": 240, "step": 1,
             "desc": "回忆奖励在会话内回落的时间常数。"},
            {"name": "anticipation_p_min", "label": "期待最低概率", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "预期奖励的概率下限，防止绝对化。"},
            {"name": "anticipation_p_max", "label": "期待最高概率", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "预期奖励的概率上限，防止绝对化。"},
            {"name": "disappointment_penalty", "label": "落空惩罚", "type": "float", "min": 0, "max": 1, "step": 0.01,
             "desc": "期待落空时的温和扣分；不要用于向用户施压。"},
        ],
    },
]
