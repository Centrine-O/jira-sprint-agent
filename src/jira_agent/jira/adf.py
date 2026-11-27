"""Converts plain text / simple markdown into Atlassian Document Format (ADF).

Jira's REST API v3 requires ADF for comments and descriptions.
Supported: paragraphs, line breaks, bullet lists (- or *), **bold**, `code`,
[text](https://link) and bare https:// links.
"""

from __future__ import annotations

import re
from typing import Any

INLINE = re.compile(
    r"\*\*(?P<bold>[^*]+)\*\*"
    r"|`(?P<code>[^`]+)`"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<lurl>https?://[^)\s]+)\)"
    r"|(?P<url>https?://[^\s)]+)"
)
BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def _inline(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos:m.start()]})
        if m.group("bold"):
            nodes.append({"type": "text", "text": m.group("bold"), "marks": [{"type": "strong"}]})
        elif m.group("code"):
            nodes.append({"type": "text", "text": m.group("code"), "marks": [{"type": "code"}]})
        elif m.group("ltext"):
            nodes.append({"type": "text", "text": m.group("ltext"),
                          "marks": [{"type": "link", "attrs": {"href": m.group("lurl")}}]})
        else:
            url = m.group("url")
            nodes.append({"type": "text", "text": url,
                          "marks": [{"type": "link", "attrs": {"href": url}}]})
        pos = m.end()
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return nodes


def _paragraph(lines: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if i > 0:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "paragraph", "content": content}


def to_adf(text: str | None) -> dict[str, Any]:
    """Turn plain text into an ADF document Jira accepts."""
    blocks: list[dict[str, Any]] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(_paragraph(para))
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            blocks.append({
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [_paragraph([item])]} for item in bullets
                ],
            })
            bullets.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        bullet = BULLET.match(line)
        if bullet:
            flush_para()
            bullets.append(bullet.group(1))
        elif not line.strip():
            flush_para()
            flush_bullets()
        else:
            flush_bullets()
            para.append(line.strip())
    flush_para()
    flush_bullets()

    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}
