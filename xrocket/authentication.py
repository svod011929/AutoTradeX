"""Headers and the WebSocket auth frame.

These helpers return the token to the caller. Callers must not log the result.
"""


def build_public_headers() -> dict[str, str]:
    return {"Accept": "application/json"}


def build_private_headers(token: str) -> dict[str, str]:
    cleaned = token.strip()
    if cleaned == "":
        raise ValueError("private requests require an API token")
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {cleaned}",
    }


def ws_auth_message(request_id: str, token: str) -> dict[str, object]:
    cleaned = token.strip()
    if cleaned == "":
        raise ValueError("websocket auth requires an API token")
    return {"id": request_id, "method": "auth", "params": {"token": cleaned}}
