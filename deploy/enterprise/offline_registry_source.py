"""Collect canonical, digest-preserving linux/amd64 OCI closures over HTTPS.

Only public Docker Hub/GHCR images declared by the handoff are fetched. Registry
bearer tokens are ephemeral, are not logged, and never enter a companion.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from enterprise_release_common import BLOCK, ContractError, atomic_json

INDEX = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
ACCEPT = ", ".join(sorted(INDEX | MANIFEST))
IMMUTABLE = re.compile(r"^([a-z0-9][a-z0-9./_-]*)@sha256:([0-9a-f]{64})$")
MAX_METADATA = 16 * BLOCK


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
    def __init__(self, *, maximum_bytes: int = 64 * 1024**3, opener=None):
        self.maximum_bytes = maximum_bytes
        self.downloaded_bytes = 0
        self.opener = opener or build_opener(HTTPSRedirect())
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

    def blob(self, layout: Path, host: str, repository: str, digest: str, *, manifest=False, size=None) -> tuple[Path, str]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ContractError("unsupported OCI descriptor digest")
        path = layout / "blobs" / "sha256" / digest[7:]
        path.parent.mkdir(parents=True, exist_ok=True)
        h, total = hashlib.sha256(), 0
        endpoint = ("manifests/" if manifest else "blobs/") + digest
        with self.response(host, repository, endpoint) as response, path.open("xb") as output:
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            for block in iter(lambda: response.read(BLOCK), b""):
                total += len(block)
                self.downloaded_bytes += len(block)
                if self.downloaded_bytes > self.maximum_bytes or (manifest and total > MAX_METADATA):
                    raise ContractError("OCI closure exceeds the safety size limit")
                h.update(block)
                output.write(block)
        if h.hexdigest() != digest[7:] or (size is not None and total != size):
            raise ContractError("OCI descriptor SHA256/size mismatch")
        return path, media_type

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
