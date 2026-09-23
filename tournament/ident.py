"""身份归并原语。

跨赛区识别同一棋手，但不向其他赛区暴露完整身份：
- 证件号归一化后计算 HMAC「相关标识」match_code，只存不可逆码；
- 同一码在任意赛区出现都确定性地指向同一 person；
- 两种证件（身份证/护照等）产生两个码，只能由资格审核员显式合并；
- 姓名同音只产生人工复核提示，绝不自动合并；
- 明文证件仅保存在密封表，赛区视图只给脱敏串。
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata

# 声调标记 → 无声调拼音，用于同音提示（客户端也可直接传 phonetic）
_TONE_MAP = str.maketrans({
    "ā": "a", "á": "a", "ǎ": "a", "à": "a",
    "ē": "e", "é": "e", "ě": "e", "è": "e",
    "ī": "i", "í": "i", "ǐ": "i", "ì": "i",
    "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
    "ū": "u", "ú": "u", "ǔ": "u", "ù": "u",
    "ǖ": "v", "ǘ": "v", "ǚ": "v", "ǜ": "v", "ü": "v",
})

CERT_TYPES = ("idcard", "passport", "officer", "hkmo", "other")


def normalize_cert(cert_type: str, cert_no: str) -> str:
    """证件号归一化：全角转半角、去空白、大写（保留 X 等字母）。"""
    if cert_type not in CERT_TYPES:
        raise ValueError(f"不支持的证件类型: {cert_type}")
    half = unicodedata.normalize("NFKC", cert_no)
    norm = "".join(half.split()).upper()
    if not norm:
        raise ValueError("证件号为空")
    return norm


def match_code(secret: bytes, cert_type: str, cert_no: str) -> str:
    """证件表示 → 不可逆相关标识（同证同码、异证异码）。"""
    norm = normalize_cert(cert_type, cert_no)
    mac = hmac.new(secret, f"{cert_type}|{norm}".encode("utf-8"), hashlib.sha256)
    return "m1:" + mac.hexdigest()[:24]


def mask_cert(cert_type: str, cert_no: str) -> str:
    """赛区视图使用的脱敏证件号。"""
    norm = normalize_cert(cert_type, cert_no)
    if cert_type == "idcard" and len(norm) >= 8:
        return norm[:3] + "*" * (len(norm) - 7) + norm[-4:]
    if len(norm) >= 4:
        return norm[:2] + "*" * (len(norm) - 4) + norm[-2:]
    return norm[:1] + "***"


def phonetic_key(name: str, phonetic: str | None = None) -> str:
    """姓名同音键：优先使用录入的无声调拼音，否则仅做字形归一。

    内置拼音表无法覆盖全部人名，故报名接口允许录入 phonetic；
    未录入时汉字按原样保留，同音提示只在有可比拼音时才产生。
    """
    if phonetic:
        base = unicodedata.normalize("NFKC", phonetic).translate(_TONE_MAP).lower()
        return "".join(ch for ch in base if ch.isalnum())
    return "".join(name.split())


def same_phonetic(a: str | None, b: str | None) -> bool:
    return bool(a and b and a == b)
