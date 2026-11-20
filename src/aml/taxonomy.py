"""领域词表：把 LLM 自造的 domain 收敛到封闭集合。

2026-11-20 实测：蒸馏让模型自由发挥 domain，2,938 条知识长出 **823 个领域**、
805 个沉淀文件（390 篇 <600B）。后果与库自己的 AGENTS.md 写的一样——
近义域互相抢检索位，人面也读不动。

口径（为什么只收敛"文件 + 新写入"）：
  · 旧条目的 `domain:` 标签**不动** —— 后端的 content_hash 把标签算进去了，
    改标签等于重存，2,900 条重存的风险远大于收益；
  · 所以：沉淀文件按词表归并、新蒸馏按词表写 domain；旧标签保留原样，只影响展示。
"""
from __future__ import annotations

from . import text

# 目标词表（≤32 个）。新增领域要先改这里，不再让模型临场发明。
VOCAB = (
    "agent-workflow", "agent-eval", "agent-memory", "agent-teams", "agent-security",
    "env-windows", "env-sandbox", "env-network", "env-python", "env-node",
    "data-scraping", "data-quality", "data-governance",
    "web-frontend", "web-deploy", "web-seo",
    "content-writing", "content-ops", "media", "office-docs",
    "browser-automation", "repo-git", "tooling", "testing",
    "security", "finance", "research", "project", "skills", "automation", "misc",
)

# 精确别名（历史 domain 名 → 词表）
ALIASES = {
    "agent-evaluation": "agent-eval", "benchmarking": "agent-eval",
    "benchmark-design": "agent-eval", "benchmark-workflow": "agent-eval",
    "agent-benchmark": "agent-eval", "llm-evaluation": "agent-eval",
    "knowledge-management": "agent-memory", "knowledge-base": "agent-memory",
    "knowledge-ops": "agent-memory", "knowledge-distillation": "agent-memory",
    "agent-knowledge-base": "agent-memory", "agent-collab": "agent-teams",
    "agent-review": "agent-teams", "agent-observability": "agent-workflow",
    "agent-integration": "agent-workflow", "agent-tooling": "tooling",
    "env-encoding": "env-windows", "env-console": "env-windows",
    "env-tooling": "tooling", "env-setup": "tooling", "env-config": "tooling",
    "env-server": "web-deploy", "env-credentials": "security",
    "data-analysis": "data-quality", "data-validation": "data-quality",
    "data-verification": "data-quality", "data-audit": "data-quality",
    "data-enrichment": "data-quality", "data-cleaning": "data-quality",
    "data-scraping": "data-scraping", "data-collection": "data-scraping",
    "data-privacy": "data-governance", "data-security": "data-governance",
    "web-scraping": "data-scraping", "web-search": "data-scraping",
    "web-research": "research", "web-design": "web-frontend",
    "web-architecture": "web-frontend", "web-testing": "testing",
    "content-generation": "content-writing", "content-rewriting": "content-writing",
    "content-editing": "content-writing", "content-planning": "content-ops",
    "content-strategy": "content-ops", "content-marketing": "content-ops",
    "content-production": "content-ops", "content-research": "content-ops",
    "writing-assistant": "content-writing", "writing-assistance": "content-writing",
    "wechat-publishing": "content-ops", "wechat": "content-ops",
    "document-generation": "office-docs", "document-processing": "office-docs",
    "document-parsing": "office-docs", "docx-generation": "office-docs",
    "docx-validation": "office-docs", "office-automation": "office-docs",
    "presentation-design": "office-docs", "presentation": "office-docs",
    "media-pipeline": "media", "media-processing": "media", "video-processing": "media",
    "audio-transcription": "media", "subtitle": "media", "video": "media",
    "browser-testing": "browser-automation", "web-automation": "browser-automation",
    "repo-hygiene": "repo-git", "repo-sync": "repo-git", "repo-discovery": "repo-git",
    "code-review": "repo-git", "code-migration": "repo-git", "code-maintenance": "repo-git",
    "release-management": "project", "release-notes": "project", "release-upgrade": "project",
    "project-planning": "project", "project-review": "project", "task-management": "project",
    "network-diagnostics": "env-network", "network-tooling": "env-network",
    "network-mirror": "env-network", "network-troubleshooting": "env-network",
    "skill-authoring": "skills", "skill-building": "skills", "skill-distillation": "skills",
    "automation-scheduling": "automation", "workflow-automation": "automation",
    "task-automation": "automation", "workflow-design": "automation",
    "research-methodology": "research", "research-method": "research",
    "trading": "finance", "trading-risk": "finance", "finance": "finance",
}

