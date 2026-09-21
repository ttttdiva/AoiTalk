"""Trusted Operations Kernel v1 persistence models.

The operations tables deliberately model *evidence* and immutable versions.  A
credential reference may be supplied to the connection service, but it is not
part of any public projection.  Evaluations, drafts, artifacts and event rows
are append-only from the service's point of view; changing a proposal creates
another version instead of mutating history.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


def _uuid(value: Any) -> str | None:
    return str(value) if value is not None else None


_SENSITIVE_PROVENANCE_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "access_token",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passphrase",
    "raw_source",
    "refresh_token",
    "secret",
    "source_text",
    "token",
    # Operations provenance is intentionally strict about body/credential
    # labels, while still using structural token matching (rather than
    # substring matching) so ordinary keys such as ``author`` survive.
    "browser_context",
    "browser_data",
    "browser_profile",
    "browser_state",
    "external_body",
    "application_message",
    "response_body",
}

# Source URLs are untrusted metadata.  Keep this list deliberately conservative:
# a query parameter that looks like a credential, bearer token, signature, or
# session identifier must never be echoed from a legacy row into a safe DTO.
_SENSITIVE_QUERY_KEY_MARKERS = {
    "apikey",
    "auth",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passphrase",
    "secret",
    "token",
    "signature",
    "sig",
    "access_token",
    "refresh_token",
    "client_secret",
}

# Short query names are security-sensitive only when their normalized form is
# exactly one of these values.  They intentionally do not use the broader
# marker-substring check below: ``postal_code``, ``status_code``, ``sort_key``,
# ``monkey`` and ``keyboard`` must remain ordinary public query names.
_EXACT_SENSITIVE_QUERY_KEYS = {
    "session",
    "session_id",
    "sessionid",
    "session_key",
    "sessionkey",
    "key",
    "jwt",
    "code",
    "state",
    "nonce",
    "oauth_code",
    "oauthcode",
    "oauth_state",
    "oauthstate",
    "oauth_nonce",
    "oauthnonce",
    "oauth2_code",
    "oauth2code",
    "oauth2_state",
    "oauth2state",
    "oauth2_nonce",
    "oauth2nonce",
    "auth_code",
    "authcode",
    "authorization_code",
    "authorizationcode",
    "oidc_code",
    "oidccode",
    "oidc_state",
    "oidcstate",
    "oidc_nonce",
    "oidcnonce",
    "relaystate",
    "relay_state",
    "ticket",
    "sid",
    "bearer",
    "assertion",
    "samlresponse",
    "saml_response",
}

_MAX_QUERY_KEY_DECODE_ROUNDS = 8
_MAX_NESTED_URL_DEPTH = 4
_HTTP_URL_PREFIX_RE = re.compile(r"^https?://", re.IGNORECASE)

# These are matched as complete separator-delimited tokens.  Do not turn the
# policy into a substring search: ``author``, ``tokenizer``, ``postal_code``,
# ``status_code``, ``sort_key``, ``monkey`` and ``keyboard`` are ordinary
# query/provenance keys and must remain safe to expose.
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "secret",
        "token",
        "signature",
        "sig",
    }
)
_SENSITIVE_KEY_PHRASES = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "client_secret",
        "clientsecret",
    }
)


def _is_http_url_candidate(value: Any) -> bool:
    """Return whether a scalar is a complete, absolute HTTP(S) candidate.

    URL-like provenance values are deliberately narrower than arbitrary text:
    leading/trailing whitespace is tolerated, while any internal whitespace
    makes the value ordinary prose instead of a URL candidate.
    """

    if not isinstance(value, str):
        return False
    rendered = value.strip()
    if not rendered or any(char.isspace() for char in rendered):
        return False
    return _HTTP_URL_PREFIX_RE.match(rendered) is not None


def _decode_security_key(value: Any, *, plus_as_space: bool = False) -> str | None:
    """Decode a security-sensitive key repeatedly, failing closed after eight rounds.

    ``parse_qsl`` performs one implicit decode, which makes nested encodings
    easy to miss.  Query components are passed here in their raw form instead;
    callers opt into ``+`` query-space semantics with ``plus_as_space`` while
    provenance keys leave literal plus signs untouched.
    """

    current = str(value)
    if plus_as_space:
        current = current.replace("+", " ")
    for _ in range(_MAX_QUERY_KEY_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    # A ninth decode would still change the key.  Do not attempt to guess its
    # eventual meaning; the whole URL is unsafe.
    return None if unquote(current) != current else current


# Kept as a private compatibility alias for existing callers/tests.
_decode_query_key = _decode_security_key


def _normalized_security_key(value: Any) -> tuple[str, str]:
    """Return separator-preserving and compact forms of a user key."""

    # Preserve word boundaries before case-folding.  Lower-casing first would
    # collapse camelCase and acronym boundaries (``sourceText``/``APIKey``),
    # allowing credential-shaped aliases to evade the structural classifier.
    rendered = str(value).strip()
    rendered = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", rendered)
    rendered = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", rendered)
    rendered = rendered.casefold()
    separated = re.sub(r"[^a-z0-9]+", "_", rendered).strip("_")
    return separated, separated.replace("_", "")


def _classify_security_key(
    value: Any,
    *,
    provenance: bool = False,
    plus_as_space: bool = False,
) -> bool:
    """Classify a decoded key using exact aliases and structural tokens."""

    decoded = _decode_security_key(value, plus_as_space=plus_as_space)
    # An encoded key that remains unstable after the bounded budget cannot be
    # trusted.  Callers use this fail-closed result for both writes and legacy
    # safe projections.
    if decoded is None:
        return True
    separated, compact = _normalized_security_key(decoded)
    if not separated:
        return False
    exact = _EXACT_SENSITIVE_QUERY_KEYS
    if provenance:
        exact = exact | _SENSITIVE_PROVENANCE_KEYS_NORMALIZED
    if separated in exact or compact in exact:
        return True
    tokens = set(separated.split("_"))
    if tokens.intersection(_SENSITIVE_KEY_TOKENS):
        return True
    # Phrase matching is separator-aware.  ``api_key`` and
    # ``provider_api_key`` are protected, while ``api_version`` remains an
    # ordinary key.
    compact_phrases = {phrase.replace("_", "") for phrase in _SENSITIVE_KEY_PHRASES}
    if compact in compact_phrases:
        return True
    token_list = separated.split("_")
    for phrase in _SENSITIVE_KEY_PHRASES:
        phrase_tokens = phrase.split("_")
        width = len(phrase_tokens)
        if any(
            token_list[index : index + width] == phrase_tokens
            for index in range(len(token_list) - width + 1)
        ):
            return True
    return False


_SENSITIVE_PROVENANCE_KEYS_NORMALIZED = frozenset(
    _normalized_security_key(key)[0]
    for key in _SENSITIVE_PROVENANCE_KEYS
)


def _is_sensitive_query_key(value: Any) -> bool:
    return _classify_security_key(value, plus_as_space=True)


def _decoded_query_value(value: Any) -> str | None:
    """Decode a query value with the same bounded budget used for keys."""

    current = str(value)
    for _ in range(_MAX_QUERY_KEY_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    return None if unquote(current) != current else current


def _query_value_has_unsafe_nested_data(value: Any, *, depth: int) -> bool:
    """Inspect only full nested URLs or boundary-delimited key/value text.

    Query values are retained byte-for-byte in the outer URL.  We decode a
    bounded copy solely for security inspection; ordinary prose and arbitrary
    values are never rewritten.  Nested absolute HTTP(S) URLs are checked
    recursively up to ``_MAX_NESTED_URL_DEPTH``.
    """

    decoded = _decoded_query_value(value)
    if decoded is None:
        # We cannot determine whether an unstable value hides a protected key
        # or nested URL, so fail closed.
        return True
    if _is_http_url_candidate(decoded):
        if depth >= _MAX_NESTED_URL_DEPTH:
            return True
        try:
            if urlsplit(decoded).fragment:
                return True
        except (TypeError, ValueError):
            return True
        return sanitize_source_url(decoded, _depth=depth + 1) is None

    # A query-like value may itself contain an encoded query string.  Inspect
    # keys only at the beginning or immediately after ?, &, or ; so words such
    # as ``author`` and ``tokenizer`` remain ordinary values.
    for match in re.finditer(r"(?:^|[?&;])([^=?&#;]+)=([^&#;]*)", decoded):
        raw_key = match.group(1)
        if _is_sensitive_query_key(raw_key):
            return True
        nested_value = match.group(2)
        if nested_value and _is_http_url_candidate(nested_value):
            if depth >= _MAX_NESTED_URL_DEPTH:
                return True
            try:
                if urlsplit(nested_value).fragment:
                    return True
            except (TypeError, ValueError):
                return True
            if sanitize_source_url(nested_value, _depth=depth + 1) is None:
                return True
    return False


def sanitize_source_url(value: Any, *, _depth: int = 0) -> str | None:
    """Return a safe HTTP(S) URL, or ``None`` for an unsafe legacy value.

    New rows are validated by the service before persistence.  This helper is
    also used by model projections so old rows containing userinfo or query
    secrets fail closed instead of leaking through read APIs or Agent tools.
    Fragments are never durable because they are client-side state rather than
    provider identity.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return None
    # A URL with literal whitespace is malformed/ambiguous.  Callers that
    # hold ordinary prose should not route it through this public URL policy.
    if any(char.isspace() for char in text):
        return None
    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.hostname is None:
            return None
        # Accessing username/password also validates malformed authority/port
        # components for urllib's split result.
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
        # Inspect raw query keys so nested percent encodings cannot evade the
        # repeated-decoding policy.  Values are retained byte-for-byte in the
        # returned URL, but full nested URLs and boundary-delimited query
        # fragments are checked on a bounded decoded copy.
        for component in re.split(r"[&;]", parsed.query):
            raw_key = component.split("=", 1)[0]
            if _is_sensitive_query_key(raw_key):
                return None
            if "=" in component:
                raw_value = component.split("=", 1)[1]
                if _query_value_has_unsafe_nested_data(raw_value, depth=_depth):
                    return None
    except (TypeError, ValueError):
        return None
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


