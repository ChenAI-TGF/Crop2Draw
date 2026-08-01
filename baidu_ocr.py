"""Baidu OCR: 高精度 accurate_basic / 标准版 general_basic."""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

CONFIG_PATH = Path(__file__).resolve().parent / "secrets.json"
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"

PROFILES = {
    "accurate": {
        "label": "高精度版",
        "endpoint": "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
        "key_fields": ("baidu_api_key", "baidu_secret_key"),
    },
    "standard": {
        "label": "标准版",
        "endpoint": "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
        "key_fields": ("baidu_standard_api_key", "baidu_standard_secret_key"),
    },
}

# per-profile token cache
_token_cache: dict[str, dict] = {
    "accurate": {"access_token": "", "expires_at": 0.0},
    "standard": {"access_token": "", "expires_at": 0.0},
}

_active_profile = "standard"


def load_secrets() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"未找到 {CONFIG_PATH.name}，请先配置百度 API Key / Secret Key"
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def set_profile(profile: str) -> None:
    global _active_profile
    if profile not in PROFILES:
        raise ValueError(f"未知 OCR 配置: {profile}")
    _active_profile = profile


def get_profile() -> str:
    return _active_profile


def profile_label(profile: str | None = None) -> str:
    p = profile or _active_profile
    return PROFILES[p]["label"]


def _creds_for(profile: str, secrets: dict) -> tuple[str, str]:
    api_f, sec_f = PROFILES[profile]["key_fields"]
    api_key = secrets.get(api_f) or ""
    secret = secrets.get(sec_f) or ""
    # backward compatible fallback for accurate
    if profile == "accurate" and (not api_key or not secret):
        api_key = secrets.get("baidu_api_key", "")
        secret = secrets.get("baidu_secret_key", "")
    if not api_key or not secret:
        raise ValueError(f"{PROFILES[profile]['label']} 缺少 API Key / Secret Key（字段 {api_f}, {sec_f}）")
    return api_key, secret


def get_access_token(force: bool = False, profile: str | None = None) -> str:
    profile = profile or _active_profile
    cache = _token_cache[profile]
    now = time.time()
    if not force and cache["access_token"] and now < cache["expires_at"] - 60:
        return cache["access_token"]

    secrets = load_secrets()
    api_key, secret = _creds_for(profile, secrets)
    query = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret,
        }
    )
    req = urllib.request.Request(f"{TOKEN_URL}?{query}", method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if "access_token" not in payload:
        raise RuntimeError(f"获取 access_token 失败 ({profile}): {payload}")
    cache["access_token"] = payload["access_token"]
    cache["expires_at"] = now + float(payload.get("expires_in", 2592000))
    return cache["access_token"]


def image_to_base64_jpeg(im: Image.Image, quality: int = 92) -> str:
    buf = io.BytesIO()
    rgb = im.convert("RGB")
    rgb.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def ocr_image(
    im: Image.Image,
    language_type: str = "CHN_ENG",
    profile: str | None = None,
) -> str:
    """Return recognized text joined by newlines."""
    profile = profile or _active_profile
    endpoint = PROFILES[profile]["endpoint"]
    token = get_access_token(profile=profile)
    body = urllib.parse.urlencode(
        {
            "image": image_to_base64_jpeg(im),
            "language_type": language_type,
            "detect_direction": "false",
            "paragraph": "false",
            "probability": "false",
        }
    ).encode("utf-8")

    def _call(tok: str) -> dict:
        req = urllib.request.Request(
            f"{endpoint}?access_token={urllib.parse.quote(tok)}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OCR HTTP {e.code}: {detail}") from e

    payload = _call(token)
    if "error_code" in payload:
        if payload.get("error_code") in (110, 111):
            payload = _call(get_access_token(force=True, profile=profile))
        if "error_code" in payload:
            raise RuntimeError(f"OCR 失败 ({PROFILES[profile]['label']}): {payload}")

    words = [item.get("words", "") for item in payload.get("words_result", [])]
    return "\n".join(w for w in words if w).strip()
