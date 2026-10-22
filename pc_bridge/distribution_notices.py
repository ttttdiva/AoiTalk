"""Release conditions for reviewed PC Bridge notice inputs, separate from hashes."""
from collections.abc import Mapping
import hashlib

from .license_bundle import LicenseBundleError


def validate_notice_conditions(manifest: Mapping, files: Mapping[str, bytes], policy: Mapping) -> None:
    """Fail closed before building when originals or release decisions are missing."""
    requirements = policy.get("required_files")
    if policy.get("schema") != 1 or not isinstance(requirements, list) or not requirements:
        raise LicenseBundleError("Distribution notice policy is missing")
    for item in requirements:
        path, expected = item["path"], item["sha256"]
        raw = files.get(path)
        if raw is None or hashlib.sha256(raw).hexdigest() != expected:
            raise LicenseBundleError("Required distribution original is missing or changed: " + path)
    checks = manifest.get("distribution_checks")
    if not isinstance(checks, dict) or checks.get("notice_assembly") != "complete":
        raise LicenseBundleError("Distribution notice assembly is incomplete")
    choices = checks.get("zune_license_choices")
    if not isinstance(choices, dict) or set(choices) != set(policy["zune_packages"]) or any(
            choice not in policy["zune_available_choices"].get(package, [])
            for package, choice in choices.items()):
        raise LicenseBundleError("Zune distribution license choices are not confirmed")
    microsoft = checks.get("microsoft_redistribution")
    if not isinstance(microsoft, dict) or microsoft.get("status") != "confirmed" or not all(
            isinstance(microsoft.get(key), str) and microsoft[key].strip()
            for key in ("applicable_license", "authorization_evidence")):
        raise LicenseBundleError("Microsoft DLL redistribution authorization is unconfirmed")
    if checks.get("release_review") != "complete":
        raise LicenseBundleError("Distribution release review is incomplete")
