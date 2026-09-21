"""Durable MediaOps Automation orchestration owned by AoiTalk.

Automation owns what/why/when. Generation Studio owns how generation executes.
Research evidence is written through the existing MediaOps Research domain.
"""
from __future__ import annotations

import asyncio, hashlib, html, json, os, re, socket
import ipaddress
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models import AutomationProgram, AutomationProgramRevision, AutomationRun, GenerationWorkspace
from .media_execution_adapters import MediaResearchExecutionAdapter
from .media_operations_generation_service import GenerationStudioAdapter, HttpGenerationStudioAdapter, StudioScope, UnavailableGenerationStudioAdapter
from .media_operations_research_service import MediaOperationsResearchService
from .media_operations_service import (
    MediaOperationsConflictError, MediaOperationsNotFoundError, MediaOperationsService,
    MediaOperationsValidationError, _as_uuid, _idempotency_key, _optional_text,
    _required_text, sha256_json,
)

_EXECUTION_MODES={"research_only","draft","review_before_generate","auto_generate"}
_NOVELTY={"conservative","balanced","exploratory"}
_DISCOVERY_TYPES={"manual","local_pool","calendar","web_search","rss","json_feed"}
_PRESET_ID_RE=re.compile(r"^pst_[A-Za-z0-9_-]{8,160}$")
_PRESET_REVISION_ID_RE=re.compile(r"^psr_[A-Za-z0-9_-]{8,160}$")
_TERMINAL={"complete","failed"}


def _safe_json(value: Any, label: str, depth: int=0) -> Any:
    if depth>6: raise MediaOperationsValidationError(f"{label} is nested too deeply")
    if value is None or isinstance(value,(bool,int,float)): return value
    if isinstance(value,str):
        if len(value)>16000: raise MediaOperationsValidationError(f"{label} contains an oversized value")
        if any(ord(ch)<0x20 and ch not in "\t\n\r" for ch in value): raise MediaOperationsValidationError(f"{label} contains control characters")
        return value
    if isinstance(value,Mapping):
        if len(value)>100: raise MediaOperationsValidationError(f"{label} has too many fields")
        out={}
        for raw_key,child in value.items():
            key=_required_text(raw_key,f"{label} key",100); compact=key.casefold().replace("_","")
            if any(token in compact for token in ("apikey","bearertoken","password","cookie","credential","workflowgraph","providerpayload")):
                raise MediaOperationsValidationError(f"{label} contains a forbidden field")
            out[key]=_safe_json(child,f"{label}.{key}",depth+1)
        return out
    if isinstance(value,Sequence) and not isinstance(value,(str,bytes,bytearray)):
        if len(value)>200: raise MediaOperationsValidationError(f"{label} has too many items")
        return [_safe_json(item,f"{label}[]",depth+1) for item in value]
    raise MediaOperationsValidationError(f"{label} must be JSON compatible")


def _normalize_revision(*,execution_mode:Any,trigger:Any,discovery:Any,research_binding:Any,planning_policy:Any,generation_action:Any,fallback:Any)->dict[str,Any]:
    mode=str(execution_mode or "draft").strip().lower()
    if mode not in _EXECUTION_MODES: raise MediaOperationsValidationError("execution_mode is not supported")
    trigger_v=_safe_json(trigger or {"type":"manual"},"trigger")
    discovery_v=_safe_json(discovery or {"type":"manual"},"discovery")
    research_v=_safe_json(research_binding or {},"research_binding")
    planning_v=_safe_json(planning_policy or {},"planning_policy")
    generation_v=_safe_json(generation_action or {},"generation_action")
    fallback_v=_safe_json(fallback or {},"fallback")
    dtype=str(discovery_v.get("type") or "manual").strip().lower()
    if dtype not in _DISCOVERY_TYPES: raise MediaOperationsValidationError("discovery.type is not supported")
    discovery_v["type"]=dtype
    novelty=str(planning_v.get("novelty_policy") or "balanced").strip().lower()
    if novelty not in _NOVELTY: raise MediaOperationsValidationError("planning_policy.novelty_policy is invalid")
    planning_v["novelty_policy"]=novelty
    count=planning_v.get("candidate_count",3)
    if isinstance(count,bool): raise MediaOperationsValidationError("planning_policy.candidate_count must be an integer")
    try: count=int(count)
    except (TypeError,ValueError) as exc: raise MediaOperationsValidationError("planning_policy.candidate_count must be an integer") from exc
    if not 1<=count<=12: raise MediaOperationsValidationError("planning_policy.candidate_count must be between 1 and 12")
    planning_v["candidate_count"]=count
    if mode in {"review_before_generate","auto_generate"}:
        kind=str(generation_v.get("kind") or "image").strip().lower(); generation_v["kind"]=kind
        _as_uuid(generation_v.get("studio_workspace_binding_id"),"studio_workspace_binding_id")
        preset_id=str(generation_v.get("preset_id") or "").strip()
        if not _PRESET_ID_RE.fullmatch(preset_id): raise MediaOperationsValidationError("generation_action.preset_id must be an opaque preset reference")
        generation_v["preset_id"]=preset_id
        policy=str(generation_v.get("preset_revision_policy") or "latest_at_submission").strip().lower()
        if policy=="latest_at_execution": policy="latest_at_submission"
        if policy not in {"latest_at_submission","pinned"}: raise MediaOperationsValidationError("generation_action.preset_revision_policy is invalid")
        revision_id=generation_v.get("preset_revision_id")
        if policy=="pinned" and (not isinstance(revision_id,str) or not _PRESET_REVISION_ID_RE.fullmatch(revision_id)):
            raise MediaOperationsValidationError("pinned generation action requires preset_revision_id")
        if policy=="latest_at_submission" and revision_id not in (None,""):
            raise MediaOperationsValidationError("latest_at_submission must not pin preset_revision_id")
        generation_v["preset_revision_policy"]=policy
    return {"execution_mode":mode,"trigger":trigger_v,"discovery":discovery_v,"research_binding":research_v,"planning_policy":planning_v,"generation_action":generation_v,"fallback":fallback_v}


class AutomationPlanner(Protocol):
    async def plan(self,*,observation:Mapping[str,Any],research_brief:Mapping[str,Any],novelty_snapshot:Mapping[str,Any],planning_policy:Mapping[str,Any],action_kind:str)->Sequence[Mapping[str,Any]]: ...


