from __future__ import annotations

import json
from types import SimpleNamespace

from src.channels.adapters import parse_feishu_callback, parse_feishu_ws_event


def test_parse_url_verification_success() -> None:
    result = parse_feishu_callback(
        {"type": "url_verification", "challenge": "abc", "token": "vtok"},
        verification_token="vtok",
    )
    assert result.ok is True
    assert result.challenge == "abc"


def test_parse_url_verification_rejects_bad_token() -> None:
    result = parse_feishu_callback(
        {"type": "url_verification", "challenge": "abc", "token": "bad"},
        verification_token="vtok",
    )
    assert result.ok is False
    assert result.status_code == 401


def test_parse_message_text_event() -> None:
    payload = {
        "header": {
            "event_type": "im.message.receive_v1",
            "token": "vtok",
            "app_id": "cli_a",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "你好 raccoon"}, ensure_ascii=False),
            },
            "mentions": [{"id": {"open_id": "ou_bot_1"}, "name": "Raccoon"}],
        },
    }
    result = parse_feishu_callback(payload, verification_token="vtok")
    assert result.ok is True
    assert result.channel_event is not None
    assert result.channel_event.text == "你好 raccoon"
    assert result.channel_event.conversation_id.startswith("feishu:cli_a:oc_1")


def test_parse_message_group_requires_mention_by_default() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1", "token": "vtok"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}, ensure_ascii=False),
            },
            "mentions": [],
        },
    }
    result = parse_feishu_callback(payload, verification_token="vtok")
    assert result.ok is True
    assert result.channel_event is None
    assert result.detail == "ignored_group_without_mention"


def test_parse_message_respects_allowlist() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1", "token": "vtok"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user_1"}},
            "message": {
                "chat_id": "oc_forbidden",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "你好"}, ensure_ascii=False),
            },
            "mentions": [],
        },
    }
    result = parse_feishu_callback(
        payload,
        verification_token="vtok",
        allow_chat_ids=["oc_allowed"],
    )
    assert result.ok is True
    assert result.channel_event is None
    assert result.detail == "ignored_chat_not_allowlisted"


def test_parse_file_message_to_attachment() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1", "token": "vtok"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user_1"}},
            "message": {
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "file",
                "content": json.dumps({"file_key": "file_x", "file_name": "demo.txt"}),
            },
            "mentions": [],
        },
    }
    result = parse_feishu_callback(payload, verification_token="vtok")
    assert result.ok is True
    assert result.channel_event is not None
    assert result.channel_event.text == "[feishu:file]"
    assert len(result.channel_event.attachments) == 1
    assert result.channel_event.attachments[0].name == "demo.txt"


def test_parse_encrypted_callback_not_supported() -> None:
    result = parse_feishu_callback({"encrypt": "xxxx"}, verification_token="vtok")
    assert result.ok is False
    assert result.status_code == 400
    assert result.detail == "encrypted_callback_not_supported_yet"


def test_parse_ws_message_event() -> None:
    ws_event = SimpleNamespace(
        header=SimpleNamespace(event_type="im.message.receive_v1", token="vtok", app_id="cli_ws"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_user_2", user_id=None, union_id=None),
                sender_type="user",
            ),
            message=SimpleNamespace(
                message_id="om_ws_1",
                root_id=None,
                parent_id=None,
                chat_id="oc_ws_1",
                chat_type="p2p",
                message_type="text",
                content=json.dumps({"text": "ws 你好"}, ensure_ascii=False),
                mentions=[],
            ),
        ),
    )
    result = parse_feishu_ws_event(ws_event, verification_token="vtok")
    assert result.ok is True
    assert result.channel_event is not None
    assert result.channel_event.text == "ws 你好"
    assert result.channel_event.conversation_id.startswith("feishu:cli_ws:oc_ws_1")
