"""
Groww Authentication & Session Management Module.
Handles API key + TOTP token generation, token storage, and session validation for Groww Trading APIs.
"""
from datetime import datetime
import json
import os
import ssl
import sys
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

GROWW_API_BASE_URL = "https://api.groww.in/v1"
GROWW_TOKEN_URL = f"{GROWW_API_BASE_URL}/token/api/access"


def generate_groww_access_token(api_key: str, totp: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Generate daily Groww access token using API Key and TOTP code.
    Endpoint: POST /v1/token/api/access
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "GrowwTradingEngine/1.0",
    }
    payload = {"key_type": "totp", "totp": str(totp).strip()}
    data_bytes = json.dumps(payload).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(GROWW_TOKEN_URL, data=data_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            body = response.read().decode("utf-8")
            res = json.loads(body) if body else {"status": "SUCCESS"}
            token = res.get("token") or res.get("access_token")
            if token:
                os.environ["GROWW_AUTH_TOKEN"] = token
            return res
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else str(e)
        return {"status": "ERROR", "http_code": e.code, "error": err_body}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


def get_groww_access_token() -> Optional[str]:
    """Retrieve active Groww auth token from environment."""
    return os.environ.get("GROWW_AUTH_TOKEN") or os.environ.get("GROWW_ACCESS_TOKEN")

