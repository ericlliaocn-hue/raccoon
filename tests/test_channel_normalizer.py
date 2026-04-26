from src.channels.normalizer import ChannelNormalizer


def test_normalize_builds_stable_conversation_id() -> None:
    event = ChannelNormalizer.normalize(
        channel="feishu",
        account_id="corp-001",
        chat_id="oc_abc123",
        thread_id="omt_777",
        sender_id="u_1",
        text="  你好  ",
    )

    assert event.channel == "feishu"
    assert event.conversation_id == "feishu:corp-001:oc_abc123:omt_777"
    assert event.text == "你好"


def test_from_gateway_payload_maps_attachments() -> None:
    event = ChannelNormalizer.from_gateway_payload(
        source="telegram",
        text="发送文件",
        conversation_id="gateway_telegram_demo",
        extra={
            "chat_id": "chat_9",
            "sender_id": "user_7",
            "attachments": [
                {
                    "name": "demo.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 128,
                    "url": "https://example.com/demo.txt",
                }
            ],
        },
    )

    assert event.conversation_id == "gateway_telegram_demo"
    assert len(event.attachments) == 1
    assert event.attachments[0].name == "demo.txt"
    assert event.attachments[0].size_bytes == 128

