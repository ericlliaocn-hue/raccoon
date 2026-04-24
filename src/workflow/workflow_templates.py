"""工作流模板市场

预置常用工作流模板，用户可一键加载使用。
"""

from __future__ import annotations

from src.types import WorkflowEntry, WorkflowStep


# ─── 预置模板 ──────────────────────────────────────────────────

def get_preset_templates() -> list[WorkflowEntry]:
    """获取所有预置工作流模板"""
    return [
        _writing_assistant(),
        _daily_report(),
        _code_review(),
        _research_summary(),
        _content_publish(),
    ]


def _writing_assistant() -> WorkflowEntry:
    """写作辅助：搜素材 → 列大纲 → 写初稿 → 润色"""
    return WorkflowEntry(
        name="写作辅助",
        description="搜索素材 → 列大纲 → 写初稿 → 润色，完整的写作流程",
        steps=[
            WorkflowStep(step_id=0, type="skill", skill_name="web_search",
                         params={"query": "{topic}"}, input_from=None),
            WorkflowStep(step_id=1, type="llm",
                         params={"text": "根据搜索结果列出文章大纲"}, input_from=0,
                         condition="on_success"),
            WorkflowStep(step_id=2, type="llm",
                         params={"text": "根据大纲写初稿"}, input_from=1,
                         condition="on_success"),
            WorkflowStep(step_id=3, type="llm",
                         params={"text": "润色初稿，使其更流畅自然"}, input_from=2,
                         condition="on_success"),
        ],
    )


def _daily_report() -> WorkflowEntry:
    """日报生成：搜新闻 → 摘要 → 格式化"""
    return WorkflowEntry(
        name="日报生成",
        description="搜索今日新闻 → LLM 摘要 → 格式化生成日报",
        steps=[
            WorkflowStep(step_id=0, type="skill", skill_name="web_search",
                         params={"query": "今日科技新闻 {date}"}),
            WorkflowStep(step_id=1, type="llm",
                         params={"text": "总结今日科技新闻要点"}, input_from=0),
            WorkflowStep(step_id=2, type="llm",
                         params={"text": "将摘要格式化为日报格式，包含标题、要点、来源"}, input_from=1),
        ],
    )


def _code_review() -> WorkflowEntry:
    """代码审查：读 diff → 分析 → 生成审查意见"""
    return WorkflowEntry(
        name="代码审查",
        description="读取代码变更 → LLM 分析 → 生成审查意见",
        steps=[
            WorkflowStep(step_id=0, type="system",
                         params={"command": "git_diff"}),
            WorkflowStep(step_id=1, type="llm",
                         params={"text": "审查以下代码变更，指出潜在问题和改进建议"}, input_from=0),
        ],
    )


def _research_summary() -> WorkflowEntry:
    """研究摘要：多源搜索 → 汇总 → 生成报告"""
    return WorkflowEntry(
        name="研究摘要",
        description="多关键词搜索 → 汇总去重 → 生成研究报告",
        steps=[
            WorkflowStep(step_id=0, type="parallel",
                         parallel_steps=[
                             WorkflowStep(step_id=10, type="skill", skill_name="web_search",
                                          params={"query": "{topic} 最新研究"}),
                             WorkflowStep(step_id=11, type="skill", skill_name="web_search",
                                          params={"query": "{topic} 技术分析"}),
                         ]),
            WorkflowStep(step_id=1, type="llm",
                         params={"text": "汇总搜索结果，去重并提取关键信息"}, input_from=0),
            WorkflowStep(step_id=2, type="llm",
                         params={"text": "基于汇总信息生成结构化研究报告"}, input_from=1),
        ],
    )


def _content_publish() -> WorkflowEntry:
    """内容发布：写文案 → 生成图片 → 发布"""
    return WorkflowEntry(
        name="内容发布",
        description="LLM 写文案 → 生成配图 → 发布到平台",
        steps=[
            WorkflowStep(step_id=0, type="llm",
                         params={"text": "为 {topic} 写一篇小红书风格的文案"}),
            WorkflowStep(step_id=1, type="skill", skill_name="image_gen",
                         params={}, input_from=0, condition="on_success"),
            WorkflowStep(step_id=2, type="skill", skill_name="xiaohongshu_publish",
                         params={}, input_from=1, condition="on_success"),
        ],
    )


# ─── 模板市场接口 ──────────────────────────────────────────────

class WorkflowTemplateMarket:
    """工作流模板市场"""

    def __init__(self) -> None:
        self._templates: dict[str, WorkflowEntry] = {}
        for t in get_preset_templates():
            self._templates[t.name] = t

    def list_templates(self) -> list[WorkflowEntry]:
        """列出所有预置模板"""
        return list(self._templates.values())

    def get_template(self, name: str) -> WorkflowEntry | None:
        """按名称获取模板"""
        return self._templates.get(name)

    def search_templates(self, query: str) -> list[WorkflowEntry]:
        """搜索模板"""
        query_lower = query.lower()
        return [
            t for t in self._templates.values()
            if query_lower in t.name.lower() or query_lower in t.description.lower()
        ]
