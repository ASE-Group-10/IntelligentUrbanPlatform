"""Split a Markdown policy document into sections (one passage each)."""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBER = re.compile(r"^\d+(\.\d+)*\.?$")


class InvalidDocumentError(ValueError):
    """The document cannot be split into uniquely identified sections."""


@dataclass(frozen=True)
class Section:
    section_id: str
    heading: str
    content: str  # verbatim body text under the heading


def _section_id(heading: str) -> str:
    first = heading.split()[0]
    if _NUMBER.match(first):  # "4.2 City-centre cordon" -> "4.2"
        return first.rstrip(".")
    return "-".join(re.findall(r"[a-z0-9]+", heading.lower()))


def split_sections(markdown: str) -> list[Section]:
    """Return every heading that has body text, in document order.

    Text before the first heading becomes section ``preamble``.

    Raises:
        InvalidDocumentError: no section has text, or two sections share an ID.
    """
    sections: list[Section] = []
    heading, section_id, body = "", "preamble", []

    def close() -> None:
        content = "\n".join(body).strip()
        if content:
            sections.append(Section(section_id, heading, content))

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            close()
            heading = match.group(2)
            section_id, body = _section_id(heading), []
        else:
            body.append(line)
    close()

    if not sections:
        raise InvalidDocumentError("document has no section with text")
    ids = [s.section_id for s in sections]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise InvalidDocumentError(f"duplicate sectionId: {duplicates}")
    return sections
