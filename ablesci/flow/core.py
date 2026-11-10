"""Local queue and PDF identity checks. No website writes or accept/reject requests."""
import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pymupdf

from ablesci.system.applog import AppLog

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"\u201c\u201d]+", re.I)


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize(text):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def normalize_author(text):
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) != "Mn")
    return re.sub(r"(?<=[a-z])\d+\b", "", normalize(text))


def doi_value(text):
    """First DOI in text, lower-case. Trailing . , ; and an unmatched ')' are stripped, balanced parentheses stay
    (10.1016/S0140-6736(20)30183-5 survives; '(doi:10.1000/x)' loses only the closing bracket)."""
    match = DOI_RE.search(text or "")
    if not match:
        return ""
    value = match.group()
    while value:
        if value[-1] in ".,;" or (value[-1] == ")" and value.count(")") > value.count("(")):
            value = value[:-1]
        else:
            break
    return value.lower()


RETRACTION_TYPES = ("retraction", "withdrawal", "removal", "expression_of_concern", "expression-of-concern")
RETRACTION_REASON = "该文献已被撤稿或有更正通知，请人工确认"


def embedded_dois(doc):
    """DOIs found in the PDF document metadata (Info dictionary) and XMP packet. Auxiliary evidence only:
    these fields are written by publishers or tools and may be stale, so they never confirm identity on their own."""
    texts = []
    try:
        texts.extend(str(v) for v in (doc.metadata or {}).values() if v)
    except Exception:
        pass
    try:
        texts.append(str(doc.get_xml_metadata() or ""))
    except Exception:
        pass
    found = []
    for text in texts:
        for m in DOI_RE.finditer(text[:200000]):
            value = doi_value(m.group())
            if value and value not in found:
                found.append(value)
    return found


def is_retracted(metadata):
    """True only when the public record says so; missing data is None, never retracted."""
    info = (metadata or {}).get("retraction")
    if not isinstance(info, dict):
        return None
    if info.get("retracted") is True:
        return True
    for update in info.get("updates") or []:
        if isinstance(update, dict) and str(update.get("type", "")).lower().replace(" ", "_") in RETRACTION_TYPES and update.get("role", "paper") == "paper":
            return True
    return info.get("retracted")


def site_url(url, path):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.netloc == "www.ablesci.com"
            and parsed.path == path and bool(re.fullmatch(r"[A-Za-z0-9]+", parse_qs(parsed.query).get("id", [""])[0])))


def checkin_result(result):
    if result.get("code") == 0:
        return "签到成功"
    message = str(result.get("msg", ""))
    if "您今天已于" in message or "今日已签到" in message or "今天已签到" in message:
        return "今日已签到"
    raise ValueError("签到未成功，请在科研通页面检查登录和账户状态。未自动重试")


ABSTRACT_HEAD_RE = re.compile(
    r"(?i)(?:\bA\s*B\s*S\s*T\s*R\s*A\s*C\s*T\b|摘要|"
    r"(?:(?<=\n)|^)[ \t]*\b(?:summary|scope)\b|\b(?:summary|scope)\b(?=\s*[:：]))\s*[:：]?\s*")
ABSTRACT_STOP_RE = re.compile(
    r"(?im)(?:^|\n)\s*(?:key\s*words?|关键词|(?:1[.\s]*)?introduction|references|参考文献)\b")


def _abstract_score(ref, texts):
    """Best similarity of the reference abstract to heading-based and heading-free candidates."""
    best, method, excerpt, heading_found = 0.0, "", "", False
    cap = len(ref) + 150
    for text in texts:
        for m in ABSTRACT_HEAD_RE.finditer(text):
            candidate = ABSTRACT_STOP_RE.split(text[m.end():], maxsplit=1)[0][:14000]
            observed = normalize(candidate)
            if len(observed) < 80:
                continue
            heading_found = True
            score = SequenceMatcher(None, ref[:12000], observed[:cap][:12000], autojunk=False).ratio()
            if score > best or not method:
                best, method, excerpt = max(score, best), "heading", candidate
    for text in texts:
        norm = normalize(text)
        if len(norm) < 80:
            continue
        windows = [norm[i:i + cap] for i in range(0, max(1, len(norm) - cap + 60), 60)]
        matcher = SequenceMatcher(None, autojunk=False)
        matcher.set_seq2(ref[:12000])
        quick = []
        for w in windows:
            matcher.set_seq1(w)
            quick.append((matcher.quick_ratio(), w))
        for _, w in sorted(quick, key=lambda x: -x[0])[:3]:
            matcher.set_seq1(w)
            # Re-cut the window around where the reference actually aligns (coarse step leaves slack).
            blocks = [b for b in matcher.get_matching_blocks() if b.size >= 8]
            if blocks:
                start = max(0, blocks[0].a - blocks[0].b)
                w = w[start:start + len(ref) + 30]
                matcher.set_seq1(w)
            score = matcher.ratio()
            if score > best or not method:
                best, method, excerpt = max(score, best), "window", w
    if not method:
        return None, "", "", heading_found
    return best, method, excerpt, heading_found


