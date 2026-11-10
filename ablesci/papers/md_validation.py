"""Offline PDF -> Markdown -> software audit batch. Targets must be supplied independently of the PDF."""
import hashlib
import json
import shutil
import time
from pathlib import Path

from ablesci.flow.pipeline import local_match_ok
from ablesci.papers.pdf_review import export_review, audit_markdown


def software_pass(verification, doi):
    return verification.get("status") == "identity_match" or local_match_ok(dict(doi=doi, verification=verification))


def validate_batch(source, references, output, engine="pymupdf", baseline=None):
    source, references, output = Path(source).resolve(), Path(references).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError("PDF 来源目录不存在")
    if output.exists() or output.is_relative_to(source):
        raise ValueError("测试输出必须是来源目录以外、尚不存在的新目录")
    inputs = json.loads(references.read_text("utf-8"))
    records = inputs.get("records", [])
    targets = {r["file"]: r for r in records}
    if len(targets) != len(records) or not records or any(not r.get("doi") or not r.get("title") for r in records):
        raise ValueError("独立书目清单需要唯一文件名、目标 DOI 和标题")
    files = sorted(source.glob("*.pdf"))
    if not files:
        raise ValueError("来源目录没有 PDF")
    output.mkdir(parents=True)
    copies = output / "sources"
    copies.mkdir()
    (output / "references.json").write_bytes(references.read_bytes())
    baseline_rows = {r["file"]: r for r in json.loads(Path(baseline).read_text("utf-8"))} if baseline else {}
    rows, controls = [], []
    for index, path in enumerate(files):
        begin = time.perf_counter()
        target = targets.get(path.name)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        row = dict(file=path.name, original_path=str(path), original_sha256=before, decision="needs_ai", conversion_ok=False)
        try:
            if target is None:
                raise ValueError("没有独立目标书目，不能用 PDF 自己提取的信息自证")
            if target.get("input_sha256") and before != target["input_sha256"]:
                raise ValueError("输入 PDF 与冻结清单不同")
            copy = copies / path.name
            shutil.copyfile(path, copy)
            exported = export_review(copy, output, "paper-%02d" % (index + 1), engine=engine, expected_sha256=before, full_document=True)
            verification = audit_markdown(exported["path"], target["title"], target["doi"], target.get("metadata"), pdf_path=copy)
            passed = software_pass(verification, target["doi"])
            row.update(conversion_ok=True, decision="pass" if passed else "needs_ai", target_doi=target["doi"],
                       target_title=target["title"], export=exported, verification=verification,
                       markdown_bytes=Path(exported["path"]).stat().st_size)
            row["decision_reason"] = ("Markdown 身份与书目核对通过" if verification["status"] == "identity_match" else
                                      "软件强证据规则通过，未交给 AI；原始初筛结果保留供追踪" if passed else verification["reason"])
            if path.name in baseline_rows:
                old = baseline_rows[path.name]
                row["baseline"] = dict(software_pass=old["software_pass"], status=old["verification"]["status"],
                                       title_score=old["verification"]["title_score"])
            wrong = records[(records.index(target) + 1) % len(records)]
            if wrong["doi"].lower() != target["doi"].lower():
                negative = audit_markdown(exported["path"], wrong["title"], wrong["doi"], wrong.get("metadata"), pdf_path=copy)
                controls.append(dict(file=path.name, kind="wrong_target", target_doi=wrong["doi"],
                                     false_accept=software_pass(negative, wrong["doi"]), status=negative["status"]))
        except Exception as exc:
            row["error"] = str(exc)[:300]
        row["original_unchanged"] = hashlib.sha256(path.read_bytes()).hexdigest() == before
        row["seconds"] = round(time.perf_counter() - begin, 3)
        rows.append(row)
        # Persist progress locally so a failed conversion never loses the batch's evidence.
        (output / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = dict(files=len(rows), converted=sum(r["conversion_ok"] for r in rows),
                   software_pass=sum(r["decision"] == "pass" for r in rows), needs_ai=sum(r["decision"] == "needs_ai" for r in rows),
                   pages=sum((r.get("export") or {}).get("page_count", 0) for r in rows),
                   originals_unchanged=all(r["original_unchanged"] for r in rows), negative_controls=len(controls),
                   negative_false_accepts=sum(c["false_accept"] for c in controls))
    result = dict(schema=1, summary=summary, input_manifest_sha256=hashlib.sha256(references.read_bytes()).hexdigest(),
                  engine=engine, audit_input="persisted_markdown_only", review_scope="identity_on_first_three_pages", rows=rows, controls=controls)
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# PDF 转 Markdown 后的软件审核实测", "", "来源：`%s`" % source,
              "", "全文转换；软件依据 Markdown 前三页进行身份核对，不解析 PDF，只对 PDF 做文件哈希校验。",
              "软件放行不等于全文完整性、公式、表格、图像或引用内容正确；参照书目是冻结的本地资料，没有联网更新。",
              "", "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```", "",
              "| 文件 | 页数 | Markdown | 软件分流 | 标题分数 | 作者 | 摘要 | 原因 |", "|---|---:|---|---|---:|---|---|---|"]
    for row in rows:
        v, e = row.get("verification") or {}, row.get("export") or {}
        b = v.get("bibliography") or {}
        md = Path(e["path"]).relative_to(output).as_posix() if e.get("path") else ""
        report.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            row["file"], e.get("page_count", ""), "[MD](%s)" % md if md else "失败", row["decision"], v.get("title_score", ""),
            b.get("author_status", ""), b.get("abstract_status", ""), (row.get("error") or row.get("decision_reason") or v.get("reason") or "").replace("|", "/").replace("\n", " ")))
    report.extend(["", "逐文件指标、SHA256、转换器、完整软件证据与错配对照见 results.json。AI 仅需处理 needs_ai 条目。"])
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return dict(ok=summary["originals_unchanged"] and summary["negative_false_accepts"] == 0 and summary["converted"] == summary["files"],
                summary=summary, report=str(output / "report.md"), results=str(output / "results.json"), output=str(output))
