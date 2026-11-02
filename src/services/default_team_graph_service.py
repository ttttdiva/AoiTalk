"""Owner-selected, revision-pinned AoiDots team configuration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from uuid import UUID

from sqlalchemy import select

from ..memory.models import Agent, AgentPrivateRoot, AgentRevision, ConversationSession, User
from .agent_team_graph_service import AgentTeamGraphError, AgentTeamGraphService, _uuid

SETTING_KEY = "aoidots_default_team_graph"


@dataclass(frozen=True)
class RoutingNode:
    key: str
    role: str
    agent_id: str
    revision_id: str | None


@dataclass(frozen=True)
class RoutingGroup:
    key: str
    leader: RoutingNode
    members: tuple[RoutingNode, ...]
    title: str = ""
    purpose: str = ""
    execution_kind: str | None = None


@dataclass(frozen=True)
class RoutingContext:
    graph_id: str
    revision_id: str
    content_hash: str
    root: RoutingNode
    groups: tuple[RoutingGroup, ...]
    title: str = ""


class DefaultTeamGraphService:
    def __init__(self, manager):
        self.graphs = AgentTeamGraphService(manager)

    async def _validated(self, session, owner, ref, *, root_session_id=None):
        if not isinstance(ref, dict) or set(ref) != {"graph_id", "revision_id", "content_hash"}:
            raise AgentTeamGraphError("default team graph unavailable", 409)
        roots = (await session.scalars(select(AgentPrivateRoot).where(
            AgentPrivateRoot.owner_user_id == owner, AgentPrivateRoot.purpose == "aoidots",
            AgentPrivateRoot.state == "active"))).all()
        if len(roots) != 1 or (root_session_id is not None and roots[0].session_id != _uuid(root_session_id, "root session")):
            raise AgentTeamGraphError("default team graph unavailable", 409)
        binding = (await session.execute(select(
            ConversationSession.user_id, ConversationSession.agent_id,
            ConversationSession.deleted_at, ConversationSession.project_id,
            ConversationSession.is_group_chat,
        ).where(ConversationSession.id == roots[0].session_id))).one_or_none()
        if (binding is None or binding.user_id != str(owner) or binding.agent_id != roots[0].agent_id
                or binding.deleted_at is not None or binding.project_id is not None or binding.is_group_chat):
            raise AgentTeamGraphError("default team graph unavailable", 409)
        graph = await self.graphs._graph(session, ref["graph_id"], owner)
        if graph.archived_at is not None:
            raise AgentTeamGraphError("default team graph unavailable", 409)
        envelope = await self.graphs.get_in_session(session, graph.id, actor_user_id=owner, revision_id=ref["revision_id"])
        if envelope["content_hash"] != ref["content_hash"]:
            raise AgentTeamGraphError("default team graph unavailable", 409)
        definition = envelope["definition"]
        nodes = {n["key"]: n for n in definition["nodes"]}
        root = nodes.get(definition["root_key"])
        if not root or root["role"] != "coordinator" or root["agent_id"] != str(roots[0].agent_id):
            raise AgentTeamGraphError("default team graph unavailable", 409)
        groups = definition["groups"]
        leaders = set()
        edges = definition["edges"]
        def reachable(start, allowed):
            seen = {start}
            pending = [start]
            while pending:
                current = pending.pop()
                for edge in edges:
                    target = None
                    if edge["source"] == current and edge["kind"] in ("supervises", "assigns", "handoff"):
                        target = edge["target"]
                    elif edge["target"] == current and edge["kind"] == "reviews":
                        target = edge["source"]
                    if target in allowed and target not in seen:
                        seen.add(target)
                        pending.append(target)
            return seen
        for group in groups:
            leader = nodes.get(group["leader_key"])
            if not leader or leader["role"] != "coordinator" or leader["agent_id"] == root["agent_id"] or len(group["node_keys"]) < 2 or root["key"] in group["node_keys"]:
                raise AgentTeamGraphError("default team graph unavailable", 409)
            if (leader["key"] not in reachable(root["key"], {root["key"], leader["key"]})
                    or not set(group["node_keys"]).issubset(reachable(leader["key"], set(group["node_keys"])) )):
                raise AgentTeamGraphError("default team graph unavailable", 409)
            leaders.add(leader["agent_id"])
        if len(leaders) < 2:
            raise AgentTeamGraphError("default team graph unavailable", 409)
        # Select metadata columns only: never load registered prompt bodies.
        for node in nodes.values():
            agent_id = UUID(node["agent_id"])
            row = (await session.execute(select(Agent.state).where(Agent.id == agent_id))).one_or_none()
            if row is None or row.state != "active":
                raise AgentTeamGraphError("default team graph unavailable", 409)
            if node["revision_id"]:
                agent = await session.scalar(select(AgentRevision.agent_id).where(AgentRevision.id == UUID(node["revision_id"])))
                if agent != agent_id:
                    raise AgentTeamGraphError("default team graph unavailable", 409)
        def safe(node):
            return RoutingNode(node["key"], node["role"], node["agent_id"], node["revision_id"])
        return RoutingContext(envelope["id"], envelope["revision_id"], envelope["content_hash"], safe(root), tuple(
            RoutingGroup(g["key"], safe(nodes[g["leader_key"]]), tuple(safe(nodes[k]) for k in g["node_keys"] if k != g["leader_key"]),
                         str(g.get("title") or "")[:200], str(g.get("purpose") or "")[:500],
                         g.get("execution_kind")) for g in groups), str(envelope.get("title") or "")[:200])

    async def get_in_session(self, session, *, owner_user_id, root_session_id=None):
        owner = await self.graphs._actor(session, owner_user_id)
        user = await session.get(User, owner)
        settings = user.user_settings if isinstance(user.user_settings, dict) else {}
        try:
            return await self._validated(session, owner, settings.get(SETTING_KEY), root_session_id=root_session_id)
        except (AgentTeamGraphError, ValueError, TypeError):
            return None

    async def get(self, *, owner_user_id):
        session = await self.graphs._session()
        try:
            return await self.get_in_session(session, owner_user_id=owner_user_id)
        finally:
            await session.close()

    async def set(self, *, owner_user_id, graph_id, revision_id, expected):
        session = await self.graphs._session()
        try:
            owner = await self.graphs._actor(session, owner_user_id)
            user = await session.scalar(select(User).where(User.id == owner).with_for_update())
            if user is None or not user.is_active or user.role != "admin":
                raise AgentTeamGraphError("default team graph unavailable", 409)
            settings = dict(user.user_settings) if isinstance(user.user_settings, dict) else {}
            # Read the exact revision through the service; its projection recomputes its hash.
            envelope = await self.graphs.get_in_session(session, graph_id, actor_user_id=owner, revision_id=revision_id)
            ref = {"graph_id": envelope["id"], "revision_id": envelope["revision_id"], "content_hash": envelope["content_hash"]}
            current = settings.get(SETTING_KEY)
            if current != expected and current != ref:
                # GET projects an unusable stored default as None. Under the
                # same User lock, permit replacing that state with expected=None
                # without exposing malformed or inaccessible historical refs.
                if expected is not None:
                    raise AgentTeamGraphError("stale default team graph", 409)
                try:
                    await self._validated(session, owner, current)
                except (AgentTeamGraphError, ValueError, TypeError):
                    pass
                else:
                    raise AgentTeamGraphError("stale default team graph", 409)
            context = await self._validated(session, owner, ref)
            if current != ref:
                settings[SETTING_KEY] = ref
                user.user_settings = settings
            await session.commit()
            return context
        finally:
            await session.close()


# This namespace is owned exclusively by AgentRunService, never by API metadata.
RUN_SNAPSHOT_KEY = "_default_routing_snapshot_v1"


def encode_run_snapshot(run, context):
    identity = {key: str(getattr(run, key)) for key in
                ("id", "user_id", "session_id", "agent_id", "agent_revision_id")}
    # Use the same JSON primitives before and after ORM persistence. Keep
    # bounded public Graph labels, never registered Agent instruction prose.
    projection = json.loads(json.dumps(asdict(context))) if context is not None else None
    return {"version": 1, "identity": identity,
            "status": "available" if context is not None else "unavailable",
            "context": projection}


def decode_run_snapshot(run, proof):
    """Fail closed on legacy, foreign, or malformed durable metadata."""
    try:
        metadata = run.run_metadata
        value = metadata.get(RUN_SNAPSHOT_KEY) if isinstance(metadata, dict) else None
        if not isinstance(value, dict) or set(value) != {"version", "identity", "status", "context"} or type(value["version"]) is not int or value["version"] != 1:
            return None
        identity = value["identity"]
        expected = {"id": proof.run_id, "user_id": proof.owner_user_id,
                    "session_id": proof.session_id, "agent_id": proof.agent_id,
                    "agent_revision_id": proof.agent_revision_id}
        if not isinstance(identity, dict) or identity != expected:
            return None
        if any(str(getattr(run, key, "")) != val or str(UUID(val)) != val for key, val in expected.items()):
            return None
        if value["status"] == "unavailable" and value["context"] is None:
            return None
        if value["status"] != "available":
            return None
        c = value["context"]
        def label(v, limit):
            if not isinstance(v, str) or len(v) > limit:
                raise ValueError("invalid label")
            return v
        def node(n):
            if not isinstance(n, dict) or set(n) != {"key", "role", "agent_id", "revision_id"}:
                raise ValueError("invalid node")
            if n["role"] not in {"coordinator", "worker", "reviewer"}:
                raise ValueError("invalid role")
            aid = str(UUID(n["agent_id"]))
            rev = str(UUID(n["revision_id"])) if n["revision_id"] is not None else None
            return RoutingNode(label(n["key"], 200), label(n["role"], 100), aid, rev)
        if not isinstance(c, dict) or set(c) != {"graph_id", "revision_id", "content_hash", "root", "groups", "title"}:
            return None
        graph_id, revision_id = str(UUID(c["graph_id"])), str(UUID(c["revision_id"]))
        if not isinstance(c["content_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", c["content_hash"]):
            return None
        root = node(c["root"])
        if root.agent_id != proof.agent_id:
            return None
        if not isinstance(c["groups"], list) or len(c["groups"]) > 100:
            return None
        groups = []
        for g in c["groups"]:
            if not isinstance(g, dict) or set(g) != {"key", "leader", "members", "title", "purpose", "execution_kind"} or not isinstance(g["members"], list) or len(g["members"]) > 100:
                return None
            kind = g["execution_kind"]
            if kind is not None:
                kind = label(kind, 100)
            groups.append(RoutingGroup(label(g["key"], 200), node(g["leader"]), tuple(node(n) for n in g["members"]), label(g["title"], 200), label(g["purpose"], 500), kind))
        return RoutingContext(graph_id, revision_id, c["content_hash"], root, tuple(groups), label(c["title"], 200))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
