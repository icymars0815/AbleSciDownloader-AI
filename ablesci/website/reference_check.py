"""Independent Crossref/PubMed evidence before every request, also used for the site's missing-DOI fallback.
Unknown is not nonexistent. An AI statement, supplied metadata, or a successful site lookup is never proof."""
import html
import re
from urllib.error import HTTPError
from urllib.parse import quote

from ablesci.flow.core import doi_value, critical_title_difference, normalize_author, now
from ablesci.papers import metadata
from ablesci.website import publish_policy as pp

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# 科研通 form: Assist[type] radio values (read from the real page, 2026-11-10)
FORM_TYPES = {"1": "期刊论文", "3": "书籍", "4": "书籍（某章节）", "2": "学位论文", "0": "其它（专利、报告等）"}
CROSSREF_TO_FORM = {
    "journal-article": "1",
    "book-chapter": "4", "book-section": "4", "book-part": "4", "reference-entry": "4",
    "book": "3", "monograph": "3", "edited-book": "3", "reference-book": "3", "book-set": "3", "book-series": "3",
    "dissertation": "2",
}


def form_type(crossref_type):
    """科研通 type value for a Crossref work type; anything unknown is 其它 ("0")."""
    return CROSSREF_TO_FORM.get(str(crossref_type or "").lower(), "0")