class DefaultAutomationPlanner:
    """Use the existing AoiTalk LLM when configured, with a research-grounded deterministic fallback."""
    def __init__(self,config:Any=None)->None: self.config=config
    async def _llm(self,prompt:str)->Sequence[Mapping[str,Any]]|None:
        try:
            from ..llm.manager import create_llm_client
            client=create_llm_client(self.config)
            if hasattr(client,"clear_history"): client.clear_history()
            response=await client.generate_response_async(prompt) if hasattr(client,"generate_response_async") else await asyncio.to_thread(client.generate_response,prompt)
            text=str(response or "").strip()
            if text.startswith("```"): text=re.sub(r"^```(?:json)?\s*|\s*```$","",text,flags=re.I|re.S)
            parsed=json.loads(text); parsed=parsed.get("candidates") if isinstance(parsed,Mapping) else parsed
            if isinstance(parsed,Sequence) and not isinstance(parsed,(str,bytes)): return [x for x in parsed if isinstance(x,Mapping)]
        except Exception: return None
        return None
    async def plan(self,*,observation:Mapping[str,Any],research_brief:Mapping[str,Any],novelty_snapshot:Mapping[str,Any],planning_policy:Mapping[str,Any],action_kind:str)->Sequence[Mapping[str,Any]]:
        count=int(planning_policy.get("candidate_count") or 3)
        prompt=json.dumps({"instruction":"Create distinct source-grounded creative candidates. Return JSON only as {candidates:[...]}. Do not invent source claims.","action_kind":action_kind,"candidate_count":count,"novelty_policy":planning_policy.get("novelty_policy","balanced"),"theme":observation,"research_brief":research_brief,"recent_usage":novelty_snapshot,"image_candidate_schema":{"title":"","concept":"","theme_interpretation":"","subject":"","outfit":"","pose":"","composition":"","environment":"","lighting":"","palette":"","accessories":[],"prompt":"","negative_prompt_additions":"","research_source_refs":[]}},ensure_ascii=False)
        planned=await self._llm(prompt)
        if planned: return list(planned)[:count]
        theme=str(observation.get("title") or observation.get("topic") or "Untitled theme")
        facts=list(research_brief.get("key_facts") or []); visual=list(research_brief.get("visual_ideas") or []); refs=list(research_brief.get("source_refs") or [])
        out=[]
        for i in range(count):
            fact=str(facts[i%len(facts)]) if facts else theme; idea=str(visual[i%len(visual)]) if visual else fact
            out.append({"title":f"{theme} — Concept {i+1}","concept":idea[:1000],"theme_interpretation":fact[:1000],"subject":theme[:500],"outfit":"","pose":"","composition":"","environment":idea[:500],"lighting":"","palette":"","accessories":[],"prompt":f"{theme}, {idea}"[:8000],"negative_prompt_additions":"","research_source_refs":refs[:20]})
        return out


