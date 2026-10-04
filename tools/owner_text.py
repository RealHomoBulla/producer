"""Every sentence the tools write for the OWNER, in the owner's language (`producer.toml`).

Agent-facing text (docs, CLI help, logs) is English everywhere. Only what lands on the owner's
pages comes from here: the digest, the unanswered-questions page, the seed pages. `ru` and `en`
carry exactly the same keys (a test enforces it); a new language is one more dict.
"""
from __future__ import annotations

import paths

TEXT: dict[str, dict[str, str]] = {
    "ru": {
        "digest.title": "# Дайджест",
        "digest.intro": ("Что агенты закончили и что это значит. Скажи **«прочитал дайджест»** —\n"
                         "страница уедет в архив, а не удалится."),
        "digest.warning": "⚠️ Пока не прочитан — только дополняется, никогда не переписывается.",
        "digest.added": "Добавлено",
        "digest.too_long": ("section is {size} chars (max {limit}); "
                            "write the key theses only, detail belongs in the agents/ report"),
        "digest.wrong_language": "section is mostly Latin letters; the owner digest is written in Russian",
        "unanswered.title": "# Без ответа — вопросы, на которые ты ещё не ответил",
        "unanswered.rebuilt": "Пересобрано: **{when}** · открытых: **{open}** · закрытых: **{closed}**",
        "unanswered.waiting": "## Ждут тебя",
        "unanswered.columns": "| | # | вопрос | что блокирует | цена молчания | когда спросили |",
        "unanswered.empty": "_Пусто._",
        "unanswered.recent_closed": "## Последние закрытые (5 из {total})",
        "unanswered.closed_columns": "| # | вопрос | как закрыт | почему |",
        "unanswered.answered": "отвечен",
        "unanswered.stale": "неактуален",
        "unanswered.no_reason": "причина не указана",
        "unanswered.gate_unchecked": "источник(и) не удалось проверить",
        "gate.found": ("FOUND: найдено {count} прямых рулинг(ов) владельца по запросу '{query}'. "
                       "Вопрос владельцу отправлять ЗАПРЕЩЕНО (решение уже принято)."),
        "gate.error": ("ERROR / UNCHECKED: не удалось проверить {count} источников! Это НЕ означает, что "
                       "рулинга нет. По правилу FAIL-CLOSED разрешение спросить владельца ЗАБЛОКИРОВАНО."),
        "gate.not_found": ("NOT_FOUND: рулингов владельца не найдено по запросу '{query}'. "
                           "Проверено источников: {count}. Разрешено спросить владельца."),
        "gate.unreadable": "НЕ СМОГ ПРОВЕРИТЬ ИСТОЧНИКИ (FAIL-CLOSED):",
        "gate.matches": "НАЙДЕННЫЕ РУЛИНГИ ({count}):",
        "gate.ruling": "--- Рулинг #{n} [{confidence}] ---",
        "gate.date": "Дата:     {value}",
        "gate.source": "Источник: {value}",
        "gate.answer": "Ответ:    {value}",
        "gate.quote": "Цитата:   {value}",
        "gate.checked": "Проверенные источники ({count}): {value}",
    },
    "en": {
        "digest.title": "# Digest",
        "digest.intro": ("What the agents finished and what it means. Say **\"read the digest\"** - "
                         "the page is archived, not deleted."),
        "digest.warning": "⚠️ Until it is read it is only appended to, never rewritten.",
        "digest.added": "Added",
        "digest.too_long": ("section is {size} chars (max {limit}); "
                            "write the key theses only, detail belongs in the agents/ report"),
        "digest.wrong_language": "section is mostly Cyrillic; the owner digest is written in English",
        "unanswered.title": "# Unanswered - questions you have not answered yet",
        "unanswered.rebuilt": "Rebuilt: **{when}** · open: **{open}** · closed: **{closed}**",
        "unanswered.waiting": "## Waiting for you",
        "unanswered.columns": "| | # | question | what it blocks | cost of silence | asked |",
        "unanswered.empty": "_Empty._",
        "unanswered.recent_closed": "## Recently closed (5 of {total})",
        "unanswered.closed_columns": "| # | question | how closed | why |",
        "unanswered.answered": "answered",
        "unanswered.stale": "no longer relevant",
        "unanswered.no_reason": "no reason given",
        "unanswered.gate_unchecked": "source(s) could not be checked",
        "gate.found": ("FOUND: {count} direct owner ruling(s) for '{query}'. Sending the owner this "
                       "question is FORBIDDEN (it is already decided)."),
        "gate.error": ("ERROR / UNCHECKED: {count} source(s) could not be checked! This does NOT mean "
                       "there is no ruling. FAIL-CLOSED: permission to ask the owner is BLOCKED."),
        "gate.not_found": ("NOT_FOUND: no owner ruling for '{query}'. Sources checked: {count}. "
                           "You may ask the owner."),
        "gate.unreadable": "COULD NOT CHECK THESE SOURCES (FAIL-CLOSED):",
        "gate.matches": "RULINGS FOUND ({count}):",
        "gate.ruling": "--- Ruling #{n} [{confidence}] ---",
        "gate.date": "Date:     {value}",
        "gate.source": "Source:   {value}",
        "gate.answer": "Answer:   {value}",
        "gate.quote": "Quote:    {value}",
        "gate.checked": "Sources checked ({count}): {value}",
    },
}


def text(key: str, language: str | None = None, **values: object) -> str:
    """The owner-facing string `key` in `language` (default: configured), with `values` filled in."""
    table = TEXT[language or paths.owner_language()]
    return table[key].format(**values) if values else table[key]
