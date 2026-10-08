"""Auditable imports of public decision data, without invented safety labels."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re

from jsonschema.exceptions import SchemaError

from .datasets import bfcl_import_records, write_suite
from .experiments import write_json
from .splits import create_split, leakage_groups
from .types import digest, json_text, strict_json

BFCL_REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
DATA_PATH = "berkeley-function-call-leaderboard/bfcl_eval/data/"


def verified_sources(raw):
    raw = Path(raw)
    manifest = strict_json((raw / "sources.json").read_text())
    if manifest["revision"] != BFCL_REVISION:
        raise ValueError("Unexpected BFCL revision")
    required = {"LICENSE", DATA_PATH + "README.md"} | {
        DATA_PATH + prefix + f"BFCL_v4_live_{category}.json"
        for category in ("simple", "multiple", "irrelevance")
        for prefix in (("",) if category == "irrelevance" else ("", "possible_answer/"))}
    if {r["path"] for r in manifest["files"]} != required or len(manifest["files"]) != len(required):
        raise ValueError("Missing or duplicated pinned BFCL sources")
    for record in manifest["files"]:
        expected_url = f"https://raw.githubusercontent.com/ShishirPatil/gorilla/{BFCL_REVISION}/" + record["path"]
        content = (raw / record["path"]).read_bytes()
        if (record["url"] != expected_url or len(content) != record["bytes"]
                or sha256(content).hexdigest() != record["sha256"]):
            raise ValueError("BFCL source hash/URL mismatch: " + record["path"])
    return manifest


def import_live_records(questions, answers, category):
    """Keep every supported official item; retain explicit exclusions before model runs."""
    if category not in {"simple", "multiple", "irrelevance"}:
        raise ValueError("Unsupported BFCL Live category")
    if len({q["id"] for q in questions}) != len(questions):
        raise ValueError("Duplicate source question IDs")
    answer_index = {a["id"]: a for a in answers}
    if len(answer_index) != len(answers):
        raise ValueError("Duplicate source answer IDs")
    if category != "irrelevance" and set(answer_index) != {q["id"] for q in questions}:
        raise ValueError("Native question/answer ID sets differ")
    tasks, excluded = [], []
    for original in questions:
        question = deepcopy(original)
        if not question["id"].startswith(f"live_{category}_"):
            raise ValueError("Question ID/category mismatch")
        conversions = []
        if category == "simple" and isinstance(question.get("function"), dict):
            question["function"] = [question["function"]]
            conversions.append("single_function_object_wrapped_as_list")
        try:
            row = bfcl_import_records([question], None if category == "irrelevance" else [answer_index[question["id"]]],
                                      source_revision=BFCL_REVISION, irrelevant=category == "irrelevance")[0]
        except (ValueError, KeyError, TypeError, SyntaxError, SchemaError) as exc:
            excluded.append({"id": question["id"], "category": category, "reason": str(exc),
                             "source_question_sha256": digest(original)})
            continue
        # Exact shared tool documentation is a conservative leakage link, not proof of iid sampling.
        signature = digest(sorted(question["function"], key=json_text))
        block = re.fullmatch(r"live_(simple|multiple|irrelevance)_[0-9]+-([0-9]+)-[0-9]+", question["id"])
        if block:
            # Conservatively keep apparent native ID variants together even when their tool docs differ.
            row["family"] = "native-id-block:" + block[1] + ":" + block[2]
        row["metadata"].update(
            public_dataset="bfcl-live", category=category, template_id="native-tool-menu-" + signature,
            id_block_grouping="conservative_heuristic_not_author_independence_label" if block else "not_applicable",
            source_question_sha256=digest(original),
            source_answer_sha256=digest(answer_index[question["id"]]) if category != "irrelevance" else None,
            import_conversions=conversions, safety_labels="unavailable", effect_labels="unavailable",
            scope="function choice only; effect=read is a legacy adapter placeholder, NOT business safety")
        tasks.append(row)
    return tasks, excluded


def prepare_live(raw, output):
    raw, output = Path(raw), Path(output)
    sources = verified_sources(raw)
    tasks, exclusions, counts = [], [], {}
    def rows(path):
        return [strict_json(line) for line in path.read_text().splitlines() if line.strip()]
    for category in ("simple", "multiple", "irrelevance"):
        name = f"BFCL_v4_live_{category}.json"
        questions = rows(raw / DATA_PATH / name)
        answers = [] if category == "irrelevance" else rows(raw / DATA_PATH / "possible_answer" / name)
        imported, excluded = import_live_records(questions, answers, category)
        tasks.extend(imported)
        exclusions.extend(excluded)
        counts[category] = {"source": len(questions), "imported": len(imported), "excluded": len(excluded)}
    groups, grouping = leakage_groups(tasks)
    output.mkdir(parents=True, exist_ok=False)
    write_suite(output / "suite.jsonl", tasks)
    split = create_split(output / "suite.jsonl", output / "split", ratios=(.2, .4, .4), seed=20260929)
    report = {"source_revision": BFCL_REVISION, "source_manifest_sha256": digest(sources),
              "categories": counts, "tasks": len(tasks), "structural_groups": len(groups),
              "candidate_counts": dict(sorted(Counter(len(t["public"]["actions"]) for t in tasks).items())),
              "split": split, "exclusions": exclusions, "model_calls": 0,
              "scope": "Public author-labeled function choice; not full BFCL argument evaluation",
              "independence_established": False, "safety_labels_available": False,
              "warnings": grouping["warnings"] + [
                  "Native ID blocks, shared tool menus and identical requests are grouped; semantic near-duplicates remain unreviewed.",
                  "This convenience benchmark is not an iid deployment sample or proof of low business risk.",
                  "Do not mix this split with earlier BFCL splits: cross-package isolation is not established."]}
    write_json(output / "audit.json", report)
    with (output / "review.jsonl").open("x") as stream:
        for group, ids in sorted(groups.items()):
            stream.write(json_text({"group": group, "task_ids": ids, "reviewer": None,
                                   "semantic_cluster": None, "status": "needs_independent_review"}) + "\n")
    return report
