"""Optional Zotero integration (off by default; the user turns it on). Two jobs, both loopback-only:
- check: before a 科研通 request, look the DOI up in the user's Zotero library (read-only local API; Zotero 7+ needs
  Settings > Advanced > "Allow other applications on this computer to communicate with Zotero").
- save: after a PDF passed the local identity check, add the item to Zotero with the connector endpoints the browser
  extension uses. Zotero 10 (ping advertises supportsAttachmentUpload) creates the item with /connector/saveItems, which
  ignores attachment URLs, and then takes the PDF bytes through /connector/saveAttachment. Zotero 7.0 has no byte upload:
  saveItems downloads attachment URLs itself, so the PDF is offered through a single-use URL on this app's own loopback
  server. Both checked against the installed omni.ja (7.0.15 and 10.0.7). Items land in the collection currently selected
  in Zotero (a connector rule) and carry the tag "KYTDL".
Nothing here runs a model, and a Zotero failure never blocks a download or a request."""
import json
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from core import doi_value, now

ZOTERO = "http://127.0.0.1:23119"
TAG = "KYTDL"
FILE_TTL = 15 * 60          # Zotero downloads the attachment shortly after saveItems returns
STATUS_TTL = 60             # cache the "is Zotero running" probe for the /api/state poll
SAVE_ATTEMPTS = 3
HEADERS = {"User-Agent": "KYTDL", "X-Zotero-Connector-API-Version": "3", "Zotero-API-Version": "3"}


class PartialSave(ValueError):
    """The item was created but its PDF was not attached: never retried (a retry would duplicate the item)."""


class ZoteroUnavailable(Exception):
    """Zotero is not running, or the local API is switched off; the caller just carries on without it."""


def _call(path, body=None, timeout=6, raw=None, headers=None):
    """GET, JSON POST (body) or raw POST (raw bytes with the given headers)."""
    if raw is not None:
        data, headers = raw, dict(HEADERS, **(headers or {}))
    else:
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(HEADERS, **({"Content-Type": "application/json"} if data is not None else {}))
    try:
        with urlopen(Request(ZOTERO + path, data=data, headers=headers, method="POST" if data is not None else "GET"), timeout=timeout) as reply:
            return reply.status, dict(reply.headers), reply.read(4_000_000)
    except HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(2000) if exc.fp else b""
    except (URLError, OSError, TimeoutError) as exc:
        raise ZoteroUnavailable("Zotero 未运行") from exc


def settings(prefs):
    prefs = prefs or {}
    on = prefs.get("zotero_enabled") is True
    collection = prefs.get("zotero_collection")
    return dict(enabled=on, check=on and prefs.get("zotero_check", True) is not False,
                save=on and prefs.get("zotero_save", True) is not False, prompted=bool(prefs.get("zotero_prompted")),
                collection=collection.strip() if isinstance(collection, str) else "")


def validate(body):
    """Validated preference changes for the optional Zotero integration (any answer also counts as "asked once")."""
    if not isinstance(body, dict) or not body or set(body) - {"enabled", "check", "save", "prompted", "collection"}:
        raise ValueError("Zotero 设置参数无效")
    if any(not isinstance(body[k], bool) for k in body if k != "collection"):
        raise ValueError("Zotero 设置参数无效")
    if "collection" in body and (not isinstance(body["collection"], str) or len(body["collection"]) > 200):
        raise ValueError("Zotero 分类名称无效")
    changes = dict(zotero_prompted=True)
    for key in ("enabled", "check", "save"):
        if key in body:
            changes["zotero_" + key] = body[key]
    if "collection" in body:
        changes["zotero_collection"] = " ".join(body["collection"].split())
    return changes


_status_cache = {}


def status(fresh=False):
    """dict(running, local_api, version). Cached for STATUS_TTL so the panel poll does not hit Zotero every few seconds."""
    if not fresh and _status_cache.get("at", 0) > time.monotonic() - STATUS_TTL:
        return dict(_status_cache["value"])
    value = dict(running=False, local_api=False, version="")
    try:
        code, headers, body = _call("/connector/ping", timeout=2)
        value["running"] = code == 200
        value["version"] = str(headers.get("X-Zotero-Version") or "")[:40]
        if value["running"]:
            code, _, _ = _call("/api/", timeout=2)
            value["local_api"] = code == 200
    except ZoteroUnavailable:
        pass
    _status_cache.update(at=time.monotonic(), value=value)
    return dict(value)


