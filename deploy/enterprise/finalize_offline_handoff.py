#!/usr/bin/env python3
"""Produce a complete offline release pair from an authenticated source handoff.

This connected-PC interface needs no private repository checkout and no custom
OfflineInputRoot. The original ZIP is never modified. The ordinary release
builder can instead call collect directly in its own sanitized staging tree.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from enterprise_release_common import (
    BLOCK, ContractError, atomic_json, hash_file, no_links, publish_directory,
    read_json, relative_path, verify_checksums, write_checksums, write_zip,
)
from produce_offline_inputs import collect, inspect_source, verify_proof


MAX_SOURCE_BYTES = 8 * 1024**3
MAX_SOURCE_ENTRIES = 20_000


def authenticated_copy(source: Path, destination: Path, expected: str) -> None:
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ContractError('trusted source handoff SHA256 is required')
    no_links(source)
    if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
        raise ContractError('source handoff ZIP is not a regular file')
    digest = hashlib.sha256()
    with source.open('rb') as incoming, destination.open('xb') as output:
        for block in iter(lambda: incoming.read(BLOCK), b''):
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(destination, 0o600)
    if digest.hexdigest() != expected:
        raise ContractError('source handoff SHA256 does not match the trusted delivery')


def extract_source(archive: Path, destination: Path) -> None:
    no_links(destination)
    if destination.exists():
        raise ContractError('handoff extraction destination must be new')
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if not entries or len(entries) > MAX_SOURCE_ENTRIES:
            raise ContractError('handoff ZIP entry count is invalid')
        seen, files, total = set(), set(), 0
        for entry in entries:
            name = relative_path(entry.filename[:-1] if entry.is_dir() else entry.filename)
            key = name.casefold()
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if key in seen or entry.flag_bits & 1 or mode not in {0, stat.S_IFDIR, stat.S_IFREG}:
                raise ContractError('handoff ZIP has duplicate, encrypted, linked or special entries')
            if (entry.is_dir() and mode == stat.S_IFREG) or (not entry.is_dir() and mode == stat.S_IFDIR):
                raise ContractError('handoff ZIP entry type is inconsistent')
            seen.add(key)
            if not entry.is_dir():
                files.add(key)
                total += entry.file_size
        if total > MAX_SOURCE_BYTES:
            raise ContractError('source ZIP exceeds the canonical 8GiB limit')
        for name in seen:
            parts = name.split('/')
            if any('/'.join(parts[:i]) in files for i in range(1, len(parts))):
                raise ContractError('handoff ZIP file is also a parent directory')
        if not {'bundle-manifest.json', 'sha256sums'}.issubset(files):
            raise ContractError('handoff ZIP lacks manifest/checksums')
        destination.mkdir(mode=0o700)
        for entry in entries:
            path = destination / entry.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry.is_dir():
                path.mkdir(exist_ok=True)
            else:
                with bundle.open(entry) as incoming, path.open('xb') as output:
                    shutil.copyfileobj(incoming, output, BLOCK)
                os.chmod(path, 0o600)
    verify_checksums(destination)


def finalize(archive: Path, expected_sha256: str, destination: Path, *, collector=None, verifier=None, hydrator=None) -> dict:
    destination = no_links(destination)
    if destination.exists():
        raise ContractError('release output must be a new directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Test doubles are injectable in the Python API only; the public CLI has
    # no skip-proof, skip-validator, or manual cache override.
    collector = collector or collect
    if verifier is None or hydrator is None:
        from offline_inputs import hydrate_archive, verify_companion
        verifier = verifier or verify_companion
        hydrator = hydrator or hydrate_archive
    staging = Path(tempfile.mkdtemp(prefix='.enterprise-release-', dir=destination.parent))
    try:
        snapshot = staging / 'authenticated-source.zip'
        authenticated_copy(archive, snapshot, expected_sha256.lower())
        handoff = staging / 'handoff'
        extract_source(snapshot, handoff)
        manifest_path = handoff / 'bundle-manifest.json'
        manifest, source = inspect_source(manifest_path)
        descriptor = manifest.get('offline_producer', {})
        prefix = str(descriptor.get('archive_prefix', ''))
        if descriptor.get('version') != 1 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]*', prefix):
            raise ContractError('handoff lacks the official standalone producer contract')
        original_offline = manifest.get('offline_build', {})
        if original_offline.get('mode') != 'none' or original_offline.get('enabled') is not False:
            raise ContractError('already-bound releases must reuse their original companion, not be regenerated')
        inputs = staging / 'inputs'
        collector(manifest_path, inputs)
        # A collector return value is not sufficient: the proof and the full
        # canonical consumer must validate the actual emitted bytes.
        verify_proof(inputs, manifest, source)
        document = read_json(inputs / 'offline-build-manifest.json')
        output = staging / 'complete'
        output.mkdir(mode=0o700)
        commit = manifest['source_commit']
        companion_name = f'{prefix}_Enterprise_Offline_Inputs_{commit}.zip'
        handoff_name = f'{prefix}_Enterprise_Handoff_{commit}.zip'
        companion = output / companion_name
        write_zip(inputs, companion)
        manifest['offline_build'] = dict(original_offline,
            format=document['format'], version=1, enabled=True, mode='external-directory', root='',
            source_commit=commit, source_tree_sha256=document['source_tree_sha256'],
            handoff_binding_sha256=document['handoff_binding_sha256'], network='none',
            companion_archive=companion_name, companion_sha256=hash_file(companion),
            manifest_sha256=hash_file(inputs / 'offline-build-manifest.json'),
            npm_lock_sha256=hash_file(source / 'frontend/package-lock.json'),
            pyproject_sha256=hash_file(source / 'pyproject.toml'))
        atomic_json(manifest_path, manifest)
        write_checksums(handoff)
        verifier(inputs, manifest_path, archive=companion)
        target = output / handoff_name
        write_zip(handoff, target)
        # Verify the transport, not merely the staging tree. This also ensures
        # that the manifest checksum and source binding survive ZIP packaging.
        roundtrip = staging / 'roundtrip'
        extract_source(target, roundtrip)
        # Reuse the private input path rather than keeping a second expanded
        # multi-GiB payload. Validate the bytes actually carried by the ZIP.
        shutil.rmtree(inputs)
        hydrator(companion, inputs)
        verifier(inputs, roundtrip / 'bundle-manifest.json', archive=companion)
        receipt = {'format': 'enterprise-offline-release-capsule', 'version': 1,
                   'status': 'complete', 'source_commit': commit,
                   'handoff': {'file': handoff_name, 'sha256': hash_file(target)},
                   'companion': {'file': companion_name, 'sha256': hash_file(companion)}}
        atomic_json(output / 'release-capsule.json', receipt)
        publish_directory(output, destination)
        return receipt
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--handoff-zip', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = finalize(args.handoff_zip, args.expected_sha256, args.output)
    except (ContractError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(1, f'Enterprise standalone producer failed: {type(exc).__name__}: {exc}\n')
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
