"""Bounded credential-visible organization hints; never changes assessed scope."""

import time
from dataclasses import dataclass, field

from .client import Client, ProbeError, organization_name


@dataclass
class OrganizationState:
    organizations: dict[str, str] = field(default_factory=dict)
    source: int = 0
    page: int = 1
    errors: list[dict] = field(default_factory=list)
    sso_partial: bool = False


def organizations(client: Client, state: OrganizationState, *, deadline: float, request_limit: int) -> dict:
    started = client.requests
    reason = None
    while state.source < 2:
        if time.monotonic() >= deadline or client.requests - started >= request_limit:
            reason = {"kind": "checkpoint", "message": "Organization loading will continue within the shared rate budget."}
            break
        prefix = "/user/orgs?" if state.source == 0 else "/user/repos?affiliation=owner,collaborator,organization_member&sort=full_name&"
        try:
            rows = client.get(f"{prefix}per_page=100&page={state.page}")
            if not isinstance(rows, list) or len(rows) > 100:
                raise ProbeError("invalid_response", "Organization discovery returned an invalid page.")
            names = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ProbeError("invalid_response", "Organization discovery returned an invalid entry.")
                owner = row if state.source == 0 else row.get("owner")
                if state.source == 1:
                    if not isinstance(owner, dict) or not isinstance(owner.get("type"), str):
                        raise ProbeError("invalid_response", "Repository owner metadata is missing; organization coverage is partial.")
                    if owner["type"] != "Organization":
                        continue
                try:
                    name = organization_name(owner.get("login") if isinstance(owner, dict) else None)
                except ValueError:
                    raise ProbeError("invalid_response", "Organization discovery returned an invalid organization login.") from None
                names.append(name)
            for name in names:
                if name not in state.organizations and len(state.organizations) >= client.config.max_repositories:
                    raise ProbeError("organization_limit", "Organization capacity reached; results are partial.")
                state.organizations[name] = name
            state.sso_partial |= bool(getattr(client, "access_warnings", set()))
        except ProbeError as error:
            reason = {"kind": error.kind, "message": error.message}
            if error.kind in {"not_found_or_hidden", "permission_or_secondary_limit"}:
                state.errors.append({"source": "memberships" if state.source == 0 else "repository owners", **reason})
                state.source += 1
                state.page = 1
                continue
            break
        if len(rows) < 100:
            state.source += 1
            state.page = 1
        elif state.page >= 1000:
            reason = {"kind": "organization_limit", "message": "Organization page limit reached; results are partial."}
            break
        else:
            state.page += 1
    return {
        "organizations": sorted(state.organizations),
        "complete": state.source == 2 and not state.errors and not state.sso_partial,
        "finished": state.source == 2 or (reason or {}).get("kind") == "organization_limit",
        "reason": reason, "errors": list(state.errors), "sso_partial": state.sso_partial,
    }