DEFAULT_OPPORTUNITY_TITLE = "Untitled opportunity"


def _canonical_url_signature(value: Any) -> str | None:
    """Canonicalize only scheme/fragment for legacy title comparison."""

    if not isinstance(value, str):
        return None
    rendered = value.strip()
    if not rendered or any(char.isspace() for char in rendered):
        return None
    try:
        parsed = urlsplit(rendered)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, "")
    )


def default_opportunity_title(source_url: Any = None) -> str:
    """Return the canonical URL title used when no title was supplied."""

    return sanitize_source_url(source_url) or DEFAULT_OPPORTUNITY_TITLE


def safe_opportunity_title(
    title: Any,
    source_url: Any = None,
    source_text: Any = None,
) -> str:
    """Project an opportunity title without leaking legacy URL metadata.

    Older rows used the raw source URL as an automatically generated title.
    Detect that exact/canonical signature and replace it with the fragmentless
    safe URL (or the neutral fallback when the source URL is unsafe).  A small
    number of older source-text-only rows also copied their first URL line into
    the title; that remediation is applied only when no source URL exists and
    the title exactly matches that first non-empty line.  Titles that do not
    match either signature are user-authored text and are returned byte-for-
    byte, including surrounding whitespace.
    """

    if title is None:
        return default_opportunity_title(source_url)
    if not isinstance(title, str):
        return DEFAULT_OPPORTUNITY_TITLE
    rendered_title = title.strip()
    if not rendered_title:
        return default_opportunity_title(source_url)

    source_url_text = source_url.strip() if isinstance(source_url, str) else ""
    if source_url_text and rendered_title == source_url_text:
        return default_opportunity_title(source_url)

    # A canonical comparison catches legacy rows where the title included a
    # client fragment or used an upper-case HTTP scheme while storage held the
    # normalized source URL.  ``sanitize_source_url`` also fails closed for
    # unsafe source/title signatures.
    safe_source = sanitize_source_url(source_url)
    canonical_title = sanitize_source_url(rendered_title)
    if canonical_title is not None and safe_source is not None and canonical_title == safe_source:
        return canonical_title

    # Preserve the same legacy detection even when the source/title URL is no
    # longer safe to expose (for example a token-bearing query).  The
    # comparison intentionally strips only the fragment and canonicalizes the
    # scheme; it does not bless the unsafe URL for output.
    source_signature = _canonical_url_signature(source_url)
    title_signature = _canonical_url_signature(rendered_title)
    if source_signature is not None and source_signature == title_signature:
        return safe_source or DEFAULT_OPPORTUNITY_TITLE

    # Source-text-only ingestion may have derived the title from the first
    # non-empty source line.  Remediate that legacy signature only when the
    # source URL is genuinely absent and the line is itself a complete HTTP(S)
    # candidate.  Never expose the source text; only the sanitized URL (or the
    # neutral fallback) is returned.
    if not source_url_text and isinstance(source_text, str):
        first_line = next(
            (line.strip() for line in source_text.splitlines() if line.strip()),
            "",
        )
        if first_line and rendered_title == first_line and _is_http_url_candidate(first_line):
            return default_opportunity_title(first_line)

    # Do not reinterpret unrelated custom titles.
    return title


