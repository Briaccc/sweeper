"""Translations for server-side messages (CLI output, API errors, plan reasons).

Messages are written in French in the source — the project started in French —
and `EN` maps each one to English. `t()` looks a message up and fills its
`{placeholders}`; a message missing from the catalogue falls back to French,
and `test_i18n.py` fails the build when that happens.

The language is one setting for the whole instance (`[ui] language`), like the
UI language of Radarr or Sonarr: plan reasons are computed once and shared by
every page that shows them.
"""

from __future__ import annotations

import os

from sweeper.i18n_en import EN  # noqa: F401  (the catalogue is long; it lives apart)

LANGUAGES = ("en", "fr")
_current = "en"


def detect(env=None) -> str:
    """Language from the environment: SWEEPER_LANG, then the usual locale variables."""
    env = os.environ if env is None else env
    for variable in ("SWEEPER_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = (env.get(variable) or "").strip().lower()
        if value and value not in ("c", "posix") and not value.startswith("c."):
            return "fr" if value.startswith("fr") else "en"
    return "en"


def set_language(language: str | None) -> str:
    """Select the language; anything unknown (or "auto") falls back to detection."""
    global _current
    _current = language if language in LANGUAGES else detect()
    return _current


def language() -> str:
    return _current


def t(text: str, /, **values) -> str:
    """Translate `text` (French source) into the current language, then format it."""
    if _current == "en":
        text = EN.get(text, text)
    return text.format(**values) if values else text


class Reason(str):
    """A translated message that also carries a stable code.

    Plan blockers are shown to people, so they are translated; code decides on
    them too (which ones a manual selection may override), so it must not
    depend on their wording. The code survives translation, the text does not.
    """

    code: str

    def __new__(cls, code: str, text: str) -> "Reason":
        obj = super().__new__(cls, text)
        obj.code = code
        return obj

    def __getnewargs__(self):
        return (self.code, str(self))


def code_of(message) -> str:
    return getattr(message, "code", "")

