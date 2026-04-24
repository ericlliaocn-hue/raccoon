"""LLM 客户端：支持讯飞星火 Spark API（OpenAI 兼容协议）

讯飞星火大模型 API 使用 OpenAI 兼容接口：
- Base URL: https://spark-api-open.xf-yun.com/v1
- Model: generalv3.5 (Spark4.0 Ultra) / generalv3 (Spark3.5 Max) / 4.0Ultra 等
- 认证: Bearer Token (APIPassword)
- 可通过环境变量 RACCOON_LLM_API_KEY / RACCOON_LLM_MODEL 覆盖

文档: https://www.xfyun.cn/doc/spark/Web.html
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

import structlog
from openai import AsyncOpenAI

from src.config import RaccoonConfig

logger = structlog.get_logger(__name__)

# 讯飞 API 常量
SPARK_BASE_URL = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
SPARK_DEFAULT_MODEL = "astron-code-latest"  # 讯飞 codeplan

# 系统提示词
SYSTEM_PROMPT = """你是「浣熊」(Project Raccoon)，一个事件驱动的 AI 智能体助手。

你的特点：
- 你运行在一个事件驱动的智能体框架中
- 你可以调用各种 Skill（技能）来完成任务
- 你擅长对话、问答、创意写作、任务规划

调用 Skill 的方式：
- @skill名：直接调用，如 @image_gen 一只猫
- @别名：每个 Skill 有中文别名，如 @图片生成、@AI绘画、@网页浏览 等
- 触发词：自然语言中包含触发词也会自动路由，如"生成图片一只猫"

行为准则：
- 用中文回复，简洁友好
- 如果用户想执行具体任务，建议他们使用 @skill名 或 @别名 调用
- 如果用户查询任务状态，建议他们输入"进度"或"状态"
- 输入 /help 查看所有可用指令，/skills 查看已安装 Skill 及其别名
"""


class LLMClient(ABC):
    """LLM 客户端抽象接口"""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """发送聊天请求，返回回复文本"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Any:
        """发送流式聊天请求，返回异步迭代器"""
        ...

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """发送带工具定义的聊天请求（预留，当前用 prompt 模拟）

        Args:
            tools: OpenAI function calling 格式的工具列表

        Returns:
            {"content": "回复文本", "tool_calls": [...]} 或 {"content": "回复文本"}
        """
        # 默认实现：忽略 tools，走普通 chat
        reply = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return {"content": reply}


class MockLLMClient(LLMClient):
    """Mock LLM 客户端（无需 API Key）"""

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        user_msg = messages[-1].get("content", "") if messages else ""
        return (
            f"[Mock LLM] 收到: {user_msg}\n\n"
            "当前 LLM 为 Mock 模式。请在 config.json 或环境变量中配置讯飞星火 API Key 以启用真实对话。\n\n"
            "配置方式：\n"
            '  1. 编辑 config.json: {"llm_provider": "spark", "llm_api_key": "你的APIKey", "llm_model": "generalv3.5"}\n'
            "  2. 或设置环境变量: RACCOON_LLM_PROVIDER=spark RACCOON_LLM_API_KEY=xxx"
        )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Any:
        """Mock 不支持流式，直接返回完整文本"""
        text = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)

        async def _gen():
            yield text

        return _gen()

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Mock chat_with_tools"""
        reply = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return {"content": reply}


class SparkLLMClient(LLMClient):
    """讯飞星火大模型客户端（OpenAI 兼容协议）"""

    def __init__(self, config: RaccoonConfig) -> None:
        api_key = config.llm_api_key
        base_url = config.llm_base_url or SPARK_BASE_URL
        model = config.llm_model or SPARK_DEFAULT_MODEL

        if not api_key:
            raise ValueError(
                "讯飞星火 API Key 未配置！"
                "请在 config.json 中设置 llm_api_key，"
                "或设置环境变量 RACCOON_LLM_API_KEY"
            )

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self._model = model
        self._base_url = base_url

        logger.info(
            "spark_client_initialized",
            model=self._model,
            base_url=self._base_url,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """调用讯飞星火 Chat API"""
        try:
            # 注入系统提示词（如果用户没有提供）
            if not messages or messages[0].get("role") != "system":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            content = response.choices[0].message.content or ""
            logger.debug(
                "spark_chat_completed",
                model=self._model,
                tokens=getattr(response.usage, "total_tokens", 0) if response.usage else 0,
            )
            return content

        except Exception as e:
            logger.error("spark_chat_failed", error=str(e))
            raise

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Any:
        """流式调用讯飞星火 Chat API"""
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        async def _gen():
            try:
                async for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content
            except Exception as e:
                logger.error("spark_stream_failed", error=str(e))
                raise

        return _gen()

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """讯飞星火 chat_with_tools（预留，当前用 prompt 模拟）

        讯飞星火 API 的 function calling 支持有限，
        当前用 prompt 模拟工具调用，后续按需接入原生 function calling。
        """
        if not tools:
            reply = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
            return {"content": reply}

        # 将工具定义注入 system prompt 模拟
        tool_desc = json.dumps(tools, ensure_ascii=False, indent=2)
        tool_prompt = (
            "你可以使用以下工具：\n"
            f"{tool_desc}\n\n"
            "如果需要调用工具，请在回复中用 JSON 格式包裹："
            "```tool_call\n{\"name\": \"工具名\", \"arguments\": {...}}\n```\n"
            "如果不需要调用工具，直接回复即可。"
        )
        enhanced_messages = [{"role": "system", "content": tool_prompt}] + messages
        reply = await self.chat(enhanced_messages, temperature=temperature, max_tokens=max_tokens)

        # 尝试解析工具调用
        tool_call_match = re.search(r"```tool_call\s*\n([\s\S]*?)```", reply)
        if tool_call_match:
            try:
                tool_call_data = json.loads(tool_call_match.group(1).strip())
                content = reply[:tool_call_match.start()].strip()
                return {
                    "content": content or None,
                    "tool_calls": [tool_call_data],
                }
            except json.JSONDecodeError:
                pass

        return {"content": reply}


class LLMFactory:
    """LLM 客户端工厂"""

    @staticmethod
    def create(config: RaccoonConfig) -> LLMClient:
        """根据配置创建 LLM 客户端"""
        provider = config.llm_provider.lower()

        if provider == "mock":
            logger.info("llm_provider", provider="mock")
            return MockLLMClient()

        elif provider in ("spark", "xfyun", "iflytek"):
            logger.info("llm_provider", provider="spark")
            return SparkLLMClient(config)

        elif provider == "openai":
            # 通用 OpenAI 兼容接口
            logger.info("llm_provider", provider="openai_compatible", base_url=config.llm_base_url)
            return SparkLLMClient(config)  # 复用 Spark 客户端（协议兼容）

        else:
            logger.warning("unknown_llm_provider", provider=provider, fallback="mock")
            return MockLLMClient()