def _doi_of(data):
    found = doi_value(str(data.get("DOI") or ""))
    if found:
        return found
    for line in str(data.get("extra") or "").splitlines():   # item types without a DOI field keep it in Extra ("DOI: ...")
        if line.strip().lower().startswith("doi:"):
            return doi_value(line)
    return ""


def find(doi):
    """Look a DOI up in the user's library: dict(found, key, title, has_pdf) or raises ZoteroUnavailable.
    The quick search narrows the candidates; only an exact DOI match on the item counts."""
    doi = doi_value(doi or "")
    if not doi:
        return dict(found=False, key="", title="", has_pdf=False)
    code, _, body = _call("/api/users/0/items?" + urlencode(dict(q=doi, qmode="everything", limit=25, format="json")))
    if code == 403:
        raise ZoteroUnavailable("Zotero 本地接口未开启（设置 > 高级 > 允许其他应用程序与 Zotero 通信）")
    if code != 200:
        raise ZoteroUnavailable("Zotero 本地接口返回 HTTP %s" % code)
    rows = json.loads(body or b"[]")
    for row in rows if isinstance(rows, list) else []:
        data = row.get("data") or {}
        if data.get("itemType") in ("attachment", "note", "annotation") or _doi_of(data) != doi:
            continue
        key = str(row.get("key") or data.get("key") or "")
        return dict(found=True, key=key, title=str(data.get("title") or "")[:300], has_pdf=_has_pdf(key, row))
    return dict(found=False, key="", title="", has_pdf=False)


def _has_pdf(key, row):
    attachment = ((row.get("links") or {}).get("attachment") or {})
    if attachment.get("attachmentType") == "application/pdf":
        return True
    if not key:
        return False
    code, _, body = _call("/api/users/0/items/%s/children?format=json" % quote(key))
    if code != 200:
        return False
    children = json.loads(body or b"[]")
    return any((c.get("data") or {}).get("contentType") == "application/pdf" for c in children if isinstance(c, dict))


class FileOffers:
    """Single-use, short-lived URLs on this app's loopback server through which Zotero fetches a verified PDF."""

    def __init__(self, ttl=FILE_TTL, clock=time.monotonic):
        self.ttl, self.clock, self.lock, self.offers = ttl, clock, threading.Lock(), {}

    def offer(self, path):
        with self.lock:
            moment = self.clock()
            self.offers = {t: v for t, v in self.offers.items() if v[1] > moment}
            token = secrets.token_urlsafe(24)
            self.offers[token] = (path, moment + self.ttl)
            return token

    def take(self, token):
        with self.lock:
            entry = self.offers.pop(token, None) if isinstance(token, str) else None
        if not entry or entry[1] < self.clock():
            return None
        return entry[0]


def _creators(authors):
    out = []
    for name in (authors or [])[:100]:
        name = " ".join(str(name or "").split())
        if not name:
            continue
        if "," in name:
            last, first = [p.strip() for p in name.split(",", 1)]
        elif " " in name:
            first, last = name.rsplit(" ", 1)
        else:
            out.append(dict(lastName=name, creatorType="author", fieldMode=1))
            continue
        out.append(dict(firstName=first, lastName=last, creatorType="author"))
    return out


def zotero_item(item):
    """Translator-format JSON for /connector/saveItems, from the item's checked public record."""
    ref = item.get("metadata") or item.get("metadata_ref") or {}
    doi = doi_value(item.get("expected_doi") or item.get("doi") or "")
    title = str(ref.get("title") or item.get("expected_title") or item.get("title") or "").strip()
    data = dict(itemType="journalArticle", title=title[:1000], creators=_creators(ref.get("authors") or item.get("authors")),
                DOI=doi, url="https://doi.org/" + doi if doi else "", tags=[dict(tag=TAG)], attachments=[])
    if ref.get("year"):
        data["date"] = str(ref["year"])[:20]
    journal = ref.get("journal") or ref.get("container_title") or ref.get("venue")
    if journal:
        data["publicationTitle"] = str(journal)[:300]
    return data