def title_evidence(texts, title, detail=False):
    """Compare contiguous lines in independent PDF reading orders; keep the threshold unchanged."""
    target = normalize(title)
    best, order, observed = 0.0, "", ""
    for name, text in texts:
        lines = [normalize(line) for line in text.splitlines() if line.strip()][:100]
        for i in range(len(lines)):
            for n in range(1, 7):
                window = " ".join(lines[i:i+n])
                score = SequenceMatcher(None, target, window, autojunk=False).ratio() if target else 0
                if score > best:
                    best, order, observed = score, name, window
                    if best == 1:
                        return (best, order, observed) if detail else (best, order)
    return (best, order, observed) if detail else (best, order)


def critical_title_difference(expected, observed):
    target, seen = normalize(expected), normalize(observed)
    numbers, observed_numbers = sorted(re.findall(r"\d+", target)), sorted(re.findall(r"\d+", seen))
    negation, observed_negation = sorted(set(target.split()) & {"not", "without"}), sorted(set(seen.split()) & {"not", "without"})
    return dict(conflict=bool(numbers and numbers != observed_numbers) or negation != observed_negation,
                expected_numbers=numbers, observed_numbers=observed_numbers,
                expected_negation=negation, observed_negation=observed_negation)


def document_kind(text):
    """Only standalone headings near the start indicate a supplement, never a body link to one."""
    lines = [line.strip() for line in text.splitlines() if line.strip()][:16]
    heading = re.compile(r"(?:electronic\s+)?(?:supporting\s+information|supplementary\s+(?:information|material[s]?|data)|supplemental\s+(?:information|material[s]?|data))(?:\s+(?:for|to))?\s*[:.：]?", re.I)
    for n, line in enumerate(lines):
        if heading.fullmatch(line):
            # ACS places a standalone "Supporting Information" in its Read Online navigation strip.
            context = " ".join(lines[max(0, n - 6):n + 2]).lower()
            if any(label in context for label in ("read online", "metrics & more", "article recommendations")):
                continue
            return "supplement"
    return "unknown"


def review_blockers(verification):
    """Reasons that identity similarity must never override during automatic completion."""
    v = verification or {}
    reasons = list(v.get("review_blockers") or [])
    if is_retracted(v) is True and "retracted" not in reasons:
        reasons.append("retracted")
    if v.get("document_kind") == "supplement" and "supplement" not in reasons:
        reasons.append("supplement")
    return reasons


