"""Text normalization shared by matching and publication-version detection."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any


def normalize_title(value: str) -> str:
    value = value or ""
    for _ in range(2):
        value = html.unescape(value)
    value = re.sub(r"&amp;?", " and ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = value.replace("’", "'").replace("‘", "'").replace("`", "'")
    value = re.sub(r"['\"]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def author_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", value)


def normalized_metadata(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)
