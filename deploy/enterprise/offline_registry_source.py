"""Collect canonical, digest-preserving linux/amd64 OCI closures over HTTPS.

Only public Docker Hub/GHCR images declared by the handoff are fetched. Registry
bearer tokens are ephemeral, are not logged, and never enter a companion.
"""
from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import re
import socket
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from enterprise_release_common import BLOCK, ContractError, atomic_json, no_links

INDEX = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
ACCEPT = ", ".join(sorted(INDEX | MANIFEST))
IMMUTABLE = re.compile(r"^([a-z0-9][a-z0-9./_-]*)@sha256:([0-9a-f]{64})$")
MAX_METADATA = 16 * BLOCK


def _ipv4_first_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, all_errors=False):
    """Create a TCP connection, preferring IPv4 but preserving IPv6 fallback."""
    host, port = address
    errors = []
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda row: 0 if row[0] == socket.AF_INET else 1)
    for af, socktype, proto, _, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            if sock is not None:
                sock.close()
    if not errors:
        raise OSError("getaddrinfo returned no usable addresses")
    if all_errors and hasattr(__builtins__, "ExceptionGroup"):
        raise ExceptionGroup("create_connection failed", errors)
    raise errors[-1]


class IPv4FirstHTTPSConnection(__import__("http.client").client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _ipv4_first_create_connection


class IPv4FirstHTTPSHandler(__import__("urllib.request").request.HTTPSHandler):
    def https_open(self, req):
        # Python 3.12 removed HTTPSHandler._check_hostname and the matching
        # HTTPSConnection(check_hostname=...) argument. Keep the 3.10/3.11
        # compatibility path without depending on that private attribute when
        # running on newer Python versions.
        kwargs = {"context": self._context}
        check_hostname = getattr(self, "_check_hostname", None)
        if check_hostname is not None:
            kwargs["check_hostname"] = check_hostname
        return self.do_open(IPv4FirstHTTPSConnection, req, **kwargs)



class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.username or target.password:
            raise ContractError("registry redirect is not credential-free HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and target.netloc != urlsplit(req.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class RegistrySource:
    def __init__(self, *, maximum_bytes: int = 64 * 1024**3, opener=None, blob_cache: Path | None = None):
        self.maximum_bytes = maximum_bytes
        self.downloaded_bytes = 0
        self.cached_bytes = 0
        # Optional read-only reuse of public OCI bytes from a previous producer.
        # Only config/layer bytes are reused; registry manifests are fetched anew.
        self.blob_cache = no_links(blob_cache) if blob_cache is not None else None
        if self.blob_cache is not None and not self.blob_cache.is_dir():
            raise ContractError("OCI blob cache must be an existing symlink-free directory")
        self.opener = opener or build_opener(HTTPSRedirect(), IPv4FirstHTTPSHandler())
        self.tokens: dict[tuple[str, str], str] = {}

    @staticmethod
    def location(ref: str) -> tuple[str, str, str]:
        match = IMMUTABLE.fullmatch(ref)
        if not match:
            raise ContractError("image must be a SHA256-pinned repository")
        repository, digest = match.groups()
        if repository.startswith("ghcr.io/"):
            return "ghcr.io", repository[8:], "sha256:" + digest
        if repository.startswith("docker.io/"):
            repository = repository[10:]
        if "." in repository.split("/")[0]:
            raise ContractError("image registry is not an approved public source")
        if "/" not in repository:
            repository = "library/" + repository
        return "registry-1.docker.io", repository, "sha256:" + digest

    def response(self, host: str, repository: str, endpoint: str, *, retried=False):
        key = (host, repository)
        headers = {"Accept": ACCEPT}
        if key in self.tokens:
            headers["Authorization"] = "Bearer " + self.tokens[key]
        url = f"https://{host}/v2/{repository}/{endpoint}"
        try:
            return self.opener.open(Request(url, headers=headers), timeout=120)
        except HTTPError as exc:
            if exc.code != 401 or retried:
                raise ContractError(f"public registry request failed: HTTP {exc.code}") from None
            self.tokens.pop(key, None)  # Long OCI transfers can outlive a bearer token.
            challenge = exc.headers.get("WWW-Authenticate", "")
            exc.close()
        if not challenge.startswith("Bearer "):
            raise ContractError("unsupported public registry authentication")
        params = dict(re.findall(r'(\w+)="([^"\r\n]+)"', challenge))
        realm = urlsplit(params.get("realm", ""))
        expected_host = "auth.docker.io" if host == "registry-1.docker.io" else "ghcr.io"
        if realm.scheme != "https" or realm.hostname != expected_host or realm.username or realm.password or realm.query:
            raise ContractError("unapproved registry bearer realm")
        expected_scope = f"repository:{repository}:pull"
        if params.get("scope", expected_scope) != expected_scope:
            raise ContractError("registry requested a non-read-only or foreign scope")
        token_url = params["realm"] + "?" + urlencode({"service": params.get("service", host), "scope": expected_scope})
        with self.opener.open(Request(token_url), timeout=60) as response:
            raw = response.read(MAX_METADATA + 1)
        if len(raw) > MAX_METADATA:
            raise ContractError("registry token response exceeds limit")
        payload = json.loads(raw)
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ContractError("invalid registry bearer response")
        self.tokens[key] = token
        return self.response(host, repository, endpoint, retried=True)

    def copy_cached_blob(self, layout: Path, destination: Path, digest: str, size: int) -> bool:
        if self.blob_cache is None:
            return False
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ContractError("OCI descriptor size is invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", layout.name):
            raise ContractError("invalid image pin name")
        cached = no_links(self.blob_cache / "images" / layout.name / "blobs" / "sha256" / digest[7:])
        if not cached.exists():
            return False
        if not cached.is_file() or cached.stat().st_size != size:
            raise ContractError("cached OCI descriptor SHA256/size mismatch")
        # A cache never supplies authority: copy, then re-hash every byte against
        # the freshly authenticated registry descriptor. Never link it into a
        # release, trust a cache index/manifest, or write/delete the cache itself.
        h, total = hashlib.sha256(), 0
        created = False
        try:
            with cached.open("rb") as incoming, destination.open("xb") as output:
                created = True
                for block in iter(lambda: incoming.read(BLOCK), b""):
                    total += len(block)
                    self.downloaded_bytes += len(block)
                    if total > size or self.downloaded_bytes > self.maximum_bytes:
                        raise ContractError("OCI closure exceeds the safety size limit")
                    h.update(block)
                    output.write(block)
            if total != size or h.hexdigest() != digest[7:]:
                raise ContractError("cached OCI descriptor SHA256/size mismatch")
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        self.cached_bytes += total
        return True

    def blob(self, layout: Path, host: str, repository: str, digest: str, *, manifest=False, size=None) -> tuple[Path, str]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ContractError("unsupported OCI descriptor digest")
        path = layout / "blobs" / "sha256" / digest[7:]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not manifest and self.copy_cached_blob(layout, path, digest, size):
            return path, "application/octet-stream"
        endpoint = ("manifests/" if manifest else "blobs/") + digest
        for attempt in range(3):
            h, total = hashlib.sha256(), 0
            retry_error = None
            try:
                with self.response(host, repository, endpoint) as response, path.open("xb") as output:
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                    for block in iter(lambda: response.read(BLOCK), b""):
                        total += len(block)
                        self.downloaded_bytes += len(block)
                        if self.downloaded_bytes > self.maximum_bytes or (manifest and total > MAX_METADATA):
                            raise ContractError("OCI closure exceeds the safety size limit")
                        h.update(block)
                        output.write(block)
            except (OSError, http.client.HTTPException) as exc:
                retry_error = exc
            else:
                if h.hexdigest() == digest[7:] and (size is None or total == size):
                    return path, media_type
                retry_error = ContractError("OCI descriptor SHA256/size mismatch")
            path.unlink(missing_ok=True)
            if attempt == 2:
                if isinstance(retry_error, ContractError):
                    raise retry_error
                raise ContractError("public registry blob transfer failed after retries") from None
        raise AssertionError("unreachable")

    def collect(self, pin: dict, output: Path) -> dict:
        name, ref = pin["name"], pin["ref"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ContractError("invalid image pin name")
        host, repository, digest = self.location(ref)
        layout = output / "images" / name
        layout.mkdir(parents=True)
        canonical_file, media_type = self.blob(layout, host, repository, digest, manifest=True)
        canonical = json.loads(canonical_file.read_bytes())
        media_type = canonical.get("mediaType", media_type)
        descriptor = {"mediaType": media_type, "digest": digest, "size": canonical_file.stat().st_size}
        if canonical.get("schemaVersion") != 2:
            raise ContractError("OCI schema version is not 2")
        if media_type in INDEX:
            matching = [r for r in canonical.get("manifests", []) if r.get("platform", {}).get("os") == "linux" and r.get("platform", {}).get("architecture") == "amd64"]
            if len(matching) != 1:
                raise ContractError("OCI index must select exactly one linux/amd64 image")
            selected = matching[0]
            if selected.get("mediaType") not in MANIFEST:
                raise ContractError("unsupported nested platform manifest")
            platform_file, _ = self.blob(layout, host, repository, selected["digest"], manifest=True, size=selected["size"])
            platform = json.loads(platform_file.read_bytes())
            platform_digest = selected["digest"]
        elif media_type in MANIFEST:
            platform, platform_digest = canonical, digest
        else:
            raise ContractError("canonical pin is not an OCI image/index")
        config = platform["config"]
        config_file, _ = self.blob(layout, host, repository, config["digest"], size=config["size"])
        if config_file.stat().st_size > MAX_METADATA:
            raise ContractError("OCI config exceeds metadata limit")
        identity = json.loads(config_file.read_bytes())
        if (identity.get("os"), identity.get("architecture")) != ("linux", "amd64"):
            raise ContractError("OCI config is not linux/amd64")
        layers = platform.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ContractError("OCI image has no layers")
        # Repeated layer descriptors may reference the same content-addressed blob.
        loaded = {config["digest"]: config["size"]}
        for layer in layers:
            if layer["digest"] in loaded:
                if loaded[layer["digest"]] != layer["size"]:
                    raise ContractError("OCI duplicate descriptor size mismatch")
                continue
            self.blob(layout, host, repository, layer["digest"], size=layer["size"])
            loaded[layer["digest"]] = layer["size"]
        atomic_json(layout / "oci-layout", {"imageLayoutVersion": "1.0.0"})
        atomic_json(layout / "index.json", {"schemaVersion": 2, "manifests": [descriptor]})
        return {"name": name, "ref": ref, "archive": "", "oci_layout": f"images/{name}",
                "platform": "linux/amd64", "archive_manifest_digest": digest,
                "platform_manifest_digest": platform_digest, "config_digest": config["digest"]}