def compare_metadata(front, early_text, expected_doi, metadata, front_variants=None, title_min=.90):
    """Conservative local string comparisons; missing data are never a mismatch."""
    checks = dict(status="unverified", sources=metadata.get("sources", []),
                  metadata_status=metadata.get("status"), fetched_at=metadata.get("fetched_at", ""),
                  title_status="unverified", author_status="unverified", abstract_status="unverified",
                  reference_title=metadata.get("title", ""), reference_authors=metadata.get("authors", []),
                  reference_abstract=metadata.get("abstract", ""), field_sources=metadata.get("field_sources", {}),
                  errors=metadata.get("errors", []))
    if metadata.get("status") != "available" or doi_value(metadata.get("doi", "")) != doi_value(expected_doi):
        checks["reason"] = metadata.get("reason", "参考记录 DOI 不一致，作者和摘要未核实")
        return checks
    title = normalize(metadata.get("title", ""))
    variants = front_variants or [("sorted", front)]
    score, order = title_evidence(variants, title)
    checks["title_score"] = round(score * 100, 1)
    checks["title_text_order"] = order
    if title:
        checks["title_status"] = "match" if score >= title_min else "difference" if score < .65 else "review"
    header = "\n".join(re.split(r"(?i)\babstract\b|摘要|\bintroduction\b", text, maxsplit=1)[0][:5000] for _, text in variants)
    header_lines = [normalize_author(line) for line in header.splitlines() if line.strip()]
    matched, missing = [], []
    for name in metadata.get("authors", []):
        tokens = normalize_author(name).split()
        # Require a full name or a surname plus given-name initial on the SAME line.
        found = len(tokens) >= 2 and any(
            normalize_author(name) in line or
            (tokens[-1] in line.split() and any(t.startswith(tokens[0][0]) and t != tokens[-1] for t in line.split()))
            for line in header_lines)
        (matched if found else missing).append(name)
    checks.update(matched_authors=matched, missing_authors=missing)
    if metadata.get("authors"):
        checks["author_status"] = "match" if not missing else "partial" if matched else "unverified"
    # Candidates come from several text orders; a heading is optional (see _abstract_score).
    texts = [early_text] if isinstance(early_text, str) else [t for t in early_text if t]
    ref = normalize(metadata.get("abstract", ""))
    excerpt, method, heading_found, best = "", "", False, None
    if len(ref) >= 80:
        best, method, excerpt, heading_found = _abstract_score(ref, texts)
    checks["pdf_abstract_excerpt"] = excerpt[:1500]
    if best is not None:
        checks["abstract_score"] = round(best * 100, 1)
        checks["abstract_method"] = method
        if best >= .90:
            checks["abstract_status"] = "match"
        elif best >= .65:
            checks["abstract_status"] = "review"
        elif heading_found:
            checks["abstract_status"] = "difference"
        # No heading and a poor window score is a layout failure: stays "unverified", never "difference".
    if len(ref) < 80:
        checks["abstract_status"] = "unavailable"     # publisher gives no public abstract (common for Elsevier)
    elif len(ref) < 250:
        for text in texts:
            for match in ABSTRACT_HEAD_RE.finditer(text):
                observed = normalize(ABSTRACT_STOP_RE.split(text[match.end():], maxsplit=1)[0][:14000])
                if len(observed) > 3 * len(ref) and ref in observed:
                    checks.update(abstract_status="reference_fragment", abstract_score=None,
                                  reference_abstract_quality="partial", abstract_partial_match=True)
                    break
    identity = checks["title_status"] == "match" and checks["author_status"] == "match"
    if all(checks[k] == "match" for k in ("title_status", "author_status", "abstract_status")):
        checks.update(status="match", reason="公开标题、作者名单及摘要均通过本地文字比对")
    elif identity and checks["abstract_status"] == "unavailable":
        checks.update(status="match", reason="公开标题与作者名单均通过本地文字比对；公开记录未提供摘要，未比对摘要")
    elif identity and checks["abstract_status"] == "review":
        checks.update(status="match", reason="公开标题与作者名单均通过本地文字比对；摘要基本一致（%s%%）" % checks.get("abstract_score"))
    elif "difference" in (checks["title_status"], checks["abstract_status"]):
        checks.update(status="difference", reason="公开记录与 PDF 文字疑似存在差异，请人工复核")
    else:
        checks.update(status="unverified", reason="作者或摘要未全部核实；缺失信息不视为错误")
        if checks["abstract_status"] == "reference_fragment":
            checks["reason"] = "公开摘要参照仅为片段，该片段吻合；完整摘要尚未核实"
    return checks


