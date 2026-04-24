"""HTTP API 认证辅助。"""

from __future__ import annotations

from fastapi import Request


def request_has_auth_token(request: Request, expected_token: str) -> bool:
    """检查 Authorization: Bearer 或 ?token= 是否匹配。"""
    if not expected_token:
        return True

    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token == expected_token:
        return True

    return request.query_params.get("token") == expected_token


def is_protected_http_endpoint(path: str, method: str) -> bool:
    """按路径和方法判断是否需要认证。"""
    if path in {"/health", "/status"}:
        return False
    if path == "/" or path.startswith("/static/"):
        return False
    if path == "/gateway/inbound":
        return False

    if path in {"/message", "/chat/stream", "/events", "/upload", "/upload/file"}:
        return True
    if path.startswith(("/files/", "/tasks", "/schedules", "/settings/", "/workflows", "/learning")):
        return True
    if path.startswith("/approvals") or path.startswith("/notify"):
        return True
    if path.startswith("/conversations"):
        return True
    if path.startswith("/skills/") and method.upper() != "GET":
        return True
    if path.startswith("/market/") and method.upper() != "GET":
        return True

    return False
