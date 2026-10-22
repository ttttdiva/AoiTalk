"""Audited upstream source/engine contract for managed Strata.

Digests use sorted POSIX paths, NUL separators, decimal content lengths and
file bytes, as implemented by strata_runtime._canonical_tree_digest().
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StrataManifest:
    repository: str
    release_tag: str
    revision: str
    engine_version: str
    archive_sha256: str
    tree_sha256: str
    tree_sha256_normalized: str
    file_count: int
    tree_bytes: int
    tree_bytes_normalized: int

    @property
    def archive_url(self) -> str:
        return f"https://codeload.github.com/{self.repository}/tar.gz/{self.revision}"

    @property
    def engine_url(self) -> str:
        return f"https://github.com/{self.repository}/releases/download/{self.release_tag}/"


STRATA_MANIFEST = StrataManifest(
    repository="Niko1221/Strata",
    release_tag="v0.1.41",
    revision="fb58e0dbc8399662c0e47c76578c6e878b14f6cf",
    engine_version="0.1.41",
    archive_sha256="0e1d7e8fbb50eebfdb28b60dcff029bfa371215958d59605825746713a0ef034",
    tree_sha256="4430385813d593db27fe4e7dd40979c2e0053065cf7b8214bc56285e71c2e006",
    tree_sha256_normalized="dc7b46f300f78bee4c0bdfca55836d4d3f0efe19983dce06607237fd8fb5b832",
    file_count=2422,
    tree_bytes=38_603_272,
    tree_bytes_normalized=38_603_031,
)