def verify_text_evidence(chunks, expected_title, expected_doi, metadata=None, sha256=None, pages=0, embedded_doi="", title_min=.90):
    """Pure identity review of observed page text; never opens a PDF or uses the target as observed evidence."""
    result = dict(status="review", title_score=0, primary_dois=[], reason="待 AI 复核", pages=pages,
                  sha256=sha256, embedded_doi=embedded_doi)
    first = [(c["order"], c["text"]) for c in chunks if c["page"] == 1]
    if not first:
        return dict(result, review_blockers=["missing_first_page"], reason="缺少首页文字，需 AI 看原图")
    if max(len(re.sub(r"\s", "", text)) for _, text in first) < 50:
        return dict(result, status="ocr_required", reason="首页文字不足，可能是扫描件；需要 OCR 或 AI 看原图")
    stop = r"(?im)^\s*(?:references|bibliography|参考文献)\s*$"
    variants = [(order, re.split(stop, text)[0]) for order, text in first]
    score, order, observed_title = title_evidence(variants, expected_title, detail=True)
    primary = sorted({doi_value(m.group()) for _, text in variants for m in DOI_RE.finditer(text)})
    doi = doi_value(expected_doi)
    front = next((text for name, text in variants if name == "original"), variants[0][1])
    result.update(title_score=round(score * 100, 1), title_text_order=order, primary_dois=primary,
                  document_kind=document_kind(front), excerpt=front[:1200])
    critical = critical_title_difference(expected_title, observed_title)
    result["title_critical_tokens"] = critical
    if critical["conflict"]:
        result["review_blockers"] = ["title_critical_difference"]
    if doi and doi in primary and score >= title_min:
        result.update(status="identity_match", reason="首页 DOI 与标题均匹配；完整性和版本仍需核对")
    elif doi and primary and doi not in primary:
        result.update(status="conflict", reason="首页检测到其他 DOI，与目标 DOI 不一致；请 AI 复核")
    elif score < .65 and normalize(expected_title):
        result.update(status="conflict", reason="首页标题与目标差异较大；请 AI 复核，不自动驳回")
    elif not doi:
        result["reason"] = "缺少目标 DOI；仅凭标题不自动确认"
    else:
        result["reason"] = "首页未同时满足 DOI 与标题匹配；请 AI 复核"
    if metadata is not None:
        orders = list(dict.fromkeys(c["order"] for c in chunks))
        early = [re.split(stop, "\n".join(c["text"] for c in chunks if c["order"] == name and c["page"] <= 3))[0]
                 for name in orders]
        checks = compare_metadata(front, early, expected_doi, metadata, front_variants=variants, title_min=title_min)
        result["bibliography"] = checks
        if checks.get("reference_abstract_quality") == "partial":
            result["reason"] += "；" + checks["reason"]
        if result["status"] == "identity_match":
            if checks["status"] == "match":
                result["reason"] = ("DOI、标题、作者与摘要均匹配" if checks["abstract_status"] == "match" else
                                    "DOI、标题、作者均匹配（" + checks["reason"].split("；", 1)[-1] + "）") + "；完整性和版本仍需核对"
            else:
                result.update(status="review", reason="DOI 和标题初筛匹配；" + checks["reason"])
        result["retraction"] = metadata.get("retraction") if isinstance(metadata.get("retraction"), dict) else dict(retracted=None, updates=[], source=None)
        if is_retracted(metadata) is True:
            if result["status"] == "identity_match":
                result["status"] = "review"
            result["reason"] = RETRACTION_REASON + "；" + result["reason"]
    blockers = review_blockers(result)
    if blockers:
        result["review_blockers"] = blockers
        if result["status"] == "identity_match":
            result["status"] = "review"
        if "supplement" in blockers:
            result["reason"] = "首页标注为补充材料，不能作为正文自动完成；" + result["reason"]
        if "title_critical_difference" in blockers:
            result["reason"] = "标题关键数字、编号或否定词不一致，不能凭总相似度放行；" + result["reason"]
    return result