# 先按内容关键词判（比 domain 名更准），再按前缀兜底。顺序 = 优先级。
KEYWORDS = (
    (("评测", "判分", "基准", "benchmark", "评分"), "agent-eval"),
    (("记忆", "知识库", "检索", "召回", "注入预算"), "agent-memory"),
    (("子代理", "子 agent", "subagent", "分片", "多 agent", "盲评"), "agent-teams"),
    (("沙箱", "sandbox", "越界", "ACL 权限", "权限拒绝"), "env-sandbox"),
    (("编码", "乱码", "GBK", "UTF-8", "BOM", "PowerShell", "CRLF"), "env-windows"),
    (("抓取", "爬虫", "反爬", "采集", "限流", "接口翻页"), "data-scraping"),
    (("数据质量", "核对", "抽样", "缺口", "脏数据", "去重"), "data-quality"),
    (("合规", "隐私", "脱敏", "授权", "风控", "客户名单"), "data-governance"),
    (("浏览器", "CDP", "调试端口", "user-data-dir", "页面渲染"), "browser-automation"),
    (("公众号", "微信", "发布", "排版", "草稿"), "content-ops"),
    (("写作", "文案", "改写", "口播", "稿", "标题"), "content-writing"),
    (("Word", "docx", "PPT", "文档", "Excel", "表格"), "office-docs"),
    (("视频", "音频", "字幕", "转写", "ffmpeg", "剪辑"), "media"),
    (("git ", "jj ", "提交", "仓库", "分支", "回滚", "commit"), "repo-git"),
    (("前端", "CSS", "HTML", "页面", "样式"), "web-frontend"),
    (("部署", "上线", "域名", "服务器", "nginx", "端口"), "web-deploy"),
    (("SEO", "GEO", "搜索排名", "关键词布局"), "web-seo"),
    (("凭据", "密钥", "token", "漏洞", "注入攻击"), "security"),
    (("测试", "pytest", "回归", "fixture"), "testing"),
    (("网络", "代理", "镜像", "DNS", "超时", "连接重置"), "env-network"),
    (("Python", "pip", "venv", "虚拟环境"), "env-python"),
    (("Node", "npm", "pnpm", "package.json"), "env-node"),
    (("行情", "股票", "交易", "投资", "财报"), "finance"),
    (("调研", "方法论", "选型", "对比实验"), "research"),
)

# 内容没命中时按 domain 前缀兜底
PREFIXES = (
    ("agent-", "agent-workflow"), ("env-", "env-windows"), ("data-", "data-quality"),
    ("web-", "web-frontend"), ("content-", "content-writing"), ("file-", "repo-git"),
    ("repo-", "repo-git"), ("code-", "repo-git"), ("tooling", "tooling"),
    ("knowledge-", "agent-memory"), ("media-", "media"), ("video", "media"),
    ("audio", "media"), ("office-", "office-docs"), ("docx", "office-docs"),
    ("document-", "office-docs"), ("presentation", "office-docs"),
    ("browser-", "browser-automation"), ("network-", "env-network"),
    ("mcp-", "tooling"), ("security-", "security"), ("frontend-", "web-frontend"),
    ("testing", "testing"), ("test-", "testing"), ("automation", "automation"),
    ("workflow", "automation"), ("research-", "research"), ("project-", "project"),
    ("release-", "project"), ("finance-", "finance"), ("trading", "finance"),
    ("skill-", "skills"), ("seo", "web-seo"), ("wechat", "content-ops"),
    ("windows", "env-windows"), ("writing", "content-writing"), ("llm-", "agent-workflow"),
    ("nlp-", "agent-workflow"), ("api-", "tooling"), ("cli-", "tooling"),
)


# 历史数据里出现过 `checklist/env-setup` 这种"类型/领域"混写；domain_of 会折成
# `checklist-env-setup`，把类型当前缀再去映射一次，否则整串落到 misc。
KTYPE_PREFIXES = ("checklist-", "pitfall-", "pattern-", "decision-", "gotcha-",
                  "reference-", "observation-", "learning-", "note-", "procedure-")


def canon(domain: str, title: str = "", content: str = "") -> str:
    """把任意 domain 收敛到词表里的一个值（永不返回空）。"""
    raw = text.domain_of(domain or "")
    if raw in VOCAB:
        return raw
    if raw in ALIASES:
        return ALIASES[raw]
    for prefix in KTYPE_PREFIXES:
        if raw.startswith(prefix):
            rest = raw[len(prefix):]
            if rest in VOCAB:
                return rest
            if rest in ALIASES:
                return ALIASES[rest]
            raw = rest
            break
    blob = f"{title} {content}"[:500]
    for words, target in KEYWORDS:
        if any(w in blob for w in words):
            return target
    for prefix, target in PREFIXES:
        if raw.startswith(prefix):
            return target
    return "misc"