def _safe_provenance(value: Any) -> Any:
    """Recursively remove protected bodies/secrets from public provenance."""

    if isinstance(value, dict):
        return {
            str(key): _safe_provenance(item)
            for key, item in value.items()
            if not _is_sensitive_provenance_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_safe_provenance(item) for item in value]
    if _is_http_url_candidate(value):
        # Legacy rows may predate URL validation.  Keep safe URLs canonical and
        # fail closed for URL-like values that contain userinfo/secrets/etc.
        return sanitize_source_url(value)
    return value


def _is_sensitive_provenance_key(value: Any) -> bool:
    return _classify_security_key(value, provenance=True, plus_as_space=False)


def _safe_action_payload(value: Any) -> dict[str, Any]:
    """Preserve exact proposal fields while whitelisting connection identity."""

    if not isinstance(value, dict):
        return {}
    payload = dict(value)
    connection = payload.get("connection")
    if isinstance(connection, dict):
        try:
            version = int(connection.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        payload["connection"] = {
            "id": _uuid(connection.get("id")),
            "version": version,
            "provider_key": connection.get("provider_key"),
            "remote_account_ref": connection.get("remote_account_ref"),
        }
    elif "connection" in payload:
        payload.pop("connection", None)
    # Credential state hashes are server-side authority evidence.  They are
    # not secrets, but omitting them from ordinary action projections avoids
    # turning a replay token into a public DTO; execution rechecks the live
    # value against the immutable capability snapshot.
    payload.pop("credential_state_hash", None)
    return payload


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class _OperationModel:
    """Small compatibility aliases shared by operation entities."""

    @property
    def owner_id(self):
        return getattr(self, "owner_user_id", None)

    @owner_id.setter
    def owner_id(self, value):
        self.owner_user_id = value


class ExternalConnection(_OperationModel, Base):
    """A provider/account binding without serialized credentials."""

    __tablename__ = "external_connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    provider_key = Column(String(120), nullable=False)
    display_name = Column(String(255), nullable=False)
    remote_account_ref = Column(String(255), nullable=True)
    # This value is an opaque server-side reference (for example a key in an
    # OS vault), never a token/password.  It must not be returned by to_dict.
    credential_ref = Column(Text, nullable=True)
    auth_status = Column(String(32), nullable=False, default="unknown", server_default="unknown")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    metadata_json = Column("metadata", JSON, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_external_connections_version_positive"),
        UniqueConstraint("owner_user_id", "project_id", "provider_key", "remote_account_ref", name="uq_external_connections_account"),
        Index("ix_external_connections_owner_project", "owner_user_id", "project_id"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "provider_key": self.provider_key,
            "display_name": self.display_name,
            "remote_account_ref": self.remote_account_ref,
            "auth_status": self.auth_status,
            "version": int(self.version or 1),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class ArtifactVersion(_OperationModel, Base):
    """Immutable content-addressed artifact metadata."""

    __tablename__ = "artifact_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    filename = Column(String(512), nullable=True)
    sha256 = Column(String(64), nullable=False, index=True)
    size_bytes = Column(Integer, nullable=False)
    mime_type = Column(String(255), nullable=False)
    storage_ref = Column(Text, nullable=True)
    provenance_json = Column("provenance", JSON, nullable=False, default=dict, server_default="{}")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_artifact_versions_size_nonnegative"),
        CheckConstraint("length(sha256) = 64", name="ck_artifact_versions_sha256_length"),
        Index("ix_artifact_versions_owner_project", "owner_user_id", "project_id"),
        Index(
            "uq_artifact_versions_personal_content",
            "owner_user_id",
            "sha256",
            "size_bytes",
            "mime_type",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_artifact_versions_project_content",
            "owner_user_id",
            "project_id",
            "sha256",
            "size_bytes",
            "mime_type",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
    )

    @property
    def size(self):
        return self.size_bytes

    @size.setter
    def size(self, value):
        self.size_bytes = value

    def to_safe_dict(self) -> Dict[str, Any]:
        # Do not expose storage_ref: it can be an absolute host path.  The
        # content hash is the stable public identity of an artifact.
        provenance = self.provenance_json if isinstance(self.provenance_json, dict) else {}
        safe_provenance = _safe_provenance(provenance)
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": int(self.size_bytes or 0),
            "mime_type": self.mime_type,
            "provenance": safe_provenance,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class EngagementOpportunity(_OperationModel, Base):
    """An externally sourced opportunity; source fields are untrusted."""

    __tablename__ = "opportunities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    connection_id = Column(
        UUID(as_uuid=True), ForeignKey("external_connections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title = Column(String(500), nullable=False)
    source_url = Column(Text, nullable=True)
    # Source text is intentionally not included in safe projections.  It is
    # retained only to make a deterministic source snapshot/hash available.
    source_text = Column(Text, nullable=True)
    source_snapshot_hash = Column(String(64), nullable=True, index=True)
    source_snapshot_json = Column("source_snapshot", JSON, nullable=False, default=dict, server_default="{}")
    status = Column(String(32), nullable=False, default="open", server_default="open", index=True)
    metadata_json = Column("metadata", JSON, nullable=False, default=dict, server_default="{}")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("length(source_snapshot_hash) = 64 OR source_snapshot_hash IS NULL", name="ck_opportunities_snapshot_hash_length"),
        Index("ix_opportunities_owner_project", "owner_user_id", "project_id"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "connection_id": _uuid(self.connection_id),
            "title": safe_opportunity_title(self.title, self.source_url, self.source_text),
            "source_url": sanitize_source_url(self.source_url),
            "source_snapshot_hash": self.source_snapshot_hash,
            "source_untrusted": True,
            "status": self.status,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class OpportunityEvaluation(_OperationModel, Base):
    """Immutable, versioned evaluation of an opportunity."""

    __tablename__ = "opportunity_evaluations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id = Column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    version = Column(Integer, nullable=False)
    estimated_effort_hours = Column(Float, nullable=True)
    estimated_cost = Column(Float, nullable=True)
    estimated_revenue = Column(Float, nullable=True)
    fit = Column(String(32), nullable=True)
    risks = Column(JSON, nullable=False, default=list, server_default="[]")
    missing_requirements = Column(JSON, nullable=False, default=list, server_default="[]")
    summary = Column(Text, nullable=True)
    evidence_refs = Column(JSON, nullable=False, default=list, server_default="[]")
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_opportunity_evaluations_version_positive"),
        UniqueConstraint("opportunity_id", "version", name="uq_opportunity_evaluations_version"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "opportunity_id": _uuid(self.opportunity_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version or 1),
            "estimated_effort_hours": self.estimated_effort_hours,
            "estimated_cost": self.estimated_cost,
            "estimated_revenue": self.estimated_revenue,
            "fit": self.fit,
            "risks": list(self.risks or []),
            "missing_requirements": list(self.missing_requirements or []),
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs or []),
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ApplicationDraft(_OperationModel, Base):
    """Immutable, versioned application message and offer."""

    __tablename__ = "application_drafts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id = Column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    version = Column(Integer, nullable=False)
    message = Column(Text, nullable=False)
    offered_price = Column(Float, nullable=True)
    currency = Column(String(16), nullable=True)
    delivery_estimate = Column(String(255), nullable=True)
    artifact_version_ids = Column(JSON, nullable=False, default=list, server_default="[]")
    draft_hash = Column(String(64), nullable=False, index=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_application_drafts_version_positive"),
        CheckConstraint("length(draft_hash) = 64", name="ck_application_drafts_hash_length"),
        UniqueConstraint("opportunity_id", "version", name="uq_application_drafts_version"),
    )

    @property
    def artifact_ids(self):
        return self.artifact_version_ids

    @artifact_ids.setter
    def artifact_ids(self, value):
        self.artifact_version_ids = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "opportunity_id": _uuid(self.opportunity_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "version": int(self.version or 1),
            "message": self.message,
            "offered_price": self.offered_price,
            "currency": self.currency,
            "delivery_estimate": self.delivery_estimate,
            "artifact_version_ids": [_uuid(item) or str(item) for item in (self.artifact_version_ids or [])],
            "draft_hash": self.draft_hash,
            "created_by": _uuid(self.created_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ExternalAction(_OperationModel, Base):
    """A proposed external engagement action with optimistic versioning."""

    __tablename__ = "external_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    # Engagement actions bind to an Opportunity and ApplicationDraft.  Media
    # actions use the same trusted action/approval/attempt/receipt kernel but
    # bind to the immutable MediaOps rows below instead, so the legacy fields
    # must remain nullable for those rows.
    opportunity_id = Column(UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=True, index=True)
    source_url = Column(Text, nullable=True)
    source_snapshot_hash = Column(String(64), nullable=True)
    connection_id = Column(UUID(as_uuid=True), ForeignKey("external_connections.id", ondelete="RESTRICT"), nullable=False, index=True)
    application_draft_id = Column(UUID(as_uuid=True), ForeignKey("application_drafts.id", ondelete="RESTRICT"), nullable=True, index=True)
    application_draft_version = Column(Integer, nullable=False, default=1, server_default="1")
    action_type = Column(String(80), nullable=False, default="engagement.submit_application", server_default="engagement.submit_application")
    authorization_mode = Column(String(24), nullable=False, default="human_approval", server_default="human_approval")
    action_policy_id = Column(UUID(as_uuid=True), ForeignKey("agent_action_policies.id", ondelete="RESTRICT"), nullable=True)
    action_policy_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_action_policy_revisions.id", ondelete="RESTRICT"), nullable=True)
    action_policy_hash = Column(String(64), nullable=True)
    automation_rule_revision_id = Column(UUID(as_uuid=True), ForeignKey("agent_automation_rule_revisions.id", ondelete="RESTRICT"), nullable=True)
    dedupe_key = Column(String(255), nullable=True, index=True)
    registry_schema_version = Column(String(32), nullable=True)
    credential_revision = Column(Integer, nullable=True)
    source_event_id = Column(UUID(as_uuid=True), ForeignKey("agent_automation_events.id", ondelete="RESTRICT"), nullable=True)
    action_position = Column(Integer, nullable=True)
    idempotency_key = Column(String(255), nullable=False)
    payload_json = Column("payload", JSON, nullable=False, default=dict, server_default="{}")
    payload_hash = Column(String(64), nullable=False, index=True)
    artifact_hashes = Column(JSON, nullable=False, default=list, server_default="[]")
    action_version = Column(Integer, nullable=False, default=1, server_default="1")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    status = Column(String(32), nullable=False, default="proposed", server_default="proposed", index=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Immutable historical origin pins for autonomous proposals. These are
    # deliberately non-FK identities: deleting a run/work item must preserve
    # provenance. Services resolve and validate live origins before execution.
    # ``owner_user_id``
    # remains the human/account ownership field for backwards compatibility;
    # an Agent ID is never written into it.
    origin_agent_id = Column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    origin_agent_run_id = Column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    origin_work_item_id = Column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Media references intentionally have no SQL foreign keys.  The MediaOps
    # tables are optional in lightweight deployments/tests and the service
    # performs the authoritative ACL, identity and hash checks before a row is
    # written or executed.
    content_item_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    content_variant_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    content_variant_revision_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    persona_revision_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    platform_account_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    platform_account_revision_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    platform = Column(String(16), nullable=True, index=True)
    # MediaOps execution authority is explicit and immutable for one action
    # revision.  Engagement actions retain the historical ``manual`` default;
    # provider execution requires the capability/adapter evidence below.
    execution_mode = Column(
        String(16),
        nullable=False,
        default="manual",
        server_default="manual",
    )
    capability_snapshot_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    capability_snapshot_hash = Column(String(64), nullable=True)
    adapter_key = Column(String(128), nullable=True)
    adapter_version = Column(String(32), nullable=True)
    credential_state_hash = Column(String(64), nullable=True)
    # A caller-provided opaque execution key is optional for manual actions;
    # provider actions bind it to the action idempotency key at proposal time.
    execution_key = Column(String(255), nullable=True)
    # Migration-only marker. Such rows are retained evidence, never executable.
    legacy_evidence_incomplete = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    __table_args__ = (
        CheckConstraint(
            "length(trim(action_type)) BETWEEN 1 AND 80 AND action_type = lower(action_type) AND action_type = trim(action_type) AND action_type NOT LIKE '% %'",
            name="ck_external_actions_type",
        ),
        CheckConstraint("authorization_mode IN ('human_approval','bounded_policy')", name="ck_external_actions_authorization_mode"),
        CheckConstraint("credential_revision IS NULL OR credential_revision > 0", name="ck_external_actions_credential_revision"),
        CheckConstraint("action_position IS NULL OR action_position >= 0", name="ck_external_actions_action_position"),
        CheckConstraint(
            "authorization_mode <> 'bounded_policy' OR (action_policy_id IS NOT NULL "
            "AND action_policy_revision_id IS NOT NULL AND action_policy_hash IS NOT NULL "
            "AND length(action_policy_hash) = 64 AND origin_agent_id IS NOT NULL "
            "AND origin_agent_run_id IS NOT NULL "
            "AND (automation_rule_revision_id IS NULL OR origin_work_item_id IS NOT NULL))",
            name="ck_external_actions_bounded_provenance",
        ),
        CheckConstraint(
            "execution_mode IN ('manual', 'provider')",
            name="ck_external_actions_execution_mode",
        ),
        CheckConstraint(
            "legacy_evidence_incomplete OR execution_mode <> 'provider' OR "
            "(adapter_key IS NOT NULL AND length(trim(adapter_key)) > 0 "
            "AND adapter_version IS NOT NULL AND length(trim(adapter_version)) > 0 "
            "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
            "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
            "AND ((action_type NOT LIKE 'media.%' AND action_type <> 'engagement.submit_application' "
            "AND registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
            "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
            "AND length(capability_snapshot_hash) = 64)))",
            name="ck_external_actions_provider_evidence",
        ),
        CheckConstraint(
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
            name="ck_external_actions_capability_snapshot_hash",
        ),
        CheckConstraint(
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
            name="ck_external_actions_credential_state_hash",
        ),
        CheckConstraint("length(payload_hash) = 64", name="ck_external_actions_payload_hash_length"),
        CheckConstraint("length(source_snapshot_hash) = 64 OR source_snapshot_hash IS NULL", name="ck_external_actions_source_hash_length"),
        CheckConstraint("action_version > 0 AND version > 0 AND application_draft_version > 0", name="ck_external_actions_versions_positive"),
        Index(
            "uq_external_actions_personal_idempotency",
            "owner_user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
            sqlite_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_external_actions_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
            sqlite_where=text("project_id IS NOT NULL"),
        ),
        Index("ix_external_actions_owner_project", "owner_user_id", "project_id"),
        Index("ix_external_actions_policy_dedupe_created", "action_policy_id", "dedupe_key", "created_at"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "opportunity_id": _uuid(self.opportunity_id),
            "source_url": sanitize_source_url(self.source_url),
            "source_snapshot_hash": self.source_snapshot_hash,
            "connection_id": _uuid(self.connection_id),
            "application_draft_id": _uuid(self.application_draft_id),
            "content_item_id": _uuid(self.content_item_id),
            "content_variant_id": _uuid(self.content_variant_id),
            "content_variant_revision_id": _uuid(self.content_variant_revision_id),
            # Descriptive aliases retained for MediaOps clients that include
            # the namespace in their DTO field names.
            "media_content_item_id": _uuid(self.content_item_id),
            "media_content_variant_id": _uuid(self.content_variant_id),
            "media_content_variant_revision_id": _uuid(self.content_variant_revision_id),
            "persona_revision_id": _uuid(self.persona_revision_id),
            "platform_account_id": _uuid(self.platform_account_id),
            "platform_account_revision_id": _uuid(self.platform_account_revision_id),
            "platform": self.platform,
            "execution_mode": self.execution_mode or "manual",
            "capability_snapshot_id": _uuid(self.capability_snapshot_id),
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "adapter_key": self.adapter_key,
            "adapter_version": self.adapter_version,
            # Credential state hashes are integrity evidence used only by the
            # server-side execution gate; they are intentionally omitted from
            # safe action projections and audit payloads.
            "execution_key": self.execution_key,
            "application_draft_version": int(self.application_draft_version or 1),
            "action_type": self.action_type,
            "payload": _safe_action_payload(self.payload_json),
            "payload_hash": self.payload_hash,
            "artifact_hashes": list(self.artifact_hashes or []),
            "action_version": int(self.action_version or 1),
            "version": int(self.version or 1),
            "status": self.status,
            "created_by": _uuid(self.created_by),
            "origin_agent_id": _uuid(self.origin_agent_id),
            "origin_agent_run_id": _uuid(self.origin_agent_run_id),
            "origin_work_item_id": _uuid(self.origin_work_item_id),
            "authorization_mode": self.authorization_mode or "human_approval",
            "legacy_evidence_incomplete": bool(self.legacy_evidence_incomplete),
            "action_policy_id": _uuid(self.action_policy_id),
            "action_policy_revision_id": _uuid(self.action_policy_revision_id),
            "action_policy_hash": self.action_policy_hash,
            "automation_rule_revision_id": _uuid(self.automation_rule_revision_id),
            "source_event_id": _uuid(self.source_event_id),
            "action_position": self.action_position,
            "dedupe_key": self.dedupe_key,
            "registry_schema_version": self.registry_schema_version,
            "created_at": _dt(self.created_at),
            "updated_at": _dt(self.updated_at),
        }

    to_dict = to_safe_dict


class ExternalActionApproval(_OperationModel, Base):
    """Human decision bound to exact action version/hash/artifact hashes."""

    __tablename__ = "external_action_approvals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("external_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action_version = Column(Integer, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    artifact_hashes = Column(JSON, nullable=False, default=list, server_default="[]")
    decision = Column(String(16), nullable=False)
    reason = Column(Text, nullable=True)
    decided_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("decision IN ('approved','rejected','invalidated')", name="ck_external_action_approvals_decision"),
        CheckConstraint("action_version > 0", name="ck_external_action_approvals_version_positive"),
        CheckConstraint("length(payload_hash) = 64", name="ck_external_action_approvals_hash_length"),
        Index("ix_external_action_approvals_action_version", "action_id", "action_version"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "action_id": _uuid(self.action_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "action_version": int(self.action_version or 1),
            "payload_hash": self.payload_hash,
            "artifact_hashes": list(self.artifact_hashes or []),
            "decision": self.decision,
            "reason": self.reason,
            "decided_by": _uuid(self.decided_by),
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class ExternalActionAttempt(_OperationModel, Base):
    """One manually executed attempt against an approved action version."""

    __tablename__ = "external_action_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("external_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action_version = Column(Integer, nullable=False)
    executor_type = Column(String(16), nullable=False, default="manual", server_default="manual")
    execution_mode = Column(
        String(16),
        nullable=False,
        default="manual",
        server_default="manual",
    )
    provider_key = Column(String(120), nullable=True)
    provider_adapter_key = Column(String(128), nullable=True)
    provider_adapter_version = Column(String(32), nullable=True)
    registry_schema_version = Column(String(32), nullable=True)
    legacy_evidence_incomplete = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    capability_snapshot_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    capability_snapshot_hash = Column(String(64), nullable=True)
    credential_state_hash = Column(String(64), nullable=True)
    execution_key = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False, default="running", server_default="running", index=True)
    provider_attempt_ref = Column(String(255), nullable=True)
    evidence_artifact_ids = Column(JSON, nullable=False, default=list, server_default="[]")
    result_summary = Column(Text, nullable=True)
    evidence_note = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "executor_type IN ('manual', 'provider', 'service')",
            name="ck_external_action_attempts_executor_type",
        ),
        CheckConstraint(
            "execution_mode IN ('manual', 'provider')",
            name="ck_external_action_attempts_execution_mode",
        ),
        CheckConstraint(
            "execution_mode <> 'provider' OR executor_type IN ('provider','service')",
            name="ck_external_action_attempts_provider_executor",
        ),
        CheckConstraint(
            "legacy_evidence_incomplete OR execution_mode <> 'provider' OR "
            "(provider_key IS NOT NULL AND length(trim(provider_key)) > 0 "
            "AND provider_adapter_key IS NOT NULL AND length(trim(provider_adapter_key)) > 0 "
            "AND provider_adapter_version IS NOT NULL AND length(trim(provider_adapter_version)) > 0 "
            "AND credential_state_hash IS NOT NULL AND length(credential_state_hash) = 64 "
            "AND execution_key IS NOT NULL AND length(trim(execution_key)) > 0 "
            "AND ((registry_schema_version IS NOT NULL AND length(trim(registry_schema_version)) > 0) "
            "OR (capability_snapshot_id IS NOT NULL AND capability_snapshot_hash IS NOT NULL "
            "AND length(capability_snapshot_hash) = 64)))",
            name="ck_external_action_attempts_provider_evidence",
        ),
        CheckConstraint(
            "capability_snapshot_hash IS NULL OR length(capability_snapshot_hash) = 64",
            name="ck_external_action_attempts_capability_snapshot_hash",
        ),
        CheckConstraint(
            "credential_state_hash IS NULL OR length(credential_state_hash) = 64",
            name="ck_external_action_attempts_credential_state_hash",
        ),
        CheckConstraint("status IN ('running','succeeded','failed','uncertain')", name="ck_external_action_attempts_status"),
        CheckConstraint("action_version > 0", name="ck_external_action_attempts_version_positive"),
    )

    @property
    def ended_at(self):
        return self.finished_at

    @ended_at.setter
    def ended_at(self, value):
        self.finished_at = value

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "action_id": _uuid(self.action_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "action_version": int(self.action_version or 1),
            "executor_type": self.executor_type,
            "execution_mode": self.execution_mode or self.executor_type or "manual",
            "provider_key": self.provider_key,
            "provider_adapter_key": self.provider_adapter_key,
            "provider_adapter_version": self.provider_adapter_version,
            "capability_snapshot_id": _uuid(self.capability_snapshot_id),
            "capability_snapshot_hash": self.capability_snapshot_hash,
            "execution_key": self.execution_key,
            "status": self.status,
            "provider_attempt_ref": self.provider_attempt_ref,
            "evidence_artifact_ids": [_uuid(item) or str(item) for item in (self.evidence_artifact_ids or [])],
            "result_summary": self.result_summary,
            "evidence_note": self.evidence_note,
            "error_message": self.error_message,
            "started_at": _dt(self.started_at),
            "finished_at": _dt(self.finished_at),
            "ended_at": _dt(self.finished_at),
            "created_by": _uuid(self.created_by),
        }

    to_dict = to_safe_dict


class ExternalActionReceipt(_OperationModel, Base):
    """Immutable success receipt generated by a successful attempt."""

    __tablename__ = "external_action_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("external_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id = Column(UUID(as_uuid=True), ForeignKey("external_action_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action_version = Column(Integer, nullable=False)
    provider_receipt_ref = Column(String(255), nullable=True)
    remote_resource_id = Column(String(255), nullable=True)
    remote_url = Column(Text, nullable=True)
    remote_status = Column(String(64), nullable=True)
    provider_observed_at = Column(DateTime, nullable=True)
    evidence_note = Column(Text, nullable=True)
    confirmation_level = Column(String(32), nullable=False, default="human_confirmed", server_default="human_confirmed")
    evidence_artifact_ids = Column(JSON, nullable=False, default=list, server_default="[]")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("confirmation_level IN ('human_confirmed','provider_confirmed','reconciled')", name="ck_external_action_receipts_confirmation_level"),
        CheckConstraint("action_version > 0", name="ck_external_action_receipts_version_positive"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "action_id": _uuid(self.action_id),
            "attempt_id": _uuid(self.attempt_id),
            "owner_user_id": _uuid(self.owner_user_id),
            "action_version": int(self.action_version or 1),
            "provider_receipt_ref": self.provider_receipt_ref,
            "remote_resource_id": self.remote_resource_id,
            # Legacy receipt rows may contain userinfo/query secrets.  Keep
            # the public projection fail-closed and fragmentless just like
            # source URLs.
            "remote_url": sanitize_source_url(self.remote_url),
            "remote_status": self.remote_status,
            "provider_observed_at": _dt(self.provider_observed_at),
            "evidence_note": self.evidence_note,
            "confirmation_level": self.confirmation_level,
            "evidence_artifact_ids": [_uuid(item) or str(item) for item in (self.evidence_artifact_ids or [])],
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


class OperationEvent(_OperationModel, Base):
    """Append-only durable timeline event."""

    __tablename__ = "operation_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    event_type = Column(String(100), nullable=False)
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_type = Column(String(16), nullable=False, default="human", server_default="human")
    payload_json = Column("payload", JSON, nullable=False, default=dict, server_default="{}")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('human','system','agent','unknown')",
            name="ck_operation_events_actor_type",
        ),
        Index("ix_operation_events_entity_created", "entity_type", "entity_id", "created_at"),
        Index("ix_operation_events_project_created", "project_id", "created_at"),
    )

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "id": _uuid(self.id),
            "owner_user_id": _uuid(self.owner_user_id),
            "project_id": _uuid(self.project_id),
            "entity_type": self.entity_type,
            "entity_id": _uuid(self.entity_id),
            "event_type": self.event_type,
            "actor_id": _uuid(self.actor_id),
            "actor_type": self.actor_type,
            "payload": self.payload_json or {},
            "created_at": _dt(self.created_at),
        }

    to_dict = to_safe_dict


# Public aliases make the domain names convenient in services/tests while
# retaining the explicit table-oriented names above.
Opportunity = EngagementOpportunity
Evaluation = OpportunityEvaluation
ActionApproval = ExternalActionApproval
ActionAttempt = ExternalActionAttempt
ActionReceipt = ExternalActionReceipt
TimelineEvent = OperationEvent


__all__ = [
    "ExternalConnection",
    "ArtifactVersion",
    "EngagementOpportunity",
    "Opportunity",
    "OpportunityEvaluation",
    "Evaluation",
    "ApplicationDraft",
    "ExternalAction",
    "ExternalActionApproval",
    "ActionApproval",
    "ExternalActionAttempt",
    "ActionAttempt",
    "ExternalActionReceipt",
    "ActionReceipt",
    "OperationEvent",
    "TimelineEvent",
    "sanitize_source_url",
    "default_opportunity_title",
    "safe_opportunity_title",
    "DEFAULT_OPPORTUNITY_TITLE",
]
