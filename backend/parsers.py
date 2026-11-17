"""Document understanding: Docling for PDFs (30s timeout -> pypdf), lightweight parsers for the rest."""
import asyncio
import email
import email.policy
import logging
import re
from pathlib import Path

log = logging.getLogger("daybreak.parsers")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def parse_ics(text: str) -> dict:
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    out = {}
    for line in unfolded.splitlines():
        m = re.match(r"^(SUMMARY|LOCATION|DTSTART|DTEND|DESCRIPTION)(;[^:]*)?:(.*)$", line.strip())
        if m and m.group(1).lower() not in out:
            out[m.group(1).lower()] = m.group(3).replace("\\,", ",").replace("\\n", "\n").strip()
    return out


def parse_eml(text: str) -> dict:
    msg = email.message_from_string(text, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else (msg.get_payload() if not msg.is_multipart() else "")
    return {
        "subject": str(msg.get("Subject", "")),
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "date": str(msg.get("Date", "")),
        "body": str(body).strip(),
    }


def _docling_convert(path: str) -> str:
    from docling.document_converter import DocumentConverter

    return DocumentConverter().convert(path).document.export_to_markdown()


def _pypdf_extract(path: str) -> str:
    from pypdf import PdfReader

    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages).strip()


async def parse_pdf(path: Path) -> tuple[str, dict]:
    """Docling first; if it errors or exceeds 30s (e.g. first-run model download), fall back to pypdf."""
    try:
        text = await asyncio.wait_for(asyncio.to_thread(_docling_convert, str(path)), timeout=30)
        if text.strip():
            return text, {"parser": "docling"}
        reason = "docling returned empty text"
    except TimeoutError:
        reason = "docling exceeded 30s timeout"
    except Exception as e:
        reason = f"docling error: {type(e).__name__}: {e}"[:300]
    log.warning("PDF fallback to pypdf: %s", reason)
    try:
        text = await asyncio.to_thread(_pypdf_extract, str(path))
        return text, {"parser": "pypdf", "fallback_reason": reason}
    except Exception as e:
        return "", {"parser": "none", "fallback_reason": reason, "error": f"{type(e).__name__}: {e}"[:300]}


def find_urls(text: str) -> list[str]:
    return [u.rstrip(".,;") for u in URL_RE.findall(text or "")]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def quote_containing(text: str, needle: str, width: int = 160) -> str | None:
    """Return the exact line/sentence from `text` that literally contains `needle` (case-insensitive)."""
    if not text or not needle:
        return None
    i = text.lower().find(needle.lower())
    if i < 0:
        return None
    start = max(text.rfind("\n", 0, i) + 1, i - width)
    end_nl = text.find("\n", i + len(needle))
    end = min(end_nl if end_nl >= 0 else len(text), i + len(needle) + width)
    return text[start:end].strip()


def deterministic_extract(roles: dict) -> dict:
    """Regex/field-based extraction straight from source text. Used to cross-check and back up Gemini."""
    out = {k: None for k in ("event_title", "event_time", "calendar_location", "email_location",
                             "portfolio_url", "expected_portfolio_keyword", "referenced_resume", "latest_resume")}
    ics, eml, url, notes = (roles.get(k) for k in ("calendar", "email", "url", "notes"))
    if ics:
        p = parse_ics(ics["content"])
        out["event_title"] = p.get("summary")
        out["event_time"] = p.get("dtstart")
        out["calendar_location"] = p.get("location")
    if eml:
        body = parse_eml(eml["content"])["body"] or eml["content"]
        m = re.search(r"(Building\s+[A-Z0-9]+,?\s+\d+\s+[A-Za-z .]+?\b(?:St|Street|Ave|Avenue|Blvd|Rd|Road|Way))\b", body)
        if m:
            out["email_location"] = m.group(1).strip()
        m = re.search(r"[\"“]([^\"”\n]{2,80})[\"”]", body)
        if m:
            out["expected_portfolio_keyword"] = m.group(1).strip()
    if url:
        urls = find_urls(url["content"])
        out["portfolio_url"] = urls[0] if urls else None
    if notes:
        text = notes["content"]
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            files = re.findall(r"[\w\-]+\.pdf", sentence)
            if not files:
                continue
            low = sentence.lower()
            if re.search(r"latest|newest|most recent|current", low):
                out["latest_resume"] = out["latest_resume"] or files[-1]
                if len(files) > 1:
                    out["referenced_resume"] = out["referenced_resume"] or files[0]
            elif re.search(r"link|deck|presentation|references|points to", low):
                out["referenced_resume"] = out["referenced_resume"] or files[0]
    return out


def resume_version(name: str) -> tuple:
    """Deterministic version key from a filename, e.g. Resume_v2_2026_Final.pdf -> (2, 2026)."""
    v = re.search(r"[vV](\d+)", name or "")
    y = re.search(r"(20\d{2})", name or "")
    return (int(v.group(1)) if v else -1, int(y.group(1)) if y else -1)
