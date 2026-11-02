"""Validate the server-owned receipt for an initial Graph planning delegation."""
import json
from collections.abc import Mapping


def accepted_graph_plan_receipt(record, *, source, graph):
    if (getattr(record, "tool", None) != "delegate_plan"
            or getattr(record, "executed", None) is not True
            or getattr(record, "synthetic", True) is not False):
        return False
    try:
        receipt = json.loads(record.result)
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(receipt, dict) or receipt.get("success") is not True:
        return False
    delegation = receipt.get("delegation")
    if not isinstance(delegation, dict) or not isinstance(source, Mapping) or not isinstance(graph, Mapping):
        return False
    if not isinstance(delegation.get("id"), str) or not delegation["id"].strip():
        return False
    actor = delegation.get("source_actor")
    keys = ("source_run_id", "source_work_item_id", "source_agent_id", "source_revision_id", "source_session_id")
    if not isinstance(actor, dict) or any(not source.get(k) or actor.get(k) != source[k] for k in keys):
        return False
    planning = delegation.get("planning")
    if not isinstance(planning, dict) or planning.get("enabled") is not True:
        return False
    provenance = planning.get("graph_provenance")
    return (planning.get("team_graph_ref") == graph.get("ref")
            and bool(graph.get("ref")) and bool(graph.get("graph_hash"))
            and isinstance(provenance, dict)
            and provenance.get("graph_hash") == graph["graph_hash"])
