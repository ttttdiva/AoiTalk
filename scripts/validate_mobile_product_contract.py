#!/usr/bin/env python3
"""Validate the native Mobile Product Contract.

The product contract is intentionally small: it is a P0 capability registry,
not an inventory of every backend route.  This validator checks the invariants
that make the registry useful in CI:

* API references are OpenAPI ``operationId`` values, never raw routes.
* critical capabilities cannot cross a Next.js BFF transport.
* every declared native route exists exactly once and every screen file is
  represented (orphan routes are therefore detected).
* tabs, entry points, capability routes, and implementation files are
  reachable and internally consistent.
* scope/permission metadata is explicit and cannot silently become public.

No server is started and no network is used.  The command is deterministic and
can be run from a clean checkout with the committed OpenAPI artifact.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = REPO_ROOT / "contracts" / "product-contract.json"
DEFAULT_SCHEMA = REPO_ROOT / "contracts" / "product-contract.schema.json"
DEFAULT_OPENAPI = REPO_ROOT / "contracts" / "openapi" / "fastapi.json"

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_ROUTE_KEYS = {"path", "route", "url", "endpoint", "href"}


class ContractValidationError(ValueError):
    """Raised by :func:`validate_or_raise` when the contract is invalid."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractValidationError(f"JSON artifact not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractValidationError(f"JSON root must be an object: {path}")
    return value


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _expect_object(value: Any, location: str, errors: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{location}: expected object, got {_type_name(value)}")
        return None
    return value


def _required(obj: Mapping[str, Any], names: Iterable[str], location: str, errors: list[str]) -> None:
    for name in names:
        if name not in obj:
            errors.append(f"{location}: missing required field {name!r}")


def _allowed(obj: Mapping[str, Any], names: Iterable[str], location: str, errors: list[str]) -> None:
    allowed = set(names)
    for name in sorted(set(obj) - allowed):
        errors.append(f"{location}: unknown field {name!r}")


def _string(value: Any, location: str, errors: list[str], *, non_empty: bool = True) -> bool:
    if not isinstance(value, str) or (non_empty and not value):
        errors.append(f"{location}: expected non-empty string")
        return False
    return True


def _unique_ids(items: list[Any], location: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(items):
        obj = _expect_object(item, f"{location}[{i}]", errors)
        if obj is None:
            continue
        ident = obj.get("id")
        if not _string(ident, f"{location}[{i}].id", errors):
            continue
        if ident in index:
            errors.append(f"{location}: duplicate id {ident!r}")
        else:
            index[ident] = dict(obj)
    return index


def _shape_errors(contract: Mapping[str, Any]) -> list[str]:
    """Small dependency-free structural/schema check.

    The checked-in JSON Schema remains the normative structure for tooling that
    has ``jsonschema`` installed.  Keeping this equivalent minimum check here
    makes the CI gate usable without adding a runtime dependency to the app.
    """

    errors: list[str] = []
    _allowed(contract, ["$schema", "contract_version", "product", "navigation", "capabilities"], "$", errors)
    _required(contract, ["contract_version", "product", "navigation", "capabilities"], "$", errors)
    if contract.get("contract_version") != "mobile-product-contract/v1":
        errors.append("$.contract_version: must be mobile-product-contract/v1")

    product = _expect_object(contract.get("product"), "$.product", errors)
    if product is not None:
        _allowed(product, ["name", "client", "source_of_truth", "version"], "$.product", errors)
        _required(product, ["name", "client", "source_of_truth"], "$.product", errors)
        if product.get("name") != "AoiTalk":
            errors.append("$.product.name: must be AoiTalk")
        if product.get("client") != "mobile":
            errors.append("$.product.client: must be mobile")
        if product.get("source_of_truth") != "contracts/product-contract.json":
            errors.append("$.product.source_of_truth: must point to contracts/product-contract.json")

    navigation = _expect_object(contract.get("navigation"), "$.navigation", errors)
    route_index: dict[str, dict[str, Any]] = {}
    entry_index: dict[str, dict[str, Any]] = {}
    if navigation is not None:
        _allowed(navigation, ["tabs", "tab_routes", "routes", "entry_points"], "$.navigation", errors)
        _required(navigation, ["tabs", "tab_routes", "routes", "entry_points"], "$.navigation", errors)
        tabs = navigation.get("tabs")
        if not isinstance(tabs, list) or not tabs or not all(isinstance(x, str) and x for x in tabs):
            errors.append("$.navigation.tabs: expected a non-empty string array")
        elif len(set(tabs)) != len(tabs):
            errors.append("$.navigation.tabs: duplicate tab")
        tab_routes = navigation.get("tab_routes")
        if not isinstance(tab_routes, list) or not tab_routes or not all(isinstance(x, str) for x in tab_routes):
            errors.append("$.navigation.tab_routes: expected a non-empty string array")
            tab_routes = []
        routes = navigation.get("routes")
        if not isinstance(routes, list) or not routes:
            errors.append("$.navigation.routes: expected a non-empty array")
            routes = []
        route_index = _unique_ids(routes, "$.navigation.routes", errors)
        entries = navigation.get("entry_points")
        if not isinstance(entries, list) or not entries:
            errors.append("$.navigation.entry_points: expected a non-empty array")
            entries = []
        entry_index = _unique_ids(entries, "$.navigation.entry_points", errors)

        expected_tab_routes = [f"tabs.{tab}" for tab in tabs] if isinstance(tabs, list) else []
        if tab_routes != expected_tab_routes:
            errors.append(
                "$.navigation.tab_routes: must exactly match tabs as tabs.<name> "
                f"(expected {expected_tab_routes!r})"
            )

        for rid, route in route_index.items():
            location = f"$.navigation.routes[{rid!r}]"
            _allowed(route, ["id", "path", "kind", "auth_required", "tab", "entry_points", "notes"], location, errors)
            _required(route, ["path", "kind", "auth_required", "tab", "entry_points"], location, errors)
            _string(route.get("path"), f"{location}.path", errors)
            if not isinstance(route.get("auth_required"), bool):
                errors.append(f"{location}.auth_required: expected boolean")
            if not isinstance(route.get("tab"), bool):
                errors.append(f"{location}.tab: expected boolean")
            route_entries = route.get("entry_points")
            if not isinstance(route_entries, list) or not route_entries:
                errors.append(f"{location}.entry_points: route must have at least one entry point")
            else:
                for eid in route_entries:
                    if eid not in entry_index:
                        errors.append(f"{location}.entry_points: unknown entry point {eid!r}")
            if bool(route.get("tab")) != (rid in tab_routes):
                errors.append(f"{location}.tab: unexpected bottom-tab membership for {rid!r}")

        for eid, entry in entry_index.items():
            location = f"$.navigation.entry_points[{eid!r}]"
            _allowed(entry, ["id", "kind", "target", "from", "notes"], location, errors)
            _required(entry, ["kind", "target"], location, errors)
            if entry.get("target") not in route_index:
                errors.append(f"{location}.target: unknown route {entry.get('target')!r}")

        for rid in tab_routes:
            route = route_index.get(rid)
            if route is None:
                errors.append(f"$.navigation.tab_routes: unknown route {rid!r}")
            elif route.get("kind") != "tab":
                errors.append(f"$.navigation.tab_routes: route {rid!r} must have kind=tab")

    capabilities = contract.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        errors.append("$.capabilities: expected a non-empty array")
    else:
        seen: set[str] = set()
        for i, item in enumerate(capabilities):
            location = f"$.capabilities[{i}]"
            cap = _expect_object(item, location, errors)
            if cap is None:
                continue
            _required(
                cap,
                [
                    "id", "domain", "criticality", "transport", "operations", "mobile", "roles",
                    "permissions", "scope", "offline", "persistence", "mutation", "acceptance",
                ],
                location,
                errors,
            )
            cid = cap.get("id")
            if not _string(cid, f"{location}.id", errors):
                continue
            if cid in seen:
                errors.append(f"$.capabilities: duplicate id {cid!r}")
            seen.add(cid)
            _allowed(
                cap,
                [
                    "id", "domain", "criticality", "transport", "operations", "mobile", "roles",
                    "permissions", "scope", "offline", "persistence", "mutation", "acceptance", "notes",
                ],
                location,
                errors,
            )
            if cap.get("criticality") not in {"p0", "p1", "p2"}:
                errors.append(f"{location}.criticality: invalid value")
            if cap.get("transport") not in {"fastapi", "sync", "local", "external", "next_bff"}:
                errors.append(f"{location}.transport: invalid value")
            if not isinstance(cap.get("operations"), list):
                errors.append(f"{location}.operations: expected array")
            mobile = _expect_object(cap.get("mobile"), f"{location}.mobile", errors)
            if mobile is not None:
                _allowed(mobile, ["routes", "entry_points", "implementation"], f"{location}.mobile", errors)
                _required(mobile, ["routes", "entry_points", "implementation"], f"{location}.mobile", errors)
            _expect_object(cap.get("scope"), f"{location}.scope", errors)
            _expect_object(cap.get("offline"), f"{location}.offline", errors)
            for key in ("roles", "permissions", "acceptance"):
                if not isinstance(cap.get(key), list) or not cap.get(key):
                    errors.append(f"{location}.{key}: expected non-empty array")
    return errors


def _operation_index(openapi: Mapping[str, Any], errors: list[str]) -> dict[str, tuple[str, str]]:
    paths = openapi.get("paths")
    if not isinstance(paths, dict):
        errors.append("OpenAPI.paths: expected object")
        return {}
    operation_index: dict[str, tuple[str, str]] = {}
    for path, path_item in sorted(paths.items()):
        if not isinstance(path_item, dict):
            continue
        for method, operation in sorted(path_item.items()):
            method_upper = method.upper()
            if method_upper not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                errors.append(f"OpenAPI {method_upper} {path}: missing operationId")
                continue
            if operation_id in operation_index:
                old_method, old_path = operation_index[operation_id]
                errors.append(
                    f"OpenAPI operationId {operation_id!r} is duplicated at "
                    f"{old_method} {old_path} and {method_upper} {path}"
                )
            else:
                operation_index[operation_id] = (method_upper, path)
    return operation_index


def _iter_route_values(value: Any, location: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _ROUTE_KEYS and isinstance(child, str):
                yield location + "." + key, child
            yield from _iter_route_values(child, f"{location}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _iter_route_values(child, f"{location}[{i}]")


def _physical_mobile_routes(repo_root: Path) -> set[str] | None:
    app_root = repo_root / "mobile" / "src" / "app"
    if not app_root.is_dir():
        return None
    result: set[str] = set()
    for source in app_root.rglob("*"):
        if not source.is_file() or source.suffix not in {".ts", ".tsx"}:
            continue
        rel_parts = source.relative_to(app_root).parts
        if "__tests__" in rel_parts or source.stem == "_layout":
            continue
        result.add(source.relative_to(app_root).with_suffix("").as_posix())
    return result


def validate_contract(
    contract: Mapping[str, Any],
    openapi: Mapping[str, Any],
    *,
    repo_root: Path | None = REPO_ROOT,
    check_routes: bool = True,
) -> list[str]:
    """Return deterministic validation errors (empty means PASS).

    ``repo_root=None`` disables filesystem checks, which is useful for focused
    unit tests.  OpenAPI and contract checks always run.
    """

    errors = _shape_errors(contract)
    navigation = contract.get("navigation") if isinstance(contract.get("navigation"), dict) else {}
    route_index = {
        item.get("id"): item
        for item in navigation.get("routes", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    entry_index = {
        item.get("id"): item
        for item in navigation.get("entry_points", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    operation_index = _operation_index(openapi, errors)

    if check_routes and repo_root is not None and route_index:
        physical_routes = _physical_mobile_routes(repo_root)
        if physical_routes is not None:
            declared_paths = {
                route.get("path")
                for route in route_index.values()
                if isinstance(route.get("path"), str)
            }
            for path in sorted(physical_routes - declared_paths):
                errors.append(f"navigation orphan route: {path}")
            for path in sorted(declared_paths - physical_routes):
                errors.append(f"navigation route has no screen file: {path}")

    capabilities = contract.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []
    for i, raw_capability in enumerate(capabilities):
        if not isinstance(raw_capability, dict):
            continue
        cid = raw_capability.get("id", f"#{i}")
        location = f"capability {cid!r}"
        cap_transport = raw_capability.get("transport")
        criticality = raw_capability.get("criticality")
        operations = raw_capability.get("operations")
        if not isinstance(operations, list):
            operations = []
        if criticality == "p0" and cap_transport == "next_bff":
            errors.append(f"{location}: critical transport cannot be next_bff")
        if criticality == "p0" and not operations and cap_transport != "local":
            errors.append(f"{location}: p0 capability must declare at least one API operation")
        declared_transports: set[str] = set()
        for op_index, raw_operation in enumerate(operations):
            if not isinstance(raw_operation, dict):
                continue
            op_location = f"{location}.operations[{op_index}]"
            _allowed(raw_operation, ["operation_id", "transport", "method", "role"], op_location, errors)
            for route_location, route_value in _iter_route_values(raw_operation, op_location):
                errors.append(f"{route_location}: raw route is forbidden; reference operationId instead ({route_value!r})")
            operation_id = raw_operation.get("operation_id")
            transport = raw_operation.get("transport")
            declared_transports.add(transport)
            if not isinstance(operation_id, str) or not operation_id:
                continue
            if transport == "next_bff" and criticality == "p0":
                errors.append(f"{location}: critical operation {operation_id!r} uses next_bff")
            if transport in {"fastapi", "sync"}:
                if operation_id not in operation_index:
                    errors.append(f"{location}: operationId {operation_id!r} is absent from OpenAPI")
                expected_method = raw_operation.get("method")
                if expected_method in _HTTP_METHODS and operation_id in operation_index:
                    actual_method, _ = operation_index[operation_id]
                    if actual_method != expected_method:
                        errors.append(
                            f"{location}: operationId {operation_id!r} method mismatch "
                            f"(contract={expected_method}, openapi={actual_method})"
                        )
                if transport == "sync" and operation_id in operation_index:
                    _, path = operation_index[operation_id]
                    if not path.startswith("/api/sync/"):
                        errors.append(f"{location}: sync operation {operation_id!r} is not under /api/sync/")
            elif transport == "local" and operation_id:
                errors.append(f"{location}: local operation must not carry operationId")
            elif transport not in {"local", "external", "next_bff", "fastapi", "sync"}:
                errors.append(f"{location}: unknown transport {transport!r}")
        if operations and cap_transport not in declared_transports:
            errors.append(
                f"{location}: capability transport {cap_transport!r} does not match operation transports "
                f"{sorted(declared_transports)!r}"
            )
        if len(declared_transports) > 1 and cap_transport not in declared_transports:
            errors.append(f"{location}: mixed transports require capability transport to be one of them")

        mobile = raw_capability.get("mobile")
        if isinstance(mobile, dict):
            routes = mobile.get("routes")
            entries = mobile.get("entry_points")
            implementation = mobile.get("implementation")
            if isinstance(routes, list):
                for rid in routes:
                    if rid not in route_index:
                        errors.append(f"{location}: unknown mobile route {rid!r}")
            if isinstance(entries, list):
                for eid in entries:
                    if eid not in entry_index:
                        errors.append(f"{location}: unknown mobile entry point {eid!r}")
                if not entries:
                    errors.append(f"{location}: capability must have at least one entry point")
            if isinstance(routes, list) and isinstance(entries, list) and route_index and entry_index:
                reachable = False
                for rid in routes:
                    route = route_index.get(rid)
                    if not route:
                        continue
                    route_entries = set(route.get("entry_points") or [])
                    if route_entries.intersection(entries):
                        reachable = True
                        break
                    for eid in entries:
                        entry = entry_index.get(eid)
                        if entry and entry.get("target") == rid:
                            reachable = True
                            break
                if not reachable:
                    errors.append(f"{location}: no declared entry point reaches a capability route")
            if repo_root is not None and isinstance(implementation, str) and implementation:
                implementation_path = repo_root / implementation
                if not implementation_path.is_file():
                    errors.append(f"{location}: implementation file does not exist: {implementation}")

        scope = raw_capability.get("scope")
        if isinstance(scope, dict):
            _allowed(
                scope,
                ["kind", "visibility", "project_parameter", "space_parameter", "notes"],
                f"{location}.scope",
                errors,
            )
            scope_kind = scope.get("kind")
            visibility = scope.get("visibility")
            if scope_kind not in {"public", "auth_user"} and visibility == "public":
                errors.append(f"{location}: scoped capability cannot declare public visibility")
            if scope_kind in {"project", "space", "space_project", "conversation", "task"} and visibility == "auth_user":
                errors.append(f"{location}: resource capability must declare ACL visibility, not auth_user")

        offline = raw_capability.get("offline")
        if isinstance(offline, dict):
            _allowed(offline, ["read", "mutation", "reconnect"], f"{location}.offline", errors)

        acceptance = raw_capability.get("acceptance")
        if isinstance(acceptance, list):
            for acceptance_index, scenario in enumerate(acceptance):
                if isinstance(scenario, dict):
                    _allowed(
                        scenario,
                        ["id", "given", "when", "then"],
                        f"{location}.acceptance[{acceptance_index}]",
                        errors,
                    )

        if criticality == "p0" and raw_capability.get("transport") == "next_bff":
            errors.append(f"{location}: p0 transport next_bff is forbidden")

        # API references may only contain operationId/transport metadata.  A
        # route-like key anywhere in a capability (outside the explicit
        # mobile.routes registry) is a contract authoring error.
        for route_location, route_value in _iter_route_values(raw_capability, location):
            if ".mobile.routes" not in route_location:
                errors.append(
                    f"{route_location}: raw route is forbidden; reference operationId instead ({route_value!r})"
                )

    return sorted(set(errors))


def validate_or_raise(contract: Mapping[str, Any], openapi: Mapping[str, Any], **kwargs: Any) -> None:
    errors = validate_contract(contract, openapi, **kwargs)
    if errors:
        raise ContractValidationError("Mobile Product Contract validation failed:\n- " + "\n- ".join(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate AoiTalk Mobile Product Contract")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--openapi", type=Path, default=DEFAULT_OPENAPI)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--skip-route-files", action="store_true", help="skip physical Expo route/orphan checks")
    args = parser.parse_args(argv)

    try:
        contract = _read_json(args.contract)
        # Read the schema even though the dependency-free structural check is
        # authoritative here; a malformed schema must fail the gate as well.
        _read_json(args.schema)
        openapi = _read_json(args.openapi)
        errors = validate_contract(
            contract,
            openapi,
            repo_root=args.repo_root,
            check_routes=not args.skip_route_files,
        )
    except ContractValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if errors:
        print("Mobile Product Contract: FAIL", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "Mobile Product Contract: PASS "
        f"({len(contract.get('capabilities', []))} capabilities, "
        f"{len(contract.get('navigation', {}).get('routes', []))} routes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
