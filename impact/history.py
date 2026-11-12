"""Bounded public incident history, distinct from observed tenant evidence."""

import re
from datetime import datetime, timezone

from .assessment import adapter_for, list_data, parsed
from .client import ProbeError

HISTORY_LIMIT = 50
EVIDENCE_LIMIT = 10


def incident_order(item: dict) -> datetime:
    return parsed(item.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc)


def project_incidents(data: dict) -> list[dict]:
    result = []
    seen = set()
    for item in list_data(data.get("incidents")):
        identity = item.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", identity):
            raise ProbeError("invalid_response", "Status history returned an invalid incident ID")
        if identity in seen:
            continue
        seen.add(identity)
        if item.get("status") not in {"resolved", "postmortem"}:
            continue
        updates = list_data(item.get("incident_updates", []))
        components = list_data(item.get("components", []))
        assessed_components = [part["name"] for part in components
                               if isinstance(part.get("name"), str) and adapter_for(part["name"]) != "unsupported"]
        if not assessed_components:
            continue
        result.append({
            "id": identity, "name": item.get("name", "Unnamed incident"),
            "status": item["status"],
            "started_at": item.get("started_at") or item.get("created_at"),
            "resolved_at": item.get("resolved_at"), "updated_at": item.get("updated_at"),
            "url": "https://www.githubstatus.com/incidents/" + identity,
            "components": assessed_components[:100],
            "updates": sorted([{
                "at": update.get("display_at") or update.get("created_at"),
                "status": update.get("status", "unknown"), "body": update.get("body", ""),
            } for update in updates[:100]], key=lambda update: parsed(update["at"]) or datetime.min.replace(tzinfo=timezone.utc)),
            "timeline_partial": len(updates) > 100,
        })
    return sorted(result, key=incident_order, reverse=True)[:HISTORY_LIMIT]