def verify_pdf(path, expected_title, expected_doi, metadata=None):
    """Identity screening only: does not certify missing pages or publication version."""
    result = {"status": "review", "title_score": 0, "primary_dois": [], "reason": "待人工复核"}
    try:   # always record the file's hash (also for scans / unreadable files): adopt and reject check that the file is unchanged
        result["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        pass
    try:
        with pymupdf.open(path) as doc:
            if not doc.is_pdf or doc.needs_pass or doc.page_count < 1:
                raise ValueError("文件不是可读的未加密 PDF")
            result["pages"] = doc.page_count
            result["embedded_doi"] = (embedded_dois(doc) or [""])[0]
            first = doc[0].get_text(sort=True)
            if len(re.sub(r"\s", "", first)) < 50:
                result.update(status="ocr_required", reason="首页文字不足，可能是扫描件；需要 OCR 或人工核对")
                return result
            # Never accept a target DOI found only in the reference list.
            front = re.split(r"(?im)^\s*(?:references|bibliography|参考文献)\s*$", first)[0]
            target = normalize(expected_title)
            raw_front = re.split(r"(?im)^\s*(?:references|bibliography|参考文献)\s*$", doc[0].get_text(sort=False))[0]
            variants = [("sorted", front), ("original", raw_front)]
            score, order = title_evidence(variants, expected_title)
            primary = sorted({doi_value(m.group()) for m in DOI_RE.finditer(front)})
            doi = doi_value(expected_doi)
            result.update(title_score=round(score * 100, 1), primary_dois=primary,
                          title_text_order=order, document_kind=document_kind(raw_front),
                          excerpt=front[:1200], sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
            if doi and doi in primary and score >= .90:
                result.update(status="identity_match", reason="首页 DOI 与标题均匹配；完整性和版本仍需核对")
            elif doi and primary and doi not in primary:
                result.update(status="conflict", reason="首页检测到其他 DOI，与目标 DOI 不一致；请人工复核")
            elif score < .65 and target:
                result.update(status="conflict", reason="首页标题与目标差异较大，请人工复核；不自动驳回")
            elif not doi:
                result["reason"] = "缺少目标 DOI；仅凭标题不自动确认"
            else:
                result["reason"] = "首页未同时满足 DOI 与标题匹配；请人工复核"
            if metadata is not None:
                stop = r"(?im)^\s*(?:references|bibliography|参考文献)\s*$"
                early = [re.split(stop, "\n".join(doc[i].get_text(**kw) for i in range(min(3, doc.page_count))))[0]
                         for kw in ({"sort": True}, {})]
                checks = compare_metadata(front, early, expected_doi, metadata, front_variants=variants)
                result["bibliography"] = checks
                if result["status"] == "identity_match":
                    if checks["status"] == "match":
                        result["reason"] = ("DOI、标题、作者与摘要均匹配" if checks["abstract_status"] == "match"
                                            else "DOI、标题、作者均匹配（" + checks["reason"].split("；", 1)[-1] + "）") + "；完整性和版本仍需核对"
                    else:
                        result.update(status="review", reason="DOI 和标题初筛匹配；" + checks["reason"])
                retraction = metadata.get("retraction") if isinstance(metadata.get("retraction"), dict) else dict(retracted=None, updates=[], source=None)
                result["retraction"] = retraction
                if is_retracted(metadata) is True and result["status"] == "identity_match":
                    result.update(status="review", reason=RETRACTION_REASON)
                elif is_retracted(metadata) is True:
                    result["reason"] = RETRACTION_REASON + "；" + result["reason"]
            blockers = review_blockers(result)
            if blockers:
                result["review_blockers"] = blockers
                if result["status"] == "identity_match":
                    result["status"] = "review"
                if "supplement" in blockers:
                    result["reason"] = "首页标注为补充材料，不能作为正文自动完成；" + result["reason"]
    except Exception:
        result.update(status="invalid", reason="PDF 无法完整解析或需要密码；请先重新下载并人工检查")
    return result


def write_json_durable(path, data):
    """Write JSON atomically and durably: tmp file + flush + fsync + replace. The previous file, if it still parses,
    is first copied to <name>.bak (the last known good version)."""
    path = Path(path)
    temp = path.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    with temp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        try:
            json.loads(path.read_text("utf-8"))
            shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        except (OSError, ValueError):
            pass   # current file is damaged: keep the older .bak instead of overwriting it with garbage
    temp.replace(path)


def read_json_with_backup(path):
    """-> (data, recovered). Falls back to <name>.bak when the main file does not parse; raises only if both are unusable."""
    path = Path(path)
    try:
        return json.loads(path.read_text("utf-8")), False
    except ValueError as first:
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            return json.loads(backup.read_text("utf-8")), True
        except (OSError, ValueError):
            raise first


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.log = AppLog(self.root / "logs")
        self.file = self.root / "queue.json"
        recovered = False
        if self.file.exists():
            self.data, recovered = read_json_with_backup(self.file)
        else:
            self.data = {"items": [], "events": []}
        self.data.setdefault("items", [])
        self.data.setdefault("events", [])
        dirty = recovered
        for item in self.data["items"]:
            status = item.get("status")
            if status == "downloading":
                item.update(status="attention", note="上次服务在下载中退出；请检查本地文件后手动重试")
                dirty = True
            elif status == "publishing" or any((item.get(k) or {}).get("status") == "attempted" for k in ("publish_action", "adopt_action", "reject_action")):
                # Never auto-retry an action that was in flight: the site may or may not have accepted it.
                for key in ("publish_action", "adopt_action", "reject_action"):
                    if (item.get(key) or {}).get("status") == "attempted":
                        item[key] = dict(item[key], status="uncertain")
                item.update(status="attention", note="上次服务在操作科研通的过程中退出，结果不确定。请在科研通核实后处理")
                dirty = True
            elif status is None:
                item["status"] = "ready"
                dirty = True
        if dirty:
            self.save()
        if recovered:
            msg = "queue.json 无法读取，已从上一份完好的备份（queue.json.bak）恢复，最近的改动可能丢失，请核对。"
            self.log.write("store", "load", "warn", message=msg)
            self.event(msg, kind="needs_action", action="pin")

    def save(self):
        with self.lock:
            write_json_durable(self.file, self.data)

    def mutate(self, change):
        """Run change(data) under the store lock and save: the only way for other modules to edit store.data directly."""
        with self.lock:
            change(self.data)
            self.save()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def get(self, item_id):
        with self.lock:
            return next((dict(i) for i in self.data["items"] if i["id"] == item_id), None)

    def add(self, title="", doi="", source="import", **fields):
        with self.lock:
            doi = doi_value(doi)
            for item in self.data["items"]:
                # An imported DOI that this account already requested on 科研通 (a published import becomes a website
                # item) is the same paper: re-importing it must never start a second paid request (real 2026-11-09).
                same_requested = source == "import" and item["source"] == "website" and bool(doi) and doi == item["doi"]
                if source != item["source"] and not same_requested:
                    continue
                if source != "demo" and fields.get("account_id", "default") != item.get("account_id", "default"):
                    continue
                if fields.get("assist_url") and fields["assist_url"] == item.get("assist_url"):
                    return item["id"]
                if doi and doi == item["doi"]:
                    return item["id"]
                # Same title is a duplicate unless both sides carry different DOIs (e.g. a paper and its correction).
                if title and normalize(title) == normalize(item["title"]) and not (doi and item["doi"] and doi != item["doi"]):
                    return item["id"]
            item = dict(id=uuid.uuid4().hex[:12], title=title.strip()[:1000], doi=doi,
                        source=source, status="ready", created=now(), verification=None)
            item.update(fields)
            self.data["items"].append(item)
            self.save()
            return item["id"]

    def update(self, item_id, **fields):
        with self.lock:
            for item in self.data["items"]:
                if item["id"] == item_id:
                    item.update(fields, updated=now())
                    self.save()
                    return
            raise ValueError("未找到任务")

    def event(self, message, **meta):
        """meta (optional): kind, batch_id, item_id, action - drives the message panel buttons."""
        with self.lock:
            entry = {"time": now(), "message": message}
            entry.update({k: v for k, v in meta.items() if v not in (None, "")})
            self.data["events"].append(entry)
            self.data["events"] = self.data["events"][-150:]
            self.save()

    def update_account(self, account_id="default", **fields):
        with self.lock:
            self.data.setdefault("account", {}).update(fields)
            self.data.setdefault("account_metrics", {}).setdefault(account_id, {}).update(fields)
            self.save()

    def update_preferences(self, **fields):
        with self.lock:
            self.data.setdefault("preferences", {}).update(fields)
            self.save()


def seed_demo(store):
    folder = store.root / "examples"
    folder.mkdir(exist_ok=True)
    for name, title, doi, body in [
        ("matching", "Demonstration article: Citrus flavonoids and gut microbiota", "10.0000/demo.match.001", "Synthetic PDF for software demonstration only. Not a published paper.\nThis file contains no research data."),
        ("wrong", "Demonstration article: Polymer coatings and battery materials", "10.0000/demo.other.002", "Synthetic PDF for software demonstration only. Not a published paper."),
        ("reference", "Demonstration article: A different publication", "10.0000/demo.other.003", "Synthetic PDF for software demonstration only.\nReferences\n10.0000/demo.match.001"),
    ]:
        path = folder / (name + ".pdf")
        if not path.exists():
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_textbox((50, 50, 540, 300), title + "\n" + doi + "\n\n" + body, fontsize=12)
            doc.save(path)
            doc.close()
        store.add("Demonstration article: Citrus flavonoids and gut microbiota" + ("" if name == "matching" else " [" + name + "]"),
                  "", source="demo", demo_name=name, expected_title="Demonstration article: Citrus flavonoids and gut microbiota",
                  expected_doi="10.0000/demo.match.001", pdf=str(path.relative_to(store.root)))
    store.event("已载入 3 条演示数据：匹配文件、错误文献、仅参考文献含目标 DOI。未访问科研通。")