class _TextExtractor(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag.casefold() in {"script","style","noscript","svg"}: self.skip+=1
    def handle_endtag(self,tag):
        if tag.casefold() in {"script","style","noscript","svg"} and self.skip: self.skip-=1
    def handle_data(self,data):
        if not self.skip:
            value=" ".join(data.split())
            if value: self.parts.append(value)


class SafeAutomationFetcher:
    """Bounded public-web fetcher reusing AoiTalk's DNS-pinned OGP transport."""

    def __init__(self, *, timeout: float = 10.0, max_bytes: int = 512000, redirects: int = 3):
        # The shared pinned transport has an absolute per-request deadline; keep
        # this setting bounded for future transport tuning and API compatibility.
        self.timeout = max(1.0, min(float(timeout), 30.0))
        self.max_bytes = max(8192, min(int(max_bytes), 2000000))
        self.redirects = max(0, min(int(redirects), 5))
        self._cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    @staticmethod
    def _domain_allowed(url: str, allow_domains: Sequence[str] = (), deny_domains: Sequence[str] = ()) -> None:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("unsafe_url")
        host = parsed.hostname.casefold().rstrip(".")
        allow = {str(item).casefold().rstrip(".") for item in allow_domains if str(item).strip()}
        deny = {str(item).casefold().rstrip(".") for item in deny_domains if str(item).strip()}
        if deny and any(host == item or host.endswith("." + item) for item in deny):
            raise ValueError("domain_denied")
        if allow and not any(host == item or host.endswith("." + item) for item in allow):
            raise ValueError("domain_not_allowed")

    @classmethod
    async def _public(cls, url: str, allow_domains: Sequence[str] = (), deny_domains: Sequence[str] = ()) -> str:
        cls._domain_allowed(url, allow_domains, deny_domains)
        # Reuse the existing OGP endpoint resolver: it rejects browser-style
        # alternate IPv4 literals, validates every DNS answer, and fails closed
        # if any answer is private/loopback/link-local/reserved.
        from ..api.routes.ogp_routes import _resolve_public_ogp_endpoint

        try:
            await _resolve_public_ogp_endpoint(url)
        except Exception as exc:
            message = str(exc).casefold()
            if "プライベート" in str(exc) or "reserved" in message or "private" in message:
                raise ValueError("private_address") from exc
            raise ValueError("unsafe_url") from exc
        return url

    async def fetch(self, url: str, *, allow_domains: Sequence[str] = (), deny_domains: Sequence[str] = ()) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cached = self._cache.get(url)
        if cached and (now - cached[0]).total_seconds() < 900:
            return dict(cached[1])

        from ..api.routes.ogp_routes import _pinned_ogp_request, _resolve_public_ogp_endpoint

        target = str(url or "").strip()
        response = None
        for redirect_count in range(self.redirects + 1):
            self._domain_allowed(target, allow_domains, deny_domains)
            try:
                parts, address = await _resolve_public_ogp_endpoint(target)
                response = await asyncio.to_thread(
                    _pinned_ogp_request,
                    parts,
                    address,
                    max_response_bytes=self.max_bytes,
                    user_agent="AoiTalk-AutomationResearch/1.0",
                    accept="text/html,application/json,text/plain,application/feed+json,application/rss+xml,application/atom+xml,text/xml,application/xml;q=0.9,*/*;q=0.1",
                )
            except Exception as exc:
                message = str(exc).casefold()
                if "サイズ上限" in str(exc):
                    raise ValueError("response_too_large") from exc
                if "タイムアウト" in str(exc) or "timeout" in message:
                    raise ValueError("timeout") from exc
                if "プライベート" in str(exc) or "private" in message or "reserved" in message:
                    raise ValueError("private_address") from exc
                raise ValueError("fetch_failed") from exc

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect_missing")
                if redirect_count >= self.redirects:
                    raise ValueError("redirect_limit")
                target = urljoin(target, location)
                # The next loop validates domain policy and performs a fresh,
                # pinned DNS resolution before opening any socket.
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise ValueError(f"http_{response.status_code}")
            break
        if response is None:
            raise ValueError("fetch_failed")

        ctype = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        allowed_types = {
            "text/html", "text/plain", "application/json", "application/feed+json",
            "application/rss+xml", "application/atom+xml", "text/xml", "application/xml",
        }
        if ctype not in allowed_types:
            raise ValueError("unsupported_content_type")
        body = bytes(response.content)
        if len(body) > self.max_bytes:
            raise ValueError("response_too_large")
        text = body.decode(response.encoding or "utf-8", errors="replace")
        if ctype == "text/html":
            parser = _TextExtractor()
            parser.feed(text)
            text = "\n".join(parser.parts)
        result = {
            "url": target,
            "content_type": ctype,
            "text": html.unescape(text)[:12000],
            "retrieved_at": now.isoformat(),
        }
        self._cache[url] = (now, result)
        return dict(result)


class MediaOperationsAutomationService(MediaOperationsService):
    def __init__(self,session:Any|None=None,*,adapter:GenerationStudioAdapter|None=None,planner:AutomationPlanner|None=None,search_client:Any|None=None,config:Any=None,fetcher:SafeAutomationFetcher|None=None)->None:
        super().__init__(session=session)
        if adapter is not None: self.adapter=adapter
        elif os.getenv("AOITALK_GENERATION_STUDIO_BEARER_TOKEN") or os.getenv("GENERATION_STUDIO_BEARER_TOKEN"): self.adapter=HttpGenerationStudioAdapter()
        else: self.adapter=UnavailableGenerationStudioAdapter()
        self.research=MediaOperationsResearchService(session=session)
        self.research_executor=MediaResearchExecutionAdapter(service=self.research,search_client=search_client,config=config)
        self.planner=planner or DefaultAutomationPlanner(config); self.fetcher=fetcher or SafeAutomationFetcher()

    async def _get(self,session:Any,model:Any,entity_id:UUID|str,label:str,*,for_update:bool=False)->Any:
        parsed=_as_uuid(entity_id,label); statement=select(model).where(model.id==parsed).limit(1)
        if for_update: statement=statement.with_for_update()
        row=await self._scalar(session,statement)
        if row is None: raise MediaOperationsNotFoundError(f"{label} not found")
        return row

    async def _latest_revision(self,session:Any,program_id:UUID)->AutomationProgramRevision|None:
        return await self._scalar(session,select(AutomationProgramRevision).where(AutomationProgramRevision.program_id==program_id).order_by(AutomationProgramRevision.version.desc()).limit(1))

    async def _program_detail(self,session:Any,program:AutomationProgram)->dict[str,Any]:
        revisions=await self._scalars(session,select(AutomationProgramRevision).where(AutomationProgramRevision.program_id==program.id).order_by(AutomationProgramRevision.version.desc()).limit(100))
        latest=await self._scalar(session,select(AutomationRun).where(AutomationRun.program_id==program.id).order_by(AutomationRun.created_at.desc()).limit(1))
        return {**program.to_safe_dict(),"current_revision":revisions[0].to_safe_dict() if revisions else None,"revisions":[x.to_safe_dict() for x in revisions],"latest_run":latest.to_safe_dict() if latest else None}

    async def create_program(self,session:Any|None=None,actor:Any|None=None,*,project_id:UUID|str|None=None,name:Any,enabled:bool=True,execution_mode:Any="draft",trigger:Any=None,discovery:Any=None,research_binding:Any=None,planning_policy:Any=None,generation_action:Any=None,fallback:Any=None,idempotency_key:Any)->dict[str,Any]:
        session=self._resolve_session(session)
        if actor is None: raise MediaOperationsValidationError("actor is required")
        project_uuid=_as_uuid(project_id,"project_id",required=False); actor_id=await self._assert_create_scope(session,actor,project_uuid); key=_idempotency_key(idempotency_key)
        values=_normalize_revision(execution_mode=execution_mode,trigger=trigger,discovery=discovery,research_binding=research_binding,planning_policy=planning_policy,generation_action=generation_action,fallback=fallback)
        program_name=_required_text(name,"name",255); create_hash=sha256_json({"name":program_name,"enabled":bool(enabled),"revision":values})
        conditions=[AutomationProgram.idempotency_key==key]
        if project_uuid is None: conditions += [AutomationProgram.project_id.is_(None),AutomationProgram.owner_user_id==actor_id]
        else: conditions += [AutomationProgram.project_id==project_uuid]
        existing=await self._scalar(session,select(AutomationProgram).where(*conditions).limit(1))
        if existing is not None:
            if existing.create_hash!=create_hash: raise MediaOperationsConflictError("idempotency key was already used with a different Automation Program payload")
            return await self._program_detail(session,existing)
        program=AutomationProgram(id=uuid4(),owner_user_id=actor_id,project_id=project_uuid,name=program_name,enabled=bool(enabled),create_hash=create_hash,idempotency_key=key,created_by=actor_id)
        revision=AutomationProgramRevision(id=uuid4(),program_id=program.id,owner_user_id=actor_id,project_id=project_uuid,version=1,execution_mode=values["execution_mode"],trigger_json=values["trigger"],discovery_json=values["discovery"],research_binding_json=values["research_binding"],planning_policy_json=values["planning_policy"],generation_action_json=values["generation_action"],fallback_json=values["fallback"],content_hash=sha256_json(values),idempotency_key=None,created_by=actor_id)
        session.add(program); session.add(revision)
        try: await self._flush_commit(session)
        except IntegrityError as exc: await self._rollback(session); raise MediaOperationsConflictError("Automation Program conflicts with an existing record") from exc
        return await self._program_detail(session,program)

    async def append_revision(self,session:Any|None=None,actor:Any|None=None,program_id:UUID|str|None=None,*,expected_version:Any,execution_mode:Any,trigger:Any=None,discovery:Any=None,research_binding:Any=None,planning_policy:Any=None,generation_action:Any=None,fallback:Any=None,idempotency_key:Any)->dict[str,Any]:
        session=self._resolve_session(session)
        if actor is None or program_id is None: raise MediaOperationsValidationError("actor and program_id are required")
        program=await self._get(session,AutomationProgram,program_id,"program_id",for_update=True); actor_id=await self._assert_entity_access(session,actor,program,permission="write"); current=await self._latest_revision(session,program.id)
        if current is None: raise MediaOperationsConflictError("Automation Program revision history is incomplete")
        try: expected=int(expected_version)
        except (TypeError,ValueError) as exc: raise MediaOperationsValidationError("expected_version must be an integer") from exc
        key=_idempotency_key(idempotency_key); values=_normalize_revision(execution_mode=execution_mode,trigger=trigger,discovery=discovery,research_binding=research_binding,planning_policy=planning_policy,generation_action=generation_action,fallback=fallback); content_hash=sha256_json(values)
        existing=await self._scalar(session,select(AutomationProgramRevision).where(AutomationProgramRevision.program_id==program.id,AutomationProgramRevision.idempotency_key==key).limit(1))
        if existing is not None:
            if existing.content_hash!=content_hash: raise MediaOperationsConflictError("idempotency key was already used with different revision content")
            return existing.to_safe_dict()
        if expected!=current.version: raise MediaOperationsConflictError("stale Automation Program version")
        revision=AutomationProgramRevision(id=uuid4(),program_id=program.id,owner_user_id=program.owner_user_id,project_id=program.project_id,version=expected+1,execution_mode=values["execution_mode"],trigger_json=values["trigger"],discovery_json=values["discovery"],research_binding_json=values["research_binding"],planning_policy_json=values["planning_policy"],generation_action_json=values["generation_action"],fallback_json=values["fallback"],content_hash=content_hash,idempotency_key=key,created_by=actor_id)
        session.add(revision); await self._flush_commit(session); return revision.to_safe_dict()

    async def list_programs(self,session:Any|None=None,actor:Any|None=None,*,project_id:UUID|str|None=None,limit:int=100)->list[dict[str,Any]]:
        session=self._resolve_session(session)
        if actor is None: raise MediaOperationsValidationError("actor is required")
        project_uuid=_as_uuid(project_id,"project_id",required=False); rows=await self._scalars(session,select(AutomationProgram).order_by(AutomationProgram.updated_at.desc()).limit(max(1,min(int(limit),200)))); out=[]
        for row in rows:
            if project_uuid is not None and row.project_id!=project_uuid: continue
            try: await self._assert_entity_access(session,actor,row,permission="read")
            except Exception: continue
            out.append(await self._program_detail(session,row))
        return out

    async def get_program(self,session:Any|None=None,actor:Any|None=None,program_id:UUID|str|None=None)->dict[str,Any]:
        session=self._resolve_session(session)
        if actor is None or program_id is None: raise MediaOperationsValidationError("actor and program_id are required")
        row=await self._get(session,AutomationProgram,program_id,"program_id"); await self._assert_entity_access(session,actor,row,permission="read"); return await self._program_detail(session,row)

    async def set_enabled(self,session:Any|None=None,actor:Any|None=None,program_id:UUID|str|None=None,*,enabled:bool)->dict[str,Any]:
        session=self._resolve_session(session); row=await self._get(session,AutomationProgram,program_id,"program_id",for_update=True); await self._assert_entity_access(session,actor,row,permission="write"); row.enabled=bool(enabled); await self._flush_commit(session); return await self._program_detail(session,row)

    async def duplicate_program(self,session:Any|None=None,actor:Any|None=None,program_id:UUID|str|None=None,*,name:Any=None,idempotency_key:Any)->dict[str,Any]:
        detail=await self.get_program(session,actor,program_id); rev=detail["current_revision"]
        return await self.create_program(session,actor,project_id=detail["project_id"],name=name or f"{detail['name']} copy",enabled=False,execution_mode=rev["execution_mode"],trigger=rev["trigger"],discovery=rev["discovery"],research_binding=rev["research_binding"],planning_policy=rev["planning_policy"],generation_action=rev["generation_action"],fallback=rev["fallback"],idempotency_key=idempotency_key)

    async def list_preset_catalog(self,session:Any|None=None,actor:Any|None=None,*,workspace_id:UUID|str)->list[dict[str,Any]]:
        session=self._resolve_session(session)
        if actor is None: raise MediaOperationsValidationError("actor is required")
        workspace=await self._get(session,GenerationWorkspace,workspace_id,"workspace_id"); await self._assert_entity_access(session,actor,workspace,permission="read")
        if workspace.status not in {"configured","verified"}: return []
        raw=await self.adapter.get_preset_catalog(StudioScope(workspace.external_workspace_id,workspace.external_project_id,workspace.base_url)); items=raw.get("items") if isinstance(raw,Mapping) else None
        if not isinstance(items,Sequence) or isinstance(items,(str,bytes)): return []
        allowed={"preset_id","name","description","current_revision_id","current_revision","checksum","model_family","updated_at"}; out=[]
        for item in items[:200]:
            if not isinstance(item,Mapping) or set(item)-allowed: continue
            if not _PRESET_ID_RE.fullmatch(str(item.get("preset_id") or "")) or not _PRESET_REVISION_ID_RE.fullmatch(str(item.get("current_revision_id") or "")): continue
            out.append(dict(item))
        return out

    async def _discover(self,revision:AutomationProgramRevision,run:AutomationRun)->dict[str,Any]:
        config=dict(revision.discovery_json or {}); kind=str(config.get("type") or "manual"); now=datetime.now(timezone.utc)
        if kind=="manual":
            title=_required_text(config.get("theme") or config.get("title") or config.get("topic"),"discovery.theme",500)
            return {"title":title,"description":_optional_text(config.get("description"),"discovery.description",2000),"source_type":"manual","source_url":None,"published_at":None,"retrieved_at":now.isoformat(),"checksum":sha256_json({"kind":kind,"title":title})}
        if kind=="local_pool":
            pool=config.get("themes") or []
            if not isinstance(pool,Sequence) or isinstance(pool,(str,bytes)) or not pool: raise MediaOperationsValidationError("local_pool discovery requires themes")
            index=int(run.execution_key[:8],16)%len(pool); raw=pool[index]; title=_required_text(raw.get("title") if isinstance(raw,Mapping) else raw,"discovery.theme",500); desc=_optional_text(raw.get("description") if isinstance(raw,Mapping) else None,"discovery.description",2000)
            return {"title":title,"description":desc,"source_type":"local_pool","source_url":None,"published_at":None,"retrieved_at":now.isoformat(),"checksum":sha256_json({"kind":kind,"title":title,"index":index})}
        if kind=="calendar":
            labels=config.get("labels") if isinstance(config.get("labels"),Mapping) else {}; title=str(labels.get(f"{now.month:02d}") or labels.get(str(now.month)) or now.strftime("%B seasonal theme"))
            return {"title":title[:500],"description":f"Calendar/seasonal observation for {now.date().isoformat()}","source_type":"calendar","source_url":None,"published_at":now.date().isoformat(),"retrieved_at":now.isoformat(),"checksum":sha256_json({"kind":kind,"date":now.date().isoformat(),"title":title})}
        if kind=="web_search":
            query=_required_text(config.get("query") or config.get("theme_query"),"discovery.query",500); request={"queries":[query],"engines":config.get("engines") or ["searxng"],"max_results_per_engine":min(20,int(config.get("max_results") or 10)),"project_id":str(run.project_id) if run.project_id else None,"domains":config.get("domains") or []}
            sources=await self.research_executor._invoke_search(request,{"id":str(run.owner_user_id),"actor_type":"human"}); projected=[self.research_executor._source_projection(x) for x in sources]; projected=[x for x in projected if x]
            if not projected: raise RuntimeError("automation_discovery_search_unavailable")
            chosen=projected[0]; return {"title":chosen["title"],"description":chosen["summary"],"source_type":"web_search","source_url":chosen.get("source_url"),"published_at":chosen.get("source_published_at"),"retrieved_at":now.isoformat(),"checksum":sha256_json(chosen),"provider_metadata":{"query":query,"candidate_count":len(projected)},"discovery_candidates":projected[:10]}
        if kind in {"rss","json_feed"}:
            url=_required_text(config.get("url"),"discovery.url",2000); fetched=await self.fetcher.fetch(url,allow_domains=config.get("allow_domains") or (),deny_domains=config.get("deny_domains") or ()); text=fetched["text"]
            if kind=="json_feed":
                try: data=json.loads(text)
                except ValueError as exc: raise RuntimeError("automation_json_feed_invalid") from exc
                items=(data.get("items") or data.get("entries") or []) if isinstance(data,Mapping) else data
                if not isinstance(items,Sequence) or isinstance(items,(str,bytes)) or not items: raise RuntimeError("automation_json_feed_empty")
                first=items[0]; title=_required_text(first.get("title") if isinstance(first,Mapping) else str(first),"discovery.feed.title",500); desc=_optional_text(first.get("description") if isinstance(first,Mapping) else None,"discovery.feed.description",2000)
            else:
                match=re.search(r"<title[^>]*>(.*?)</title>",text,re.I|re.S); title=html.unescape(re.sub(r"<[^>]+>","",match.group(1))).strip() if match else "Feed theme"; desc=text[:2000]
            return {"title":title[:500],"description":desc,"source_type":kind,"source_url":fetched["url"],"published_at":None,"retrieved_at":fetched["retrieved_at"],"checksum":sha256_json({"kind":kind,"url":fetched["url"],"title":title})}
        raise MediaOperationsValidationError("unsupported discovery type")


    async def _run_research(self,session:Any,actor:Any,revision:AutomationProgramRevision,run:AutomationRun)->tuple[str|None,dict[str,Any]]:
        binding=dict(revision.research_binding_json or {}); routine_id=binding.get("research_routine_id"); routine_version=binding.get("routine_version")
        observation=run.observation_json or {}
        if not routine_id or routine_version in (None,""):
            brief={"key_facts":[observation.get("description") or observation.get("title")],"semantic_ideas":[observation.get("title")],"visual_ideas":[observation.get("description") or observation.get("title")],"materials":[],"colors":[],"cultural_historical_context":[],"current_trending_context":[],"source_refs":[observation.get("source_url")] if observation.get("source_url") else []}
            return None,brief
        routine=await self.research.get_research_routine(session,actor,routine_id); revisions=routine.get("revisions") or []; pinned=next((x for x in revisions if int(x.get("version") or 0)==int(routine_version)),None)
        if pinned is None: raise MediaOperationsConflictError("research routine revision is unavailable")
        started=await self.research.start_research_run(session,actor,research_routine_id=routine_id,routine_version=int(routine_version),focus_note=str(observation.get("title") or "")[:4000],source_refs=[observation.get("source_url")] if observation.get("source_url") else [],omissions=[],status="recorded",idempotency_key=f"automation:{run.execution_key}:research")
        research_run_id=started["id"]
        request=self.research_executor._search_request({"project_id":str(run.project_id) if run.project_id else None,"payload":{"routine_revision":pinned}}); theme=str(observation.get("title") or "").strip()
        if theme:
            queries=list(request.get("queries") or []); request["queries"]=[f"{theme} {q}"[:500] for q in queries] if queries else [theme]
        sources=await self.research_executor._invoke_search(request,actor)
        for index,source in enumerate(sources[:20]):
            projection=self.research_executor._source_projection(source)
            if not projection or not projection["evidence"]: continue
            try: await self.research.append_research_finding(session,actor,research_run_id,kind="fact",statement=projection["summary"],evidence=projection["evidence"],idempotency_key=f"automation:{run.execution_key}:finding:{index}")
            except MediaOperationsConflictError: pass
            try: await self.research.create_research_candidate(session,actor,research_run_id=research_run_id,candidate_key=hashlib.sha256((projection.get("source_url") or projection["title"]).encode()).hexdigest(),title=projection["title"],summary=projection["summary"],source_url=projection.get("source_url"),source_published_at=projection.get("source_published_at"),evidence=projection["evidence"],reason="Automation research discovery",idempotency_key=f"automation:{run.execution_key}:candidate:{index}")
            except MediaOperationsConflictError: pass
        findings=await self.research.list_research_findings(session,actor,research_run_id,limit=100); refs=[]; facts=[]; notes=[]; allow=binding.get("allow_domains") or []; deny=binding.get("deny_domains") or []
        for finding in findings[:50]:
            facts.append(str(finding.get("statement") or "")[:2000])
            for evidence in finding.get("evidence") or []:
                provenance=evidence.get("provenance") if isinstance(evidence,Mapping) else None
                url=(evidence.get("source_url") if isinstance(evidence,Mapping) else None) or (provenance.get("url") if isinstance(provenance,Mapping) else None)
                if url and url not in refs: refs.append(url)
        if binding.get("fetch_pages",True):
            for url in refs[:5]:
                try:
                    page=await self.fetcher.fetch(url,allow_domains=allow,deny_domains=deny); notes.append({"url":page["url"],"content_type":page["content_type"],"text":page["text"][:4000]})
                except Exception: continue
        brief={"key_facts":facts[:30],"semantic_ideas":facts[:12],"visual_ideas":[x.get("text","")[:1000] for x in notes[:8]] or facts[:8],"materials":[],"colors":[],"cultural_historical_context":[],"current_trending_context":facts[:10],"source_refs":refs[:50],"source_notes":notes[:8],"theme":observation.get("title")}
        return research_run_id,brief

    async def _novelty_snapshot(self,session:Any,program:AutomationProgram,policy:str)->dict[str,Any]:
        rows=await self._scalars(session,select(AutomationRun).where(AutomationRun.program_id==program.id).order_by(AutomationRun.created_at.desc()).limit(20)); fields={k:[] for k in ("theme","concept","outfit","pose","composition","background","palette","prompt")}
        for row in rows:
            theme=(row.observation_json or {}).get("title")
            if theme and theme not in fields["theme"]: fields["theme"].append(theme)
            for candidate in row.candidates_json or []:
                if not isinstance(candidate,Mapping): continue
                payload=candidate.get("payload") if isinstance(candidate.get("payload"),Mapping) else candidate
                for key in ("concept","outfit","pose","composition","palette","prompt"):
                    value=payload.get(key)
                    if value and value not in fields[key]: fields[key].append(str(value)[:1000])
                value=payload.get("environment") or payload.get("background")
                if value and value not in fields["background"]: fields["background"].append(str(value)[:1000])
        cap={"conservative":5,"balanced":10,"exploratory":20}.get(policy,10)
        return {"policy":policy,**{k:v[:cap] for k,v in fields.items()}}

    async def list_due_programs(self,session:Any|None=None,actor:Any|None=None,*,project_id:UUID|str|None=None,as_of:Any=None,limit:int=100)->list[dict[str,Any]]:
        session=self._resolve_session(session)
        if actor is None: raise MediaOperationsValidationError("actor is required")
        now=as_of if isinstance(as_of,datetime) else datetime.utcnow()
        project_uuid=_as_uuid(project_id,"project_id",required=False)
        rows=await self._scalars(session,select(AutomationProgram).where(AutomationProgram.enabled.is_(True)).order_by(AutomationProgram.updated_at.asc()).limit(max(1,min(int(limit),200))))
        result=[]
        for program in rows:
            if project_uuid is not None and program.project_id!=project_uuid: continue
            try: await self._assert_entity_access(session,actor,program,permission="read")
            except Exception: continue
            revision=await self._latest_revision(session,program.id)
            if revision is None: continue
            trigger=dict(revision.trigger_json or {}); trigger_type=str(trigger.get("type") or "manual").strip().lower()
            if trigger_type not in {"heartbeat","internal"}: continue
            period=str(trigger.get("dedupe_period") or "daily").strip().lower()
            if period=="minute": token=now.strftime("%Y-%m-%dT%H:%M")
            elif period=="hourly": token=now.strftime("%Y-%m-%dT%H")
            else: period="daily"; token=now.strftime("%Y-%m-%d")
            trigger_key=f"internal:{period}:{token}"
            execution_key=sha256_json({"program_id":str(program.id),"revision_id":str(revision.id),"trigger_key":trigger_key})
            existing=await self._scalar(session,select(AutomationRun.id).where(AutomationRun.execution_key==execution_key).limit(1))
            if existing is not None: continue
            result.append({**program.to_safe_dict(),"current_revision":revision.to_safe_dict(),"trigger_key":trigger_key,"next_due_at":now.isoformat()})
        return result

    async def trigger_program(self,session:Any|None=None,actor:Any|None=None,program_id:UUID|str|None=None,*,trigger_key:Any,trigger_kind:str="manual",execute:bool=True)->dict[str,Any]:
        session=self._resolve_session(session)
        if actor is None or program_id is None: raise MediaOperationsValidationError("actor and program_id are required")
        program=await self._get(session,AutomationProgram,program_id,"program_id"); actor_id=await self._assert_entity_access(session,actor,program,permission="write")
        if not program.enabled and trigger_kind!="manual": raise MediaOperationsConflictError("Automation Program is disabled")
        revision=await self._latest_revision(session,program.id)
        if revision is None: raise MediaOperationsConflictError("Automation Program revision history is incomplete")
        trigger_value=_required_text(trigger_key,"trigger_key",255); execution_key=sha256_json({"program_id":str(program.id),"revision_id":str(revision.id),"trigger_key":trigger_value}); existing=await self._scalar(session,select(AutomationRun).where(AutomationRun.execution_key==execution_key).limit(1))
        if existing is not None: await self._assert_entity_access(session,actor,existing,permission="read"); return existing.to_safe_dict()
        run=AutomationRun(id=uuid4(),program_id=program.id,program_revision_id=revision.id,owner_user_id=program.owner_user_id,project_id=program.project_id,execution_key=execution_key,trigger_kind=str(trigger_kind or "manual")[:32],state="scheduled",correlation_id=f"aoi-auto-{execution_key[:24]}",created_by=actor_id); session.add(run)
        try: await self._flush_commit(session)
        except IntegrityError:
            await self._rollback(session); recovered=await self._scalar(session,select(AutomationRun).where(AutomationRun.execution_key==execution_key).limit(1))
            if recovered is not None: return recovered.to_safe_dict()
            raise
        return await self.execute_run(session,actor,run.id) if execute else run.to_safe_dict()

    async def execute_run(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None)->dict[str,Any]:
        session=self._resolve_session(session)
        if actor is None or run_id is None: raise MediaOperationsValidationError("actor and run_id are required")
        run=await self._get(session,AutomationRun,run_id,"run_id",for_update=True); await self._assert_entity_access(session,actor,run,permission="write")
        if run.state in _TERMINAL or run.state in {"waiting_review","generation_running","uncertain"}: return run.to_safe_dict()
        program=await self._get(session,AutomationProgram,run.program_id,"program_id"); revision=await self._get(session,AutomationProgramRevision,run.program_revision_id,"program_revision_id")
        try:
            if not run.observation_json:
                run.state="theme_discovery"; run.started_at=run.started_at or datetime.utcnow(); await self._flush_commit(session)
                try:
                    run.observation_json=await self._discover(revision,run)
                except Exception:
                    fallback=dict(revision.fallback_json or {})
                    fallback_theme=fallback.get("theme") or fallback.get("manual_theme")
                    if not fallback_theme:
                        raise
                    title=_required_text(fallback_theme,"fallback.theme",500)
                    run.observation_json={"title":title,"description":"Fallback theme","source_type":"fallback_manual","source_url":None,"published_at":None,"retrieved_at":datetime.now(timezone.utc).isoformat(),"checksum":sha256_json({"fallback":title}),"fallback_used":True}
                await self._flush_commit(session)
            if not run.research_brief_json:
                run.state="research"; await self._flush_commit(session)
                try:
                    research_id,brief=await self._run_research(session,actor,revision,run)
                except Exception:
                    fallback=dict(revision.fallback_json or {})
                    if str(fallback.get("research_policy") or "").lower() not in {"observation_only","theme_only"}:
                        raise
                    observation=run.observation_json or {}
                    research_id=None; brief={"key_facts":[observation.get("description") or observation.get("title")],"semantic_ideas":[observation.get("title")],"visual_ideas":[observation.get("description") or observation.get("title")],"materials":[],"colors":[],"cultural_historical_context":[],"current_trending_context":[],"source_refs":[observation.get("source_url")] if observation.get("source_url") else [],"fallback_used":"observation_only"}
                run.research_run_id=_as_uuid(research_id,"research_run_id",required=False) if research_id else None; run.state="brief"; run.research_brief_json=_safe_json(brief,"research_brief"); await self._flush_commit(session)
            if revision.execution_mode=="research_only": run.state="complete"; run.completed_at=datetime.utcnow(); await self._flush_commit(session); return run.to_safe_dict()
            if not run.candidates_json:
                run.state="concept_planning"; await self._flush_commit(session); planning=revision.planning_policy_json or {}; policy=str(planning.get("novelty_policy") or "balanced"); snapshot=await self._novelty_snapshot(session,program,policy); run.novelty_snapshot_json=snapshot; run.state="prompt_planning"; await self._flush_commit(session)
                action=revision.generation_action_json or {}; values=await self.planner.plan(observation=run.observation_json or {},research_brief=run.research_brief_json or {},novelty_snapshot=snapshot,planning_policy=planning,action_kind=str(action.get("kind") or "image")); candidates=[]
                for index,value in enumerate(values[:int(planning.get("candidate_count") or 3)]):
                    payload=_safe_json(dict(value),f"candidate[{index}]"); cid="cand_"+sha256_json({"run":str(run.id),"index":index,"payload":payload})[:24]; candidates.append({"candidate_id":cid,"status":"draft","payload":payload})
                if not candidates: raise RuntimeError("automation_planner_returned_no_candidates")
                run.candidates_json=candidates; await self._flush_commit(session)
            if revision.execution_mode=="draft": run.state="complete"; run.completed_at=datetime.utcnow(); await self._flush_commit(session); return run.to_safe_dict()
            if revision.execution_mode=="review_before_generate": run.state="waiting_review"; await self._flush_commit(session); return run.to_safe_dict()
            return await self._submit_candidate(session,actor,run,revision,(run.candidates_json or [])[0]["candidate_id"])
        except Exception as exc:
            if run.state=="generation_submitting": run.state="uncertain"; run.error_code="generation_outcome_uncertain"
            else: run.state="failed"; run.error_code=re.sub(r"[^a-z0-9_]+","_",type(exc).__name__.lower())[:128] or "automation_failed"; run.completed_at=datetime.utcnow()
            run.error_message=str(exc)[:1000] if isinstance(exc,MediaOperationsValidationError) else run.error_code; await self._flush_commit(session); return run.to_safe_dict()


    def _candidate(self,run:AutomationRun,candidate_id:str)->Mapping[str,Any]:
        for candidate in run.candidates_json or []:
            if isinstance(candidate,Mapping) and candidate.get("candidate_id")==candidate_id: return candidate
        raise MediaOperationsNotFoundError("candidate_id not found")

    async def edit_candidate(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None,*,candidate_id:str,payload:Mapping[str,Any])->dict[str,Any]:
        session=self._resolve_session(session); run=await self._get(session,AutomationRun,run_id,"run_id",for_update=True); await self._assert_entity_access(session,actor,run,permission="write")
        if run.state not in {"waiting_review","complete"}: raise MediaOperationsConflictError("candidate can be edited only before generation")
        values=[]; found=False
        for candidate in run.candidates_json or []:
            item=dict(candidate)
            if item.get("candidate_id")==candidate_id: item["payload"]=_safe_json(dict(payload),"candidate.payload"); item["status"]="edited"; found=True
            values.append(item)
        if not found: raise MediaOperationsNotFoundError("candidate_id not found")
        run.candidates_json=values; await self._flush_commit(session); return run.to_safe_dict()

    async def regenerate_candidates(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None,*,idempotency_key:Any)->dict[str,Any]:
        session=self._resolve_session(session); run=await self._get(session,AutomationRun,run_id,"run_id",for_update=True); await self._assert_entity_access(session,actor,run,permission="write")
        if run.state!="waiting_review" or run.generation_request_hash:
            raise MediaOperationsConflictError("candidates can be regenerated only before a generation intent exists")
        key=_idempotency_key(idempotency_key); ledger=dict(run.novelty_snapshot_json or {}); receipts=list(ledger.get("regeneration_keys") or [])
        if key in receipts: return run.to_safe_dict()
        revision=await self._get(session,AutomationProgramRevision,run.program_revision_id,"program_revision_id"); program=await self._get(session,AutomationProgram,run.program_id,"program_id")
        previous=[item.get("payload") for item in run.candidates_json or [] if isinstance(item,Mapping) and isinstance(item.get("payload"),Mapping)]
        planning=revision.planning_policy_json or {}; policy=str(planning.get("novelty_policy") or "balanced"); snapshot=await self._novelty_snapshot(session,program,policy)
        snapshot["regeneration_avoid"]=[{k:v for k,v in item.items() if k in {"concept","outfit","pose","composition","environment","palette","prompt"}} for item in previous[:12]]
        values=await self.planner.plan(observation=run.observation_json or {},research_brief=run.research_brief_json or {},novelty_snapshot=snapshot,planning_policy=planning,action_kind=str((revision.generation_action_json or {}).get("kind") or "image")); candidates=[]
        for index,value in enumerate(values[:int(planning.get("candidate_count") or 3)]):
            payload=_safe_json(dict(value),f"candidate[{index}]"); cid="cand_"+sha256_json({"run":str(run.id),"regeneration_key":key,"index":index,"payload":payload})[:24]; candidates.append({"candidate_id":cid,"status":"draft","payload":payload})
        if not candidates: raise RuntimeError("automation_planner_returned_no_candidates")
        snapshot["regeneration_keys"]=(receipts+[key])[-20:]; run.novelty_snapshot_json=snapshot; run.candidates_json=candidates; await self._flush_commit(session); return run.to_safe_dict()

    async def approve_candidate(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None,*,candidate_id:str)->dict[str,Any]:
        session=self._resolve_session(session); run=await self._get(session,AutomationRun,run_id,"run_id",for_update=True); await self._assert_entity_access(session,actor,run,permission="write")
        if run.state!="waiting_review": raise MediaOperationsConflictError("Automation Run is not waiting for review")
        revision=await self._get(session,AutomationProgramRevision,run.program_revision_id,"program_revision_id"); return await self._submit_candidate(session,actor,run,revision,candidate_id)

    async def _submit_candidate(self,session:Any,actor:Any,run:AutomationRun,revision:AutomationProgramRevision,candidate_id:str,*,generation_index:int=1,allow_uncertain_retry:bool=False)->dict[str,Any]:
        candidate=self._candidate(run,candidate_id); action=dict(revision.generation_action_json or {})
        if str(action.get("kind") or "image")!="image": raise MediaOperationsValidationError("this Generation Studio binding currently supports image execution only")
        workspace=await self._get(session,GenerationWorkspace,action["studio_workspace_binding_id"],"studio_workspace_binding_id"); await self._assert_entity_access(session,actor,workspace,permission="read")
        payload=candidate.get("payload") if isinstance(candidate.get("payload"),Mapping) else {}; prompt=_required_text(payload.get("prompt"),"candidate.prompt",8000); negative=_optional_text(payload.get("negative_prompt_additions"),"candidate.negative_prompt_additions",8000) or ""
        preset={"preset_id":action["preset_id"],"revision_policy":action.get("preset_revision_policy","latest_at_submission")}
        if preset["revision_policy"]=="pinned": preset["preset_revision_id"]=action["preset_revision_id"]
        external_key=f"aoi:auto:{run.id}:{candidate_id}:image:{generation_index}"
        request={"preset":preset,"prompt_overlay":{"positive":prompt,"negative_additions":negative},"provenance":{"origin_system":"aoi_talk","automation_run_id":str(run.id),"candidate_id":candidate_id,"correlation_id":run.correlation_id},"idempotency_key":external_key}; request_hash=sha256_json(request)
        if run.generation_request_hash:
            if run.generation_request_hash!=request_hash: raise MediaOperationsConflictError("generation intent payload changed after durable submission intent")
            if run.state=="uncertain" and not allow_uncertain_retry: return run.to_safe_dict()
        else:
            run.generation_request_json=_safe_json(request,"generation_request"); run.generation_request_hash=request_hash; run.external_idempotency_key=external_key; run.selected_candidate_id=candidate_id; run.state="generation_submitting"; await self._flush_commit(session)
        scope=StudioScope(workspace.external_workspace_id,workspace.external_project_id,workspace.base_url)
        try: receipt=await self.adapter.submit_preset_image(scope,request)
        except Exception:
            run.state="uncertain"; run.error_code="generation_outcome_uncertain"; await self._flush_commit(session); return run.to_safe_dict()
        status=str(receipt.get("status") or "uncertain").lower() if isinstance(receipt,Mapping) else "uncertain"; external_run_id=receipt.get("run_id") if isinstance(receipt,Mapping) else None
        if not isinstance(external_run_id,str) or not external_run_id.startswith("run_"):
            run.state="uncertain"; run.error_code="generation_receipt_invalid"; await self._flush_commit(session); return run.to_safe_dict()
        run.external_run_id=external_run_id; run.preset_id=str(receipt.get("preset_id") or action["preset_id"]); run.preset_revision_id=str(receipt.get("preset_revision_id") or "") or None
        try: run.preset_revision_number=int(receipt.get("revision_number"))
        except (TypeError,ValueError): run.preset_revision_number=None
        checksum=str(receipt.get("checksum") or ""); run.preset_checksum=checksum if re.fullmatch(r"[0-9a-f]{64}",checksum) else None; run.result_deep_link=_optional_text(receipt.get("result_deep_link"),"result_deep_link",2000)
        if status in {"succeeded","complete","completed","success"}: run.state="complete"; run.completed_at=datetime.utcnow()
        elif status in {"failed","cancelled","unavailable"}: run.state="failed"; run.completed_at=datetime.utcnow(); run.error_code=str(receipt.get("reason_code") or receipt.get("error_code") or "generation_failed")[:128]
        else: run.state="generation_running"
        await self._flush_commit(session); return run.to_safe_dict()

    async def retry_uncertain_generation(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None)->dict[str,Any]:
        session=self._resolve_session(session); run=await self._get(session,AutomationRun,run_id,"run_id",for_update=True); await self._assert_entity_access(session,actor,run,permission="write")
        if run.state!="uncertain": raise MediaOperationsConflictError("Automation Run is not uncertain")
        revision=await self._get(session,AutomationProgramRevision,run.program_revision_id,"program_revision_id")
        if not run.selected_candidate_id: raise MediaOperationsConflictError("uncertain run has no durable candidate intent")
        return await self._submit_candidate(session,actor,run,revision,run.selected_candidate_id,allow_uncertain_retry=True)

    async def reconcile_run(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None)->dict[str,Any]:
        session=self._resolve_session(session); run=await self._get(session,AutomationRun,run_id,"run_id",for_update=True); await self._assert_entity_access(session,actor,run,permission="write")
        if not run.external_run_id: return run.to_safe_dict()
        revision=await self._get(session,AutomationProgramRevision,run.program_revision_id,"program_revision_id"); action=revision.generation_action_json or {}; workspace=await self._get(session,GenerationWorkspace,action["studio_workspace_binding_id"],"studio_workspace_binding_id"); scope=StudioScope(workspace.external_workspace_id,workspace.external_project_id,workspace.base_url)
        try: receipt=await self.adapter.get_preset_run(scope,run.external_run_id)
        except Exception: return run.to_safe_dict()
        status=str(receipt.get("status") or "uncertain").lower()
        if status in {"succeeded","complete","completed","success"}: run.state="complete"; run.completed_at=datetime.utcnow()
        elif status in {"failed","cancelled","quarantined","unavailable"}: run.state="failed"; run.completed_at=datetime.utcnow(); run.error_code=str(receipt.get("error_code") or "generation_failed")[:128]
        else: run.state="generation_running"
        link=receipt.get("result_deep_link")
        if isinstance(link,str) and len(link)<=2000: run.result_deep_link=link
        run.generation_result_json=_safe_json({
            "status": status,
            "output_asset_ids": list(receipt.get("output_asset_ids") or [])[:20],
            "output_version_ids": list(receipt.get("output_version_ids") or [])[:20],
            "outputs": list(receipt.get("outputs") or [])[:20],
            "result_deep_link": run.result_deep_link,
        },"generation_result")
        await self._flush_commit(session); return run.to_safe_dict()

    async def list_runs(self,session:Any|None=None,actor:Any|None=None,*,program_id:UUID|str|None=None,project_id:UUID|str|None=None,limit:int=100)->list[dict[str,Any]]:
        session=self._resolve_session(session)
        if actor is None: raise MediaOperationsValidationError("actor is required")
        statement=select(AutomationRun).order_by(AutomationRun.created_at.desc()).limit(max(1,min(int(limit),200)))
        if program_id is not None: statement=statement.where(AutomationRun.program_id==_as_uuid(program_id,"program_id"))
        if project_id is not None: statement=statement.where(AutomationRun.project_id==_as_uuid(project_id,"project_id"))
        rows=await self._scalars(session,statement); out=[]
        for row in rows:
            try: await self._assert_entity_access(session,actor,row,permission="read")
            except Exception: continue
            out.append(row.to_safe_dict())
        return out

    async def get_run(self,session:Any|None=None,actor:Any|None=None,run_id:UUID|str|None=None)->dict[str,Any]:
        session=self._resolve_session(session)
        if actor is None or run_id is None: raise MediaOperationsValidationError("actor and run_id are required")
        row=await self._get(session,AutomationRun,run_id,"run_id"); await self._assert_entity_access(session,actor,row,permission="read"); return row.to_safe_dict()