def supports_upload():
    """Zotero 10+ takes attachment bytes (/connector/saveAttachment); Zotero 7.0 does not."""
    code, _, body = _call("/connector/ping", {})
    try:
        return code == 200 and bool((json.loads(body or b"{}").get("prefs") or {}).get("supportsAttachmentUpload"))
    except ValueError:
        return False


def collection_target(name):
    """treeViewID ("C12") of an existing collection with exactly this name, from the connector's target list; None if absent."""
    code, _, body = _call("/connector/getSelectedCollection", {})
    if code != 200:
        return None
    try:
        targets = json.loads(body or b"{}").get("targets") or []
    except ValueError:
        return None
    hits = [t.get("id") for t in targets if isinstance(t, dict) and str(t.get("id", "")).startswith("C") and t.get("name") == name]
    return hits[0] if len(hits) == 1 else None


def move_to(session, name):
    """Move the session's saved item into the named collection (/connector/updateSession; Zotero also shows that collection).
    Returns the collection name used, or '' when it does not exist (the item then stays where Zotero put it)."""
    target = collection_target(name) if name else None
    if not target:
        return ""
    code, _, _ = _call("/connector/updateSession", dict(sessionID=session, target=target, tags=[]), timeout=20)
    return name if code == 200 else ""


def save(item, pdf_path, offer_url=None, collection=""):
    """Create the item with its PDF in Zotero. offer_url() returns a single-use loopback URL for the PDF (Zotero 7 fallback).
    collection: name of an existing Zotero collection to move it into ('' = the collection selected in Zotero).
    Returns dict(status='sent', mode=..., collection=...) once Zotero accepted it; raises ZoteroUnavailable or ValueError."""
    data = zotero_item(item)
    if not data["title"] or not data["DOI"]:
        raise ValueError("缺少标题或 DOI，不存入 Zotero")
    data["id"] = "kytdl-" + str(item["id"])
    session = "kytdl-" + secrets.token_hex(8)
    upload = supports_upload()
    if not upload:
        if offer_url is None:
            raise ValueError("该版本 Zotero 需要通过本机链接下载附件")
        data["attachments"] = [dict(title="Full Text PDF", url=offer_url(), mimeType="application/pdf")]
    code, _, body = _call("/connector/saveItems", dict(sessionID=session, uri="", items=[data]), timeout=30)
    if code == 500 and b"libraryEditable" in (body or b""):
        raise ValueError("Zotero 当前选中的文献库只读，请在 Zotero 中选择“我的文库”或可编辑的分类")
    if code != 201:
        raise ValueError("Zotero 未接受该条目（HTTP %s）" % code)
    if not upload:
        return dict(status="sent", mode="url", collection=move_to(session, collection), time=now())
    meta = dict(sessionID=session, parentItemID=data["id"], title="Full Text PDF", url=data["url"])
    code, _, body = _call("/connector/saveAttachment?sessionID=" + quote(session), timeout=60, raw=pdf_path.read_bytes(),
                          headers={"Content-Type": "application/pdf", "X-Metadata": json.dumps(meta)})
    if code == 201:
        return dict(status="sent", mode="upload", collection=move_to(session, collection), time=now())
    if code == 200:
        raise PartialSave("Zotero 当前文献库不允许添加文件（条目已建，未附 PDF）")
    raise PartialSave("Zotero 未接受 PDF 附件（HTTP %s，条目已建，未附 PDF）" % code)


def save_candidates(items):
    """Items whose PDF passed the identity check and that were not (successfully or finally) sent to Zotero yet."""
    out = []
    for i in items:
        state = i.get("zotero") or {}
        if i.get("source") == "demo" or not i.get("pdf") or state.get("status") in ("sent", "saved", "exists", "skipped", "partial"):
            continue
        if (i.get("verification") or {}).get("status") != "identity_match" or int(state.get("attempts") or 0) >= SAVE_ATTEMPTS:
            continue
        if i.get("status") == "oa_review":
            continue
        out.append(i)
    return sorted(out, key=lambda i: i.get("created", ""))
