"""Phase 3 write tools: classification (taxonomy) categories and assignments.

Same rules as the other write tools: staged only, additive only, exact identification. A
taxonomy is matched by id or full name; a classification by id, full name (when unique) or path
such as "Developed markets » Europe" ("/" and ">" work as separators too). Weights are percent:
one security or account may be spread over several classifications of a taxonomy, but its
weights there never add up to more than 100%.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from decimal import Decimal

from fastmcp.exceptions import ToolError

from . import staging
from . import jtypes as j
from . import write_common as w
from .jsontypes import JsonObject

SEPARATORS = re.compile(r"\s*(?:»|>|/)\s*")
LISTED = 30  # classifications named in a "not found" message


def _hundred() -> int:
    return int(j.classification().ONE_HUNDRED_PERCENT)


def _percent(weight: int) -> float:
    return float(Decimal(weight) * 100 / _hundred())


def _segments(text: str) -> list[str]:
    return [part.casefold() for part in SEPARATORS.split(text.strip()) if part]


def _path(classification: j.Classification) -> str:
    return str(classification.getPathName(False))


def exact_taxonomy(client: j.Client, query: str) -> j.Taxonomy:
    needle = query.strip().casefold()
    taxonomies = list(client.getTaxonomies())
    keys: tuple[Callable[[j.Taxonomy], str], ...] = (lambda t: str(t.getId()), lambda t: str(t.getName()))
    for key in keys:
        found = [t for t in taxonomies if key(t).casefold() == needle]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ToolError(f"{query!r} matches {len(found)} taxonomies; use the id instead")
    names = ", ".join(sorted(str(t.getName()) for t in taxonomies)) or "none"
    raise ToolError(f"no taxonomy is exactly {query!r}. Taxonomies: {names}")


def exact_classification(taxonomy: j.Taxonomy, query: str, *, allow_root: bool) -> j.Classification:
    """By id, by full name if it is unique, or by path below the root (the root's own name may
    lead the path)."""
    text = query.strip()
    if not text:
        raise ToolError("classification must not be empty")
    root = taxonomy.getRoot()
    candidates = [c for c in taxonomy.getAllClassifications() if allow_root or not w.same_id(c, root)]
    found = [c for c in candidates if str(c.getId()) == text]
    if not found:
        found = [c for c in candidates if str(c.getName()).casefold() == text.casefold()]
        if len(found) > 1:
            paths = "; ".join(sorted(_path(c) for c in found))
            raise ToolError(f"{query!r} names {len(found)} classifications ({paths}); give the path or id instead")
    if not found:
        wanted = _segments(text)
        found = [
            c for c in candidates
            if wanted in (_segments(_path(c)), _segments(str(c.getPathName(True))))
        ]
    if len(found) == 1:
        return found[0]
    paths = sorted(_path(c) for c in candidates if not w.same_id(c, root))
    shown = "; ".join(paths[:LISTED]) + (f"; … {len(paths) - LISTED} more" if len(paths) > LISTED else "")
    raise ToolError(
        f"no classification in {taxonomy.getName()} is exactly {query!r}. "
        f"Classifications: {shown or 'none yet (add one with add_classification)'}"
    )


def _assigned(taxonomy: j.Taxonomy, vehicle: j.InvestmentVehicle) -> int:
    """The vehicle's total weight across the taxonomy."""
    return sum(
        int(a.getWeight())
        for c in taxonomy.getAllClassifications()
        for a in c.getAssignments()
        if w.same(a.getInvestmentVehicle(), vehicle)
    )


# --- tools -----------------------------------------------------------------------------------


