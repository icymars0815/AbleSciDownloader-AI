"""Is this reference real? Used when 科研通's DOI lookup comes back empty (user rule, 2026-11-10): never retry blindly and
never post a request for a reference nobody can confirm (it may be mistyped, or made up by an AI). Crossref (the DOI
registry) and PubMed are asked; only a record whose DOI and title both match counts. The confirmed record then fills the
科研通 form directly: DOI, title, official link, document type, and authors / source as "other information"."""
from urllib.error import HTTPError
from urllib.parse import quote

from ablesci.flow.core import doi_value
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
        return None
    issued = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    return dict(source="Crossref", doi=doi, title=((m.get("title") or [""])[0] or "").strip(), kind=m.get("type") or "",
                container=((m.get("container-title") or [""])[0] or "").strip(), publisher=m.get("publisher") or "",
                year=issued[0] if issued else None,
                authors=[" ".join(filter(None, [a.get("given", ""), a.get("family", "")])) or a.get("name", "") for a in m.get("author") or []][:20])


def pubmed(doi, get=None):
    """PubMed's record for the DOI (journal articles only), or None."""
    get = get or metadata.get_json
    found = get("%s/esearch.fcgi?db=pubmed&retmode=json&term=%s%%5BAID%%5D" % (EUTILS, quote(doi, safe="")))
    ids = (found.get("esearchresult") or {}).get("idlist") or []
    if len(ids) != 1:
        return None
    summary = (get("%s/esummary.fcgi?db=pubmed&retmode=json&id=%s" % (EUTILS, ids[0])).get("result") or {}).get(ids[0]) or {}
    ids_doi = [doi_value(a.get("value", "")) for a in summary.get("articleids") or [] if a.get("idtype") == "doi"]
    if doi not in ids_doi:
        return None
    return dict(source="PubMed", doi=doi, title=str(summary.get("title") or "").strip().rstrip("."), kind="journal-article",
                container=summary.get("fulljournalname") or summary.get("source") or "", publisher="",
                year=(str(summary.get("pubdate") or "")[:4] or None), authors=[a.get("name", "") for a in summary.get("authors") or []][:20],
                pmid=ids[0])


def verify(doi, expected_title, get=None):
    """dict(verified, sources, record, problems). verified only when a source has this exact DOI with a title that
    matches the paper's title (>= pp.TITLE_MIN). A network failure on both sources is "not verified", never a pass."""
    doi = doi_value(doi)
    out = dict(verified=False, sources=[], record=None, problems=[])
    if not doi:
        out["problems"].append("没有 DOI，无法核实")
        return out
    if not expected_title:
        out["problems"].append("没有该文献的标题，无法与登记信息比对")
    records, errors = [], []
    for name, fetch in (("Crossref", crossref), ("PubMed", pubmed)):
        try:
            record = fetch(doi, get)
        except Exception as exc:   # offline / API down: counts as "could not check", never as a pass
            errors.append("%s 查询失败（%s）" % (name, type(exc).__name__))
            continue
        if record is None:
            out["problems"].append("%s 查不到该 DOI" % name)
        elif expected_title and pp.title_matches(expected_title, record["title"]):
            records.append(record)
        else:
            out["problems"].append("%s 登记的标题与该文献不符：%s" % (name, record["title"][:80]))
    out["problems"] += errors
    if records:
        out.update(verified=True, sources=[r["source"] for r in records], record=records[0])   # Crossref first: it knows the type
    return out


def form_values(record):
    """What goes into the 科研通 form for a verified record."""
    value = form_type(record.get("kind"))
    parts = []
    if record.get("authors"):
        parts.append("作者：" + ", ".join(record["authors"][:8]) + (" 等" if len(record["authors"]) > 8 else ""))
    source = record.get("container") or ""
    extra = ", ".join(str(x) for x in (record.get("publisher"), record.get("year")) if x)
    if source or extra:
        parts.append("来源：" + source + ("（%s）" % extra if extra else ""))
    return dict(doi=record["doi"], title=record["title"], url="https://doi.org/" + record["doi"], type_value=value,
                type_label=FORM_TYPES[value], note="；".join(parts)[:300])
