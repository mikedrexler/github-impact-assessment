"""Deterministic incident fixtures, shared by --demo and automated tests."""

from datetime import datetime, timedelta, timezone

from .client import Config, ProbeError


class DemoClient:
    def __init__(self, config: Config, stop):
        self.config = config
        self.stop = stop
        self.requests = 0
        self.now = datetime.now(timezone.utc)

    def ago(self, minutes: int) -> str:
        return (self.now - timedelta(minutes=minutes)).isoformat()

    def status(self):
        self.requests += 1
        names = ["Actions", "Webhooks"]
        components = [{"id": name.casefold().replace(" ", "-"), "name": name,
                       "status": "degraded_performance"} for name in names]
        return {
            "page": {"updated_at": self.ago(1)}, "status": {"description": "FIXTURE: Actions and Webhooks incident"},
            "components": components,
            "incidents": [{
                "id": "demo-incident", "name": "FIXTURE: Actions queues and webhook delivery failures",
                "status": "investigating", "started_at": self.ago(40), "updated_at": self.ago(1),
                "components": components,
            }],
        }

    def get(self, path: str):
        self.requests += 1
        fixtures: dict[str, dict] = {
            "example/queue-symptom": {"has_issues": True, "has_pages": True},
            "example/self-hosted": {"has_issues": True, "has_pages": False},
            "example/limited-access": {"has_issues": False, "has_pages": False},
        }
        for metadata in fixtures.values():
            metadata["permissions"] = {"push": True}
        if path.startswith("/repos/") and path.removeprefix("/repos/") in fixtures:
            name = path.removeprefix("/repos/")
            return {"full_name": name, **fixtures[name]}
        if path.startswith("/user/repos?"):
            return [
                {"full_name": name, "owner": {"login": "example", "type": "Organization"}, **metadata}
                for name, metadata in fixtures.items()
            ]
        if path.startswith("/user/orgs?"):
            return [{"login": "example"}, {"login": "fixture-membership"}]
        if path.startswith("/orgs/"):
            organization = path.split("/")[2]
            return [{"full_name": name, **metadata} for name, metadata in fixtures.items()
                    if name.split("/")[0] == organization]
        if "/limited-access/" in path:
            raise ProbeError("permission_or_secondary_limit", "FIXTURE: Read-only endpoint returned HTTP 403")
        if "/actions/runs?" in path:
            return {"total_count": 1, "workflow_runs": [{
                "id": 101, "status": "queued", "conclusion": None,
                "created_at": self.ago(20), "updated_at": self.ago(1),
                "head_sha": "a" * 40,
            }]}
        if "/jobs?" in path:
            return {"total_count": 1, "jobs": [{
                "id": 201, "status": "queued", "created_at": self.ago(20),
                "labels": ["self-hosted"] if "/self-hosted/" in path else ["ubuntu-latest"],
            }]}
        if "/check-runs?" in path:
            return {"total_count": 0, "check_runs": []}
        if "/hooks?" in path:
            return [{"id": 301, "active": True}]
        if "/hooks/301/deliveries?" in path:
            return [{"id": 401, "delivered_at": self.ago(10), "status_code": 503}]
        raise ProbeError("not_found_or_hidden", "Unconfigured fixture endpoint")

    def past_incidents(self):
        self.requests += 1
        incident = self.status()["incidents"][0]
        return {"incidents": [
            {**incident, "status": "resolved", "resolved_at": self.ago(0),
             "incident_updates": [
                 {"created_at": self.ago(40), "status": "investigating", "body": "FIXTURE: Investigating queue delays."},
                 {"created_at": self.ago(0), "status": "resolved", "body": "FIXTURE: Incident resolved."},
             ]},
            {**incident, "id": "demo-older", "name": "FIXTURE: Earlier incident",
             "started_at": self.ago(1440), "status": "resolved", "resolved_at": self.ago(1400)},
        ]}
