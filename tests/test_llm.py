"""LLM 客户端测试"""

import pytest
from src.config import RaccoonConfig
from src.llm import LLMFactory, MockLLMClient, SparkLLMClient


@pytest.mark.asyncio
async def test_mock_client_chat():
    client = MockLLMClient()
    messages = [{"role": "user", "content": "你好"}]
    reply = await client.chat(messages)
    assert "Mock LLM" in reply
    assert "你好" in reply


@pytest.mark.asyncio
async def test_mock_client_stream():
    client = MockLLMClient()
    messages = [{"role": "user", "content": "测试"}]
    gen = await client.chat_stream(messages)
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    assert len(chunks) > 0
    assert "Mock" in chunks[0]


def test_factory_creates_mock():
    config = RaccoonConfig(llm_provider="mock")
    client = LLMFactory.create(config)
    assert isinstance(client, MockLLMClient)


def test_factory_creates_spark():
    config = RaccoonConfig(
        llm_provider="spark",
        llm_api_key="test-key",
        llm_model="generalv3.5",
    )
    client = LLMFactory.create(config)
    assert isinstance(client, SparkLLMClient)


def test_factory_spark_no_key_raises():
    config = RaccoonConfig(llm_provider="spark", llm_api_key="")
    with pytest.raises(ValueError, match="API Key"):
        LLMFactory.create(config)


def test_factory_unknown_falls_back_to_mock():
    config = RaccoonConfig(llm_provider="unknown_provider")
    client = LLMFactory.create(config)
    assert isinstance(client, MockLLMClient)


def test_factory_openai_compatible():
    config = RaccoonConfig(
        llm_provider="openai",
        llm_api_key="test-key",
        llm_base_url="https://api.example.com/v1",
    )
    client = LLMFactory.create(config)
    assert isinstance(client, SparkLLMClient)


def test_config_defaults():
    config = RaccoonConfig()
    assert config.llm_provider == "spark"
    assert config.llm_model == "astron-code-latest"
    assert "maas-coding-api" in config.llm_base_url
