"""Per-record decision trail.

Turns the analysis (groups, review items, related links, version labels) and
the user's applied choices into one plain-language row per record. The rows
feed the extra export columns and the downloadable decision log, so the reason
for keeping or removing every record survives outside the application.
"""

from __future__ import annotations

from typing import Any

from .rules import rule_name

ANNOTATION_FIELDS = [
    "publication_version",
    "publication_version_evidence",
    "duplicate_group",
    "dedup_status",
    "dedup_reason",
    "related_records",
    "review_items",
]


def _short(record: dict[str, Any] | None) -> str:
    if not record:
        return "unknown record"
    source = str(record.get("source_filename", ""))
    if record.get("source_sheet"):
        source += f"/{record['source_sheet']}"
    return f"{source} row {record.get('source_row', '?')}"


def removal_reasons(
    groups: list[dict[str, Any]],
    selections: dict[str, list[str]],
    review_items: list[dict[str, Any]],
    review_choices: dict[str, str],
    families: list[dict[str, Any]],
    family_choices: dict[str, list[str]],
    removed_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Explain why each removed record was removed."""

    reasons: dict[str, dict[str, Any]] = {}
    for number, group in enumerate(groups, start=1):
        kept = selections.get(group["group_id"], [])
        for record_id in group["record_ids"]:
            if record_id in removed_ids and record_id not in kept:
                reasons[record_id] = {
                    "via": "duplicate group",
                    "item_id": group["group_id"],
                    "item_number": number,
                    "kept_record_ids": list(kept),
                    "rules": [
                        f"{rule} {rule_name(rule)}" for rule in group.get("rules", [])
                    ],
                }
    for number, family in enumerate(families, start=1):
        excluded = set(family_choices.get(family["family_id"], []))
        if not excluded:
            continue
        kept_ids = [
            record_id
            for unit in family["units"]
            if unit["unit_id"] not in excluded
            for record_id in unit["record_ids"]
            if record_id not in removed_ids
        ]
        for unit in family["units"]:
            if unit["unit_id"] not in excluded:
                continue
            for record_id in unit["record_ids"]:
                if record_id in removed_ids:
                    reasons[record_id] = {
                        "via": "related publication family",
                        "item_id": family["family_id"],
                        "item_number": number,
                        "kept_record_ids": kept_ids,
                        "rules": [f"{rule} {rule_name(rule)}" for rule in family["rules"]],
                        "version": unit.get("label", ""),
                    }
    # review_items are review clusters; review_choices maps a cluster id to
    # the record ids the user kept.
    for number, item in enumerate(review_items, start=1):
        keep = review_choices.get(item.get("cluster_id", ""))
        if not keep:
            continue
        rules = item.get("rules") or [item.get("rule", "")]
        for record_id in item["record_ids"]:
            if record_id not in keep and record_id in removed_ids and record_id not in reasons:
                reasons[record_id] = {
                    "via": "manual review",
                    "item_id": item["cluster_id"],
                    "item_number": number,
                    "kept_record_ids": list(keep),
                    "rules": [f"{rule} {rule_name(rule)}" for rule in rules if rule],
                }
    return reasons


def build_annotations(
    records: list[dict[str, Any]],
    analysis: dict[str, Any],
    deduplication: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    by_id = {record["record_id"]: record for record in records}
    versions = analysis.get("versions") or {}
    groups = analysis.get("groups") or []
    reviews = analysis.get("review_clusters") or []
    families = analysis.get("related_families") or []
    deduplication = deduplication or {}
    removals = deduplication.get("removals", {})
    applied = deduplication is not None and "output_records" in deduplication
    selections = deduplication.get("selections", {})

    group_of: dict[str, tuple[int, dict[str, Any]]] = {}
    for number, group in enumerate(groups, start=1):
        for record_id in group["record_ids"]:
            group_of[record_id] = (number, group)
    related_of: dict[str, list[str]] = {}
    for number, family in enumerate(families, start=1):
        for unit in family["units"]:
            others = [other for other in family["units"] if other["unit_id"] != unit["unit_id"]]
            text = f"Family #{number}: " + "; ".join(
                f"{other['label'].lower()} at "
                + ", ".join(_short(by_id.get(record_id)) for record_id in other["record_ids"])
                for other in others
            )
            for record_id in unit["record_ids"]:
                related_of.setdefault(record_id, []).append(text)
    review_of: dict[str, list[str]] = {}
    for number, item in enumerate(reviews, start=1):
        for record_id in item["record_ids"]:
            review_of.setdefault(record_id, []).append(
                f"#{number} {' / '.join(item.get('labels', []))}"
            )

    annotations: dict[str, dict[str, str]] = {}
    for record in records:
        record_id = record["record_id"]
        version = versions.get(record_id, {})
        group_number, group = group_of.get(record_id, (0, None))
        if record_id in removals:
            info = removals[record_id]
            kept = "; ".join(_short(by_id.get(item)) for item in info["kept_record_ids"])
            status = "removed"
            reason = (
                f"Removed via {info['via']} #{info['item_number']} "
                f"({', '.join(info['rules'])}); kept: {kept}."
            )
        elif group is not None:
            kept_ids = selections.get(group["group_id"]) if applied else None
            rules = ", ".join(f"{rule} {rule_name(rule)}" for rule in group.get("rules", []))
            if kept_ids is None:
                status = "pending review"
                recommended = record_id == group["recommended_record_id"]
                reason = (
                    f"In duplicate group #{group_number} ({rules}); "
                    + ("recommended copy." if recommended else "not the recommended copy.")
                )
            else:
                status = "kept"
                reason = f"Kept from duplicate group #{group_number} ({rules})."
        else:
            status = "kept" if applied else "unique"
            reason = "No corroborated duplicate was found."
            if related_of.get(record_id):
                reason += " Linked to related publication(s); kept as a separate publication."
        annotations[record_id] = {
            "publication_version": version.get("label", ""),
            "publication_version_evidence": "; ".join(version.get("evidence", [])),
            "duplicate_group": str(group_number) if group_number else "",
            "dedup_status": status,
            "dedup_reason": reason,
            "related_records": " | ".join(related_of.get(record_id, [])),
            "review_items": " | ".join(review_of.get(record_id, [])),
        }
    return annotations


DECISION_LOG_FIELDS = [
    "record_id",
    "source_filename",
    "source_sheet",
    "source_row",
    "title",
    "year",
    "first_author",
    "doi",
    *ANNOTATION_FIELDS,
]


def decision_log_rows(
    records: list[dict[str, Any]], annotations: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        row = {field: record.get(field, "") for field in DECISION_LOG_FIELDS[:8]}
        row.update(annotations.get(record["record_id"], {}))
        rows.append(row)
    return rows
