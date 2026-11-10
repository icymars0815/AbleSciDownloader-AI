"""Paired local benchmark with identical extraction views, metadata and review rules."""
import hashlib
import json
import shutil
import statistics
import time
from pathlib import Path

import pymupdf

from ablesci.flow.core import embedded_dois, verify_text_evidence
from ablesci.papers.pdf_review import export_review, audit_markdown, apply_version_review
from ablesci.papers.md_validation import software_pass


def direct_review(path, record):
    """Same review rules and same three reading orders as Markdown, without serialization."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    chunks = []
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        for n in range(min(3, doc.page_count)):
            page = doc[n]
            views = dict(original=page.get_text(sort=False), sorted=page.get_text(sort=True),
                         blocks="\n".join(b[4] for b in page.get_text("blocks", sort=True) if len(b) > 6 and b[6] == 0))
            chunks.extend(dict(page=n + 1, order=order, text=views[order]) for order in ("sorted", "original", "blocks"))
        result = verify_text_evidence(chunks, record["title"], record["doi"], record.get("metadata"),
                                      sha256=digest, pages=doc.page_count, embedded_doi=(embedded_dois(doc) or [""])[0])
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("基准测试期间来源变化")
    return apply_version_review(result, chunks)


def benchmark(source, references, output, runs=3):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or not 1 <= runs <= 7:
        raise ValueError("需要来源目录以外的新输出目录，重复次数为 1–7")
    records = json.loads(Path(references).read_text("utf-8"))["records"]
    output.mkdir(parents=True)
    copies = output / "sources"
    copies.mkdir()
    for record in records:
        shutil.copyfile(source / record["file"], copies / record["file"])
    rows = []
    for index, record in enumerate(records):
        path = copies / record["file"]
        # Prime all modes and the OS cache once; warm-up is not included in timing.
        direct_review(path, record)
        exported = export_review(path, output, "bench-%02d" % index)
        audit_markdown(exported["path"], record["title"], record["doi"], record.get("metadata"), pdf_path=path)
        measurements = []
        for repeat in range(runs):
            times, decisions = {}, {}
            for mode in (("direct", "convert_audit", "cached_md") if repeat % 2 == 0 else ("cached_md", "convert_audit", "direct")):
                begin = time.perf_counter()
                if mode == "direct":
                    result = direct_review(path, record)
                else:
                    if mode == "convert_audit":
                        exported = export_review(path, output, "bench-%02d" % index)
                        converted = time.perf_counter()
                        times["conversion"] = converted - begin
                    else:
                        converted = begin
                    result = audit_markdown(exported["path"], record["title"], record["doi"], record.get("metadata"), pdf_path=path)
                    if mode == "convert_audit":
                        times["md_audit"] = time.perf_counter() - converted
                times[mode] = time.perf_counter() - begin
                decisions[mode] = dict(pass_=software_pass(result, record["doi"]), status=result["status"],
                                       blockers=sorted(result.get("review_blockers") or []))
            measurements.append(dict(times=times, decisions=decisions))
        medians = {key: statistics.median(m["times"][key] for m in measurements) for key in measurements[0]["times"]}
        equal = all(len({json.dumps(d, sort_keys=True) for d in m["decisions"].values()}) == 1 for m in measurements)
        rows.append(dict(file=record["file"], medians=medians, identical_decisions=equal, measurements=measurements))
        (output / "progress.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    totals = {key: sum(r["medians"][key] for r in rows) for key in rows[0]["medians"]}
    summary = dict(files=len(rows), repetitions=runs, totals_seconds=totals,
                   conversion_ratio=totals["convert_audit"] / totals["direct"], cached_ratio=totals["cached_md"] / totals["direct"],
                   decision_mismatches=sum(not r["identical_decisions"] for r in rows),
                   python_and_os_cache="warmed; alternating mode order", extraction_scope="first_three_pages",
                   excludes=["network metadata", "AI review time", "CLI startup", "full-document conversion"])
    (output / "results.json").write_text(json.dumps(dict(summary=summary, rows=rows), ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(ok=summary["decision_mismatches"] == 0, summary=summary, results=str(output / "results.json"))