def add_classification(taxonomy: str, name: str, parent: str | None = None) -> staging.ApplyResult:
    """Add a new category to a taxonomy, e.g. a region or asset class. Changes go to the staged
    copy only. Refused if the parent already has a category with this name.

    Args:
        taxonomy: taxonomy id or full name (see list_taxonomies).
        name: name of the new category.
        parent: existing category to put it under (id, name or path such as
            "Developed markets » Europe"); omitted means top level.
    """
    clean_name = name.strip()
    if not clean_name:
        raise ToolError("name must not be empty")
    if SEPARATORS.search(clean_name):
        raise ToolError(f"name must not contain », > or /, which separate path parts: {name!r}")

    def mutate(client: j.Client) -> JsonObject:
        scheme = exact_taxonomy(client, taxonomy)
        under = scheme.getRoot() if parent is None or not parent.strip() else exact_classification(
            scheme, parent, allow_root=True
        )
        siblings = list(under.getChildren())
        for sibling in siblings:
            if str(sibling.getName()).casefold() == clean_name.casefold():
                raise ToolError(
                    f"{_path(sibling)} already exists in {scheme.getName()} (id {sibling.getId()}). "
                    "Nothing was staged."
                )
        created = j.classification()(under, str(uuid.uuid4()), clean_name)
        # PP's target weights for rebalancing: give the new category what its siblings leave.
        created.setWeight(max(0, _hundred() - int(under.getChildrenWeight())))
        created.setRank(max((int(s.getRank()) for s in siblings), default=-1) + 1)
        under.addChild(created)
        return {
            "taxonomy": str(scheme.getName()),
            "classification": {
                "id": str(created.getId()),
                "name": clean_name,
                "path": _path(created),
                "target_weight_percent": _percent(int(created.getWeight())),
            },
        }

    where = f" under {parent.strip()}" if parent and parent.strip() else ""
    return w.staged(f"add classification {clean_name}{where} to {taxonomy}", {"classifications": 1}, mutate)


def assign_classification(
    taxonomy: str,
    classification: str,
    security: str | None = None,
    account: str | None = None,
    weight: float | str = 100,
) -> staging.ApplyResult:
    """Put a security or an account into a category of a taxonomy, with a weight in percent.
    Fills what get_allocation reports as unassigned. Changes go to the staged copy only.

    Refused if it is already in that category (existing weights are never changed), or if its
    weights in this taxonomy would add up to more than 100%.

    Args:
        taxonomy: taxonomy id or full name (see list_taxonomies).
        classification: category id, name (if unique) or path such as "Developed markets » Europe".
        security: the security (UUID, ISIN, ticker or full name). Give this or account.
        account: the cash account (UUID or full name). Give this or security.
        weight: percent of the security or account that belongs here, up to 2 decimals (default 100).
    """
    if (security is None) == (account is None):
        raise ToolError("give exactly one of security or account")
    basis_points = w.fixed_point(weight, 100, "weight", 2)
    if basis_points > _hundred():
        raise ToolError(f"weight is a percentage and cannot exceed 100, got {weight!r}")

    def mutate(client: j.Client) -> JsonObject:
        scheme = exact_taxonomy(client, taxonomy)
        target = exact_classification(scheme, classification, allow_root=False)
        vehicle: j.InvestmentVehicle = (
            w.exact_security(client, security) if security is not None else w.exact_account(client, account or "")
        )
        for existing in target.getAssignments():
            if w.same(existing.getInvestmentVehicle(), vehicle):
                raise ToolError(
                    f"{vehicle.getName()} is already in {_path(target)} with "
                    f"{_percent(int(existing.getWeight())):g}%. Weights are never changed; nothing was staged."
                )
        before = _assigned(scheme, vehicle)
        if before + basis_points > _hundred():
            raise ToolError(
                f"{vehicle.getName()} already has {_percent(before):g}% assigned in {scheme.getName()}; "
                f"adding {_percent(basis_points):g}% would exceed 100%. Nothing was staged."
            )
        added = j.assignment()(vehicle, basis_points)
        added.setRank(max((int(a.getRank()) for a in target.getAssignments()), default=-1) + 1)
        target.addAssignment(added)
        return {
            "taxonomy": str(scheme.getName()),
            "classification": _path(target),
            "classification_id": str(target.getId()),
            ("security" if security is not None else "account"): str(vehicle.getName()),
            "weight_percent": _percent(basis_points),
            "assigned_in_taxonomy_percent": _percent(before + basis_points),
        }

    what = security if security is not None else account
    return w.staged(
        f"assign {what} to {classification} in {taxonomy} ({weight}%)", {"assignments": 1}, mutate
    )


CLASSIFICATION_TOOLS: list[Callable[..., object]] = [add_classification, assign_classification]