def crossref(doi, get=None):
    """Crossref's record for the DOI, or None when Crossref does not know it (HTTP 404)."""
    try:
        raw = (get or metadata.get_json)("https://api.crossref.org/works/" + quote(doi, safe="/"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    m = raw.get("message") or {}
    if doi_value(m.get("DOI", "")) != doi:
        raise ValueError("Crossref returned another DOI")
    issued = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    return dict(source="Crossref", doi=doi, title=html.unescape(re.sub(r"<[^>]*>", "", (m.get("title") or [""])[0] or "")).strip(), kind=m.get("type") or "",
                container=((m.get("container-title") or [""])[0] or "").strip(), publisher=m.get("publisher") or "",
                year=issued[0] if issued else None,
                authors=[" ".join(filter(None, [a.get("given", ""), a.get("family", "")])) or a.get("name", "") for a in m.get("author") or []][:300],
                editors=[" ".join(filter(None, [a.get("given", ""), a.get("family", "")])) or a.get("name", "") for a in m.get("editor") or []][:300])


def pubmed(doi, get=None):
    """PubMed's record for the DOI (journal articles only), or None."""
    def request(endpoint, params):
        if get is not None:
            from urllib.parse import urlencode
            return get("%s/%s?%s" % (EUTILS, endpoint, urlencode(dict(params, retmode="json"))))
        from ablesci.papers.literature_search import _pubmed_get
        return _pubmed_get(endpoint, params)
    found = request("esearch.fcgi", dict(db="pubmed", term=doi + "[AID]"))
    if found.get("error") or found.get("ERROR") or (found.get("esearchresult") or {}).get("ERROR"):
        raise ValueError("PubMed query failed")
    if not isinstance(found.get("esearchresult", {}).get("idlist"), list):
        raise ValueError("Invalid PubMed search response")
    ids = (found.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return None
    if len(ids) != 1:
        raise ValueError("PubMed returned ambiguous records")
    summary = (request("esummary.fcgi", dict(db="pubmed", id=ids[0])).get("result") or {}).get(ids[0]) or {}
    ids_doi = [doi_value(a.get("value", "")) for a in summary.get("articleids") or [] if a.get("idtype") == "doi"]
    if doi not in ids_doi:
        raise ValueError("PubMed summary did not confirm the DOI")
    return dict(source="PubMed", doi=doi, title=html.unescape(re.sub(r"<[^>]*>", "", str(summary.get("title") or ""))).strip().rstrip("."), kind="journal-article",
                container=summary.get("fulljournalname") or summary.get("source") or "", publisher="",
                year=(str(summary.get("pubdate") or "")[:4] or None), authors=[a.get("name", "") for a in summary.get("authors") or []][:300],
                pmid=ids[0])


def _author_matches(cited, registered):
    cited = re.sub(r"\bet\s+al\b.*", "", cited, flags=re.I)
    a, b = normalize_author(cited).split(), normalize_author(registered).split()
    return bool(a and b and any(len(t) > 1 and t in b for t in a) and all(
        any(t == u or (len(t) == 1 or len(u) == 1) and (t.startswith(u) or u.startswith(t)) for u in b) for t in a))


def title_agrees(cited, registered):
    clean = pp.clean_cited_title(cited, registered) if pp.looks_like_citation(cited) else ""
    if clean:
        # A title followed by extra content-changing words is not an exact title embedded in a citation.
        match = re.search(re.escape(registered.rstrip(".")) + r"(.*)$", cited, re.I | re.S)
        suffix = match.group(1).lstrip() if match else "unrecognized"
        if suffix and not suffix.startswith((".", "?", "!", "。")):
            clean = ""
    compared = clean or cited
    return (pp.title_similarity(compared, registered) >= pp.TITLE_MIN and
            not critical_title_difference(compared, registered)["conflict"] and
            not critical_title_difference(registered, compared)["conflict"])


def verify(doi, expected_title, get=None, *, authors=(), allow_doi_only=False):
    """One matching primary source suffices; conflicting records hold. Missing coverage is not proof of nonexistence.
    Original title/authors, when provided, must match. Explicit DOI-only input verifies existence, not a citation."""
    doi = doi_value(doi)
    out = dict(verified=False, state="unavailable", sources=[], record=None, problems=[], checks=[], checked_at=now(),
               against="cited title" if expected_title else "DOI only (the list gave no title)")
    if not doi:
        out.update(state="unsupported")
        out["problems"].append("没有 DOI，当前自动核实不覆盖；不代表文献或书籍不存在，需人工核查出版社或目录记录")
        return out
    if not expected_title and not allow_doi_only:
        out["problems"].append("没有该文献的标题，无法与登记信息比对")
        return out
    records, errors, conflicts = [], [], []
    for name, fetch in (("Crossref", crossref), ("PubMed", pubmed)):
        try:
            record = fetch(doi, get)
        except Exception as exc:   # offline / API down: counts as "could not check", never as a pass
            errors.append("%s 查询失败（%s）" % (name, type(exc).__name__))
            out["checks"].append(dict(source=name, state="unavailable"))
            continue
        if record is None:
            out["problems"].append("%s 查不到该 DOI" % name)
            out["checks"].append(dict(source=name, state="not_found"))
        else:
            issues = []
            if not record.get("title"):
                issues.append("登记缺少标题，无法核实")
            elif expected_title and not title_agrees(expected_title, record["title"]):
                issues.append("登记的标题与原始引用不符：" + record["title"][:100])
            if authors:
                known = record.get("authors") or record.get("editors") or []
                missing = [a for a in authors if not any(_author_matches(a, b) for b in known)]
                if missing:
                    issues.append("引用作者未能与登记记录对应：" + "、".join(missing[:3]))
            out["checks"].append(dict(source=name, state="mismatch" if issues else "match", doi=record["doi"], title=record["title"][:2000]))
            if issues:
                conflicts.extend(name + " " + issue for issue in issues)
            else:
                records.append(record)
    out["problems"] += errors
    out["problems"] += conflicts
    if conflicts:
        out.update(state="mismatch")
    elif records:
        out.update(verified=True, state="verified", sources=[r["source"] for r in records], record=records[0])   # Crossref first: it knows the type
    elif not errors:
        out.update(state="not_found")
    return out


def verify_item(item):
    """Original import fields beat an enriched title. Always obtains new primary-source evidence; no caller pass flag."""
    original = item.get("submitted_reference")
    if isinstance(original, dict):
        title, authors = original.get("title") or "", original.get("authors") or []
        if doi_value(original.get("doi") or "") and doi_value(original["doi"]) != doi_value(item.get("doi") or ""):
            return dict(verified=False, state="mismatch", sources=[], record=None, problems=["原始引用的 DOI 与当前条目不同，请重新核对导入内容"],
                        checks=[], checked_at=now(), against="original DOI")
    else:
        title = item.get("cited_text") or ("" if item.get("title_from_metadata") else item.get("title") or "")
        authors = (item.get("authors") or []) if not item.get("title_from_metadata") else []
    result = verify(item.get("doi") or "", title, authors=authors, allow_doi_only=not bool(title))
    result.setdefault("against", "cited title" if title else "DOI only (the list gave no title)")
    return result


def form_values(record):
    """What goes into the 科研通 form for a verified record."""
    value = form_type(record.get("kind"))
    parts = []
    if record.get("authors"):
        parts.append("作者：" + ", ".join(record["authors"][:8]) + (" 等" if len(record["authors"]) > 8 else ""))
    elif record.get("editors"):
        parts.append("编者：" + ", ".join(record["editors"][:8]) + (" 等" if len(record["editors"]) > 8 else ""))
    source = record.get("container") or ""
    extra = ", ".join(str(x) for x in (record.get("publisher"), record.get("year")) if x)
    if source or extra:
        parts.append("来源：" + source + ("（%s）" % extra if extra else ""))
    return dict(doi=record["doi"], title=record["title"], url="https://doi.org/" + record["doi"], type_value=value,
                type_label=FORM_TYPES[value], note="；".join(parts)[:300])
