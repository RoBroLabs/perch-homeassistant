"""The fingerprint word list, shared with the Perch app (`protocol/words.json`)."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

WORDS_FILE = Path(__file__).parent / "words.json"
_CACHE_KEY = f"{DOMAIN}_words"
WORD_COUNT = 1296


def load_words(path: Path = WORDS_FILE) -> list[str]:
    """Read and check the word list (blocking)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    words = data["words"] if isinstance(data, dict) else data
    if len(words) != WORD_COUNT or len(set(words)) != WORD_COUNT:
        raise ValueError("Perch word list must hold 1296 distinct words")
    return list(words)


async def async_get_words(hass: HomeAssistant) -> list[str]:
    """The word list, read once off the event loop."""
    words = hass.data.get(_CACHE_KEY)
    if words is None:
        words = await hass.async_add_executor_job(load_words)
        hass.data[_CACHE_KEY] = words
    return words
