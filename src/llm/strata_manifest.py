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
    release_tag="v0.2.9",
    revision="c62d5e3a15b40ea51c96aae9e8d36928965f53ab",
    engine_version="0.1.55",
    archive_sha256="ed63c4da7a87694641e958c5d20c8cb35ae9edeb6b2fcb82d92e785ed4ebf147",
    tree_sha256="bcb1a95b3303cd3740eaf2c7955ace7266a74bb2a1b72f0498592c79b88cdefd",
    tree_sha256_normalized="f259f0b78e091ede4be75d16ef9aad1fd432dff4cfa6b38dc5d3f4791b32dd49",
    file_count=5319,
    tree_bytes=86_361_397,
    tree_bytes_normalized=86_360_395,
)

# DeepSeek V4.1 is shipped by the same upstream project, but it is not
# compatible with the Qwen engine/source pin above.  Keep this contract
# separate so selecting the DeepSeek profile never replaces the managed Qwen
# runtime.  The release source is selected by its immutable commit; the
# existing Qwen archive/tree digests remain the authoritative integrity pin for
# the long lived Qwen installation.  The DeepSeek source archive/tree is pinned
# here as well, while its signed engine package is verified by its own package
# manifest before launch.
STRATA_DEEPSEEK_MANIFEST = StrataManifest(
    repository="Niko1221/Strata",
    release_tag="v0.2.4",
    revision="96ca4383c7b05a8a3e6064c65d9e9977d4b3bb59",
    engine_version="0.1.49.2",
    archive_sha256="f523bdf8589e7bcb442c0ef2ad06fcd2d58ad82f607f74fae69278b67e49f9f4",
    tree_sha256="d477b89784c32e23b6a47298d9fb2752f4591da20526ac4253573c3555e4fb44",
    tree_sha256_normalized="7cd7dcaaada6d21958f082ab2cb83c253868d7080e1a4869a2f958e8694f8f53",
    file_count=4887,
    tree_bytes=80_942_832,
    tree_bytes_normalized=80_941_836,
)

# Short name used by runtime code and tests that refer to the upstream
# component as the DSV41 engine.
STRATA_DSV41_MANIFEST = STRATA_DEEPSEEK_MANIFEST
