"""Labelled fictional controls for automatic acceptance versus AI handoff; never real publications."""
import json
from pathlib import Path

import pymupdf

from ablesci.papers.pdf_review import export_review, audit_markdown
from ablesci.papers.md_validation import software_pass

DOI = "10.1234/fictional.threshold"
TITLE = "Fictional dietary intervention with strain KLDS1.0344 in 2021 prevents chronic liver injury and improves intestinal barrier function in mice"
ABSTRACT = "This fictional software fixture compares invented dietary outcomes under controlled conditions. All samples, authors and scientific statements are artificial and serve only to test document identity checks."
META = dict(status="available", doi=DOI, title=TITLE, authors=["Alice Smith", "Bob Jones"], abstract=ABSTRACT, sources=["fictional control"])


def make_cases():
    def case(name, auto, title=TITLE, doi=DOI, authors="Alice Smith, Bob Jones", abstract=ABSTRACT, heading="", reference=None):
        text = heading + "\n" + title + "\n" + authors + ("\nDOI: " + doi if doi else "") + "\nABSTRACT\n" + abstract + "\nKeywords\nfixture"
        return dict(name=name, expected_auto=auto, text=text, metadata=reference or dict(META))
    return [case("positive_clean", True),
            case("positive_case_and_punctuation", True, title=TITLE.upper().replace(" in ", " IN ")),
            case("positive_research_label", True, title="RESEARCH ARTICLE: " + TITLE),
            case("positive_author_footnotes", True, authors="Alice Smith1, Bob Jones2"),
            case("positive_no_printed_doi_strong_bibliography", True, doi=""),
            case("negative_strain_number", False, title=TITLE.replace("KLDS1.0344", "KLDS1.0901")),
            case("negative_title_year", False, title=TITLE.replace("2021", "2022")),
            case("negative_title_negation", False, title=TITLE.replace("prevents", "does not prevent")),
            case("negative_different_doi", False, doi="10.1234/fictional.other"),
            case("negative_supporting_information", False, heading="Supporting Information"),
            case("negative_accepted_manuscript_requires_ai", False, heading="Accepted Manuscript"),
            case("negative_retracted_requires_ai", False, reference=dict(META, retraction=dict(retracted=True, updates=[]))),
            case("negative_different_abstract", False, abstract="The invented quantum optics experiment concerns laser cavities and has nothing to do with any dietary intervention. " * 3),
            case("negative_different_authors", False, authors="Eve Wrong, Mallory Other"),
            dict(name="negative_image_only_requires_ai", expected_auto=False, text="", metadata=dict(META))]


def challenge(output, thresholds=(.85, .90, .95, .98, .995)):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("挑战测试需要一个尚不存在的新目录")
    output.mkdir(parents=True)
    cases = make_cases()
    rows = []
    for n, case in enumerate(cases):
        pdf = output / (case["name"] + ".pdf")
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(40, 40, 550, 760), case["text"], fontsize=10)
            doc.save(pdf)
        exported = export_review(pdf, output, "control-%02d" % n)
        scores = []
        for threshold in thresholds:
            result = audit_markdown(exported["path"], TITLE, DOI, case["metadata"], pdf_path=pdf, title_min=threshold)
            accepted = software_pass(result, DOI)
            scores.append(dict(threshold=threshold, accepted=accepted, title_score=result["title_score"],
                               status=result["status"], blockers=result.get("review_blockers", []), reason=result["reason"]))
        rows.append(dict(name=case["name"], expected_auto=case["expected_auto"], export=exported, scores=scores))
    table = []
    for threshold in thresholds:
        tp = fp = tn = fn = 0
        for row in rows:
            accepted = next(s["accepted"] for s in row["scores"] if s["threshold"] == threshold)
            if row["expected_auto"]:
                tp += accepted
                fn += not accepted
            else:
                fp += accepted
                tn += not accepted
        table.append(dict(title_threshold=threshold, true_positive=tp, false_positive=fp, true_negative=tn, false_negative=fn))
    result = dict(label_definition="positive = safe for software automatic acceptance; negative = must hand to AI, including version/retraction cases",
                  fictional=True, cases=len(rows), thresholds=table, rows=rows)
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(ok=True, fictional_cases=len(rows), thresholds=table, results=str(output / "results.json"))
