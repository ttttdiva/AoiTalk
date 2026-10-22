"""Bounded, data-only See-through ABI 1 packages. No inference/runtime imports."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import struct
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

FORMAT = "seethrough-rig"
MAX_UPLOAD = 64 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_JSON = 24 * 1024 * 1024
MAX_FILES = 64
HEX = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
PARAMS = frozenset("AngleX AngleY AngleZ BodyAngleX BodyAngleY BodyAngleZ Breath EyeLOpen EyeROpen EyeBallX EyeBallY EyeHappy EyeJitome BrowLY BrowRY BrowLAngle BrowRAngle BrowHappy MouthOpenY MouthForm MouthHappy WindBlow Wind PhysicsGain EyeSurprised EyeWorried EyeShy BrowSurprised BrowWorried BrowShy MouthSurprised MouthWorried MouthShy MouthVowel".split())


class PackageError(ValueError):
    pass


def fail(message):
    raise PackageError(message)


def flat_name(value):
    reserved = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
    if (not isinstance(value, str) or not NAME.fullmatch(value)
            or value.endswith((".", " ")) or ".." in value or value.split(".")[0].upper() in reserved):
        fail("パッケージ内のファイル名が不正です")
    return value


def obj(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        fail("未対応のデータフィールドまたは必須フィールド不足です")
    return value


def integer(value, low=0, high=1_000_000):
    if type(value) is not int or not low <= value <= high:
        fail("整数の範囲が不正です")
    return value


def number(value, low=-1_000_000, high=1_000_000):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        fail("数値の範囲が不正です")
    return value


def array(value, low=0, high=10000):
    if not isinstance(value, list) or not low <= len(value) <= high:
        fail("配列長が不正です")
    return value


def label(value, maximum=160):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 for c in value) or value in {"__proto__", "constructor", "prototype"}:
        fail("識別子が不正です")
    return value


def vector(value, n=2):
    for x in array(value, n, n):
        number(x)
    return value


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value or key in {"__proto__", "prototype", "constructor"}:
            fail("重複キーまたはprototypeキーは禁止されています")
        value[key] = child
    return value


def json_data(data):
    if len(data) > MAX_JSON:
        fail("JSONが大きすぎます")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: fail("非有限数は禁止されています"))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PackageError("JSONを読み込めません") from exc
    budget = [0]

    def walk(v, depth=0, binary_value=False):
        budget[0] += 1
        if depth > 32 or budget[0] > 2_000_000:
            fail("JSON構造の上限を超えています")
        if isinstance(v, dict):
            if len(v) > 4096:
                fail("JSONキー数の上限を超えています")
            for k, child in v.items():
                if len(k) > 160 or k in {"__proto__", "constructor", "prototype"}:
                    fail("不正なキーです")
                walk(child, depth + 1, binary_value=k == "b64")
        elif isinstance(v, list):
            if len(v) > 500_000:
                fail("配列が大きすぎます")
            for child in v:
                walk(child, depth + 1)
        elif type(v) in (int, float):
            number(v, -1e9, 1e9)
        elif isinstance(v, str):
            if len(v) > 16 * 1024 * 1024 or (not binary_value and re.search(r"(?:https?|ftp|file|data|javascript|vbscript):|[<>]|^[A-Za-z]:[\\/]|^[/\\]", v, re.I)):
                fail("URL・コード・絶対パスは禁止されています")
    walk(value)
    return value


def binary(value, dtype, length=None):
    if isinstance(value, list):
        values = array(value, 0, 500_000)
        for x in values:
            integer(x, 0, 2**32 - 1) if dtype in {"index", "u16", "u32"} else number(x)
        if dtype == "u16" and any(x > 65535 for x in values):
            fail("u16の範囲を超えています")
    else:
        obj(value, {"b64", "dtype"}, {"b64", "dtype"})
        actual = value["dtype"]
        if actual not in ({"u16", "u32"} if dtype == "index" else {dtype}):
            fail("未対応のbinary型です")
        if not isinstance(value["b64"], str):
            fail("binary値が不正です")
        try:
            raw = base64.b64decode(value["b64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PackageError("base64が不正です") from exc
        size, code = (2, "H") if actual == "u16" else (4, "I" if actual == "u32" else "f")
        if len(raw) > 2_000_000 or len(raw) % size:
            fail("binaryバイト長が不正です")
        values = [v[0] for v in struct.iter_unpack("<" + code, raw)]
        for x in values:
            number(x) if actual == "f32" else integer(x, 0, 2**32 - 1)
    if length is not None and len(values) != length:
        fail("頂点数とbinary配列長が一致しません")
    return values


def image_size(data):
    strict_image_container(data)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "WEBP"} or not 1 <= image.width <= 8192 or not 1 <= image.height <= 8192 or image.width * image.height > 32_000_000:
                fail("画像の形式・画素数が不正です")
            dimensions = image.size
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return dimensions
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PackageError("画像を読み込めません") from exc


RIG_KEYS = {
    "head": "center pivot radius rigidPivot yawScale pitchScale rollScale focal shift mode bob hang_rotate body_rigid depthScale foreshorten",
    "body": "axisX pivot twistScale pitchScale leanScale breathLift breathWiden shift stagePivot depthScale depthDrop sway rigid",
    "eyes": "L R",
    "brows": "L R",
    "mouth": "center range width height open form maxOpen",
    "landmarks": "face_box center_x head_top eye_y ear_y chin_y neck_box shoulder_y waist_y hip_y hem_y bottom_y head_center head_radius neck_pivot body_radius",
}


def numeric_object(value, allowed):
    obj(value, allowed)
    for v in value.values():
        number(v)


def validate_rig(rig, partial=False):
    obj(rig, RIG_KEYS, () if partial else {"head", "body"})
    vectors = {"center": 2, "pivot": 2, "rigidPivot": 2, "stagePivot": 2,
               "irisCenter": 2, "irisRange": 2, "face_box": 4, "neck_box": 4,
               "head_center": 2, "neck_pivot": 2}
    for key, value in rig.items():
        obj(value, RIG_KEYS[key].split())
        if key in {"eyes", "brows"}:
            for side in value.values():
                allowed = ("x0 step up low margin irisCenter irisRange" if key == "eyes" else "center range maxAngle blinkDrop").split()
                obj(side, [*allowed, "continuous"] if key == "eyes" else allowed, () if partial else allowed)
                for field, val in side.items():
                    if field == "continuous":
                        validate_continuous_definition(val, partial=partial)
                    elif field in vectors:
                        vector(val, vectors[field])
                    elif field in {"up", "low"}:
                        for x in array(val, 2, 128):
                            number(x)
                    else:
                        number(val, .000001 if field == "step" else -1_000_000)
                if key == "eyes" and not partial and len(side.get("up", [])) != len(side.get("low", [])):
                    fail("目の曲線長が一致しません")
        else:
            for field, val in value.items():
                if field in vectors:
                    vector(val, vectors[field])
                elif field in {"radius", "head_radius"} or (field == "shift" and key == "head"):
                    for x in array(val, 2, 3):
                        number(x)
                elif field == "mode":
                    if val not in {"rigid", "parallax", "surface"}:
                        fail("未対応の頭部modeです")
                elif field in {"hang_rotate", "body_rigid"}:
                    for x in array(val, 0, 512):
                        label(x)
                elif field == "foreshorten":
                    numeric_object(val, {"k", "min", "max"})
                elif field == "rigid":
                    if type(val) is not bool:
                        fail("rigidはbooleanが必要です")
                else:
                    number(val)
            if key == "head" and not partial:
                for field in ("center", "pivot", "radius", "shift", "yawScale", "pitchScale", "rollScale"):
                    if field not in value:
                        fail("頭部rigが不足しています")
                number(value.get("focal"), 1, 1_000_000)
            if key == "body" and not partial:
                for field in ("axisX", "pivot", "twistScale", "pitchScale", "leanScale", "breathLift", "breathWiden"):
                    if field not in value:
                        fail("body rigが不足しています")


def opacity(value):
    bound = set()
    for term in array(value, 0, 32):
        obj(term, {"param", "keys", "sum", "oneMinusSum", "lo", "hi"})
        if "keys" in term:
            if term.get("param") not in PARAMS:
                fail("opacity parameterが未対応です")
            points = array(term["keys"], 2, 64)
            previous = -math.inf
            for p in points:
                vector(p)
                if p[0] <= previous or not 0 <= p[1] <= 1:
                    fail("opacity key順序・値が不正です")
                if term["param"] == "MouthVowel":
                    integer(p[0], 0, 6)  # 6 is the off sentinel after vowel o.
                previous = p[0]
            if len({p[1] for p in points}) > 1: bound.add(term["param"])
        elif "sum" in term or "oneMinusSum" in term:
            for p in array(term.get("sum", term.get("oneMinusSum")), 1, 32):
                if p not in PARAMS:
                    fail("opacity parameterが未対応です")
                if p == "MouthVowel":
                    fail("MouthVowel requires discrete keys")
                bound.add(p)
            for field in ("lo", "hi"):
                if field in term:
                    number(term[field], -100, 100)
        else:
            fail("opacity bindingが不足しています")
    return bound


def expressions(values):
    ids = set()
    for exp in array(values, 0, 64):
        obj(exp, {"id", "label", "params"}, {"id", "params"})
        eid = label(exp["id"])
        if eid in ids:
            fail("表情idが重複しています")
        ids.add(eid)
        if "label" in exp: label(exp["label"])
        obj(exp["params"], PARAMS)
        for key, v in exp["params"].items():
            integer(v, 0, 5) if key == "MouthVowel" else number(v, -100, 100)
    return ids


def mouth_vowels(model):
    """Derive optional ABI1 vowel controls from exclusive open/closed bindings.

    This is a data contract probe; the canonical runtime independently probes
    the real renderer. No declared capability can grant unsupported input.
    """
    def alpha(drawable, vowel, opened):
        # Nonzero canonical PARAM_DEFS defaults; all other known controls are 0.
        params = {"EyeLOpen": 1, "EyeROpen": 1, "Wind": .35, "PhysicsGain": 1,
                  "MouthVowel": vowel, "MouthOpenY": opened}
        result = drawable.get("baseOpacity", 1.0)
        for term in drawable.get("opacity", []):
            if "keys" in term:
                x, points = params.get(term["param"], 0), term["keys"]
                value = points[0][1] if x <= points[0][0] else points[-1][1]
                for left, right in zip(points, points[1:]):
                    if left[0] <= x <= right[0]:
                        t = (x - left[0]) / (right[0] - left[0])
                        value = left[1] + t * (right[1] - left[1])
                        break
            else:
                values = term.get("oneMinusSum", term.get("sum", []))
                value = sum(params.get(key, 0) for key in values)
                if "oneMinusSum" in term:
                    lo, hi = term.get("lo", .85), term.get("hi", 1)
                    if hi == lo:
                        t = math.nan if value == lo else (1 if value > lo else 0)
                    else:
                        t = max(0, min(1, (value - lo) / (hi - lo)))
                    value = 1 - t * t * (3 - 2 * t)
                else:
                    value = max(0, min(1, value))
            result *= value
        return math.nan if math.isnan(result) else max(0, min(1, result))
    mouths = [d for d in model["drawables"] if d["id"] == "mouth" or d["id"].startswith("mouth-")]
    supported = []
    for index, vowel in enumerate(("a", "i", "u", "e", "o"), 1):
        expected = "mouth-vowel_" + vowel
        opened = [(d["id"], alpha(d, index, 1)) for d in mouths if alpha(d, index, 1) > .001]
        closed = [(d["id"], alpha(d, index, 0)) for d in mouths if alpha(d, index, 0) > .001]
        if (len(opened) == 1 and opened[0][0] == expected and opened[0][1] >= .999
                and len(closed) == 1 and not closed[0][0].startswith("mouth-vowel_") and closed[0][1] >= .999):
            supported.append(vowel)
    return supported


CONTINUOUS_SHAPES = {"open", "half", "closed", "jitome", "happy", "surprised", "worried", "shy"}


def validate_continuous_definition(definition, partial=False):
    """Whitelist every nested field, including partial editor settings."""
    obj(definition, {"version", "contours", "halfOpen", "browJitomeDrop", "sources", "strokeGuides", "underLashCoverage"},
        () if partial else {"version", "contours"})
    if "version" in definition:
        integer(definition["version"], 1, 3)
    if "underLashCoverage" in definition:
        coverage = obj(definition["underLashCoverage"], {"marginPx"}, {"marginPx"})
        if number(coverage["marginPx"], 0, 2) == 0:
            fail("underLashCoverage marginPxは0より大きく2以下の値が必要です")
    if "strokeGuides" in definition:
        obj(definition["strokeGuides"], CONTINUOUS_SHAPES,
            () if partial else {"open", "half", "closed", "jitome", "happy"})
        for points in definition["strokeGuides"].values():
            for point in array(points, 14, 518):
                vector(point)
            if len(points) % 2:
                fail("strokeGuidesの頂点数が不正です")
    if not partial:
        extension_fields = {"strokeGuides", "underLashCoverage"} & set(definition)
        if (definition["version"] == 3) != bool(extension_fields):
            fail("strokeGuidesまたはunderLashCoverageにはcontinuous version 3が必要です")
    if "halfOpen" in definition:
        number(definition["halfOpen"], 0, 1)
        if definition["halfOpen"] in (0, 1):
            fail("continuous halfOpenは0より大きく1より小さい値が必要です")
    if "browJitomeDrop" in definition:
        number(definition["browJitomeDrop"])
    if "sources" in definition:
        obj(definition["sources"], CONTINUOUS_SHAPES)
        for source in definition["sources"].values():
            label(source, 120)
    if "contours" in definition:
        obj(definition["contours"], CONTINUOUS_SHAPES,
            () if partial else {"open", "half", "closed", "jitome", "happy"})
        for points in definition["contours"].values():
            for point in array(points, 8, 512):
                vector(point)
            if len(points) % 2:
                fail("continuous contourの頂点数が不正です")


def validate_continuous_rounding_clearance(positions, endpoints, triangles):
    """Certify v3's source-space convex hull against Float32 rounding.

    X is fixed, so each canonical area is affine in the endpoint Y values.
    A full maximum-range float32 ULP plus conservative double arithmetic
    bounds certifies the space between keys; endpoint sampling alone cannot.
    Keep this formula identical to the canonical JS/receiving validator.
    This is not a guarantee after arbitrary head/body/camera transforms and
    their final Float32 upload, which still require rendering verification.
    """
    def require(condition, message):
        if not condition:
            fail(message)

    eps = 2.0 ** -52
    maxima = [max(abs(endpoint[2*i+1]) for endpoint in endpoints) for i in range(len(positions)//2)]
    errors = []
    for maximum in maxima:
        double_error = 128 * eps * max(1, maximum)
        bits = struct.unpack('<I', struct.pack('<f', maximum + double_error))[0]
        exponent = (bits >> 23) & 255
        ulp = 2.0 ** (exponent - 150 if exponent else -149)
        errors.append(ulp + double_error)
    for a, b, c in triangles:
        ids = (a, b, c)
        xs = [positions[2*i] for i in ids]
        coefficients = (xs[2] - xs[1], xs[0] - xs[2], xs[1] - xs[0])
        bound = 0.
        for j, i in enumerate(ids):
            bound += abs(coefficients[j]) * errors[i]
            bound += 128 * eps * (abs(xs[(j+1)%3]) + abs(xs[(j+2)%3])) * max(1, maxima[i])
        for endpoint in endpoints:
            # Explicit accumulation matches JS exactly; Python's sum may use
            # compensated addition, changing decisions at the bound itself.
            area = 0.
            for coefficient, i in zip(coefficients, ids):
                area += coefficient * endpoint[2*i+1]
            require(area > bound and area > 1e-9, 'continuous ring lacks Float32 interpolation rounding clearance')


def validate_continuous_eyes(model):
    """Bounded opt-in geometry validation; stdlib only at the package boundary.

    All vertex X values stay fixed, so oriented triangle areas are affine in
    the convex key weights. Checking all keys certifies intermediate mixtures.
    """
    def require(condition, message):
        if not condition:
            fail(message)

    def _unit(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1

    def f32_values(value, count):
        # The renderer decodes both plain arrays and binary payloads to Float32.
        # Certify the same coordinates/weights, never the pre-conversion doubles.
        values = binary(value, "f32", count)
        return list(struct.unpack(f"<{len(values)}f", struct.pack(f"<{len(values)}f", *values)))

    def f32_point(point):
        return list(struct.unpack("<2f", struct.pack("<2f", *point)))

    eyes = model.get('rig', {}).get('eyes', {})
    enabled = {}
    shapes = {'open', 'half', 'closed', 'jitome', 'happy', 'surprised', 'worried', 'shy'}
    def numeric(x):
        return type(x) in (int, float) and math.isfinite(x) and abs(x) <= 1e9
    def near(x, y):
        return abs(x - y) <= 1e-4
    def area(p, a, b, c):
        return ((p[2*b] - p[2*a]) * (p[2*c+1] - p[2*a+1])
                - (p[2*b+1] - p[2*a+1]) * (p[2*c] - p[2*a]))
    for side, eye in eyes.items():
        require(isinstance(eye, dict), 'invalid eye rig')
        if 'continuous' not in eye:
            continue
        require(side in ('L', 'R'), 'invalid continuous eye side')
        definition = eye['continuous']
        validate_continuous_definition(definition)
        require(isinstance(definition, dict) and type(definition.get('version')) is int and definition['version'] in (1, 2, 3),
                'invalid continuous eye version')
        require(set(definition) <= {'version', 'contours', 'halfOpen', 'browJitomeDrop', 'sources', 'underLashCoverage', 'strokeGuides'},
                'unknown continuous eye definition field')
        contours = definition.get('contours')
        require(isinstance(contours, dict) and {'open', 'half', 'closed', 'jitome', 'happy'} <= set(contours) <= shapes,
                'invalid continuous eye contours')
        sources = definition.get('sources', {})
        require(isinstance(sources, dict) and set(sources) <= set(contours)
                and all(isinstance(value, str) and 0 < len(value) <= 120 for value in sources.values()),
                'invalid continuous eye sources')
        rest = contours['open']
        require(isinstance(rest, list) and 8 <= len(rest) <= 512 and len(rest) % 2 == 0, 'invalid contour point count')
        n, mid = len(rest), len(rest) // 2
        for name, points in contours.items():
            require(isinstance(points, list) and len(points) == n, 'contour correspondence length')
            require(all(isinstance(p, list) and len(p) == 2 and all(numeric(x) for x in p) for p in points),
                    'invalid contour point')
        for name, points in contours.items():
            require(all(p[0] == rest[i][0] for i, p in enumerate(points)), 'contour X correspondence')
            require(all(points[i][0] > points[i-1][0] + 1e-6 for i in range(1, mid+1)), 'contour X order')
            for i in range(1, mid):
                require(points[i][0] == points[n-i][0], 'upper/lower X correspondence')
                gap = points[n-i][1] - points[i][1]
                require(gap >= -1e-5, 'crossed contour')
                if definition['version'] == 3:
                    decoded = f32_point([points[i][1], points[n-i][1]])
                    require(gap >= 0 and decoded[1] >= decoded[0], 'crossed Float32 continuous contour')
                    if name == 'closed':
                        require(decoded[1] == decoded[0], 'closed Float32 contour has aperture')
                if name == 'open':
                    require(gap > 1e-6, 'collapsed open contour')
                if name == 'closed':
                    require(abs(gap) <= 1e-6, 'closed contour has aperture')
        if 'strokeGuides' in definition:
            guides = definition['strokeGuides']
            require(set(guides) == set(contours), 'stroke guides must cover every contour key')
            require(all(len(points) == n + 6 for points in guides.values()), 'stroke guide correspondence length')
            for name, points in guides.items():
                require(all(point[0] == guides['open'][i][0] for i, point in enumerate(points)),
                        'stroke guide X correspondence')
                for i in range(mid + 1):
                    lower = i if i in (0, mid) else n - i
                    require(points[i][0] == rest[i][0] and points[mid + 1 + i][0] == rest[i][0]
                            and points[i][1] < contours[name][i][1]
                            and points[mid + 1 + i][1] > contours[name][lower][1],
                            'stroke guides must surround the aperture')
                tl, tr, br, bl = points[n + 2:]
                require(tl[0] < rest[0][0] and tr[0] > rest[mid][0] and tl[0] == bl[0] and tr[0] == br[0]
                        and tl[1] < contours[name][0][1] < bl[1]
                        and tr[1] < contours[name][mid][1] < br[1], 'stroke guide wing bounds')
        half, drop = definition.get('halfOpen', 0.5), definition.get('browJitomeDrop', 0)
        require(numeric(half) and 0 < half < 1, 'invalid continuous halfOpen')
        require(numeric(drop) and abs(drop) <= model['canvas']['height'], 'invalid continuous brow drop')
        enabled[side] = definition
    if not enabled:
        uses_jitome = any(
            "EyeJitome" in d.get("keyforms", {}) or any(
                term.get("param") == "EyeJitome" or "EyeJitome" in term.get("sum", term.get("oneMinusSum", []))
                for term in d.get("opacity", [])
            ) for d in model["drawables"]
        ) or any("EyeJitome" in chain.get("keyforms", {}) for chain in model.get("chains", [])) or any(exp.get("params", {}).get("EyeJitome", 0) != 0 for exp in model.get("expressions", []))
        require(not uses_jitome, "EyeJitome requires continuous eye geometry")
    if enabled:
        require(model['rig'].get('head', {}).get('mode') == 'rigid' and model['rig'].get('body', {}).get('rigid') is True,
                'continuous eyes require rigid head/body')
    by_id = {d['id']: d for d in model['drawables']}
    roles = {side: [] for side in enabled}
    side_breath = {}
    registered_ring_ids = set()
    art_changes = {side: set() for side in enabled}
    for d in model['drawables']:
        if 'eyeRig' not in d:
            continue
        binding = d['eyeRig']
        require(isinstance(binding, dict) and set(binding) <= {'side', 'role', 'indices', 'weights', 'artKeys'}
                and isinstance(binding.get('side'), str) and binding['side'] in enabled
                and binding.get('role') in ('ring', 'mask', 'iris', 'white'),
                'invalid eyeRig binding')
        side, role = binding['side'], binding['role']
        roles[side].append(role)
        definition = enabled[side]
        contours, rest = definition['contours'], definition['contours']['open']
        def matches(x, y):
            return (x == struct.unpack('<f', struct.pack('<f', y))[0]
                    if definition['version'] == 3 else near(x, y))
        require(role == 'mask' or not d.get('isMask'), 'visible continuous eye art cannot be a mask')
        require(not d.get('strandIds') and not d.get('keyforms') and not d.get('sub') and not d.get('morph'),
                'incompatible continuous eye deformation')
        require(all(df.get('type') == 'iris' and role == 'iris' and df.get('eye') == side for df in d.get('deformers', [])),
                'incompatible continuous local deformer')
        n = d['vertexCount']
        require(n <= 20000, 'continuous eye mesh vertex budget')
        attrs = d.get('attrs', {})
        head = f32_values(attrs.get('head'), n)
        breath = f32_values(attrs.get('breath'), n)
        require(all(w == 1 for w in head), 'continuous eye requires full head attachment')
        side_breath.setdefault(side, breath[0])
        require(all(w == side_breath[side] for w in breath), 'continuous eye requires shared rigid breath weights')
        if definition["version"] >= 2 and role == "ring":
            registered_ring_ids.add(d["id"])
            art_keys = obj(binding.get("artKeys"), set(contours), set(contours))
            open_uv = f32_values(d["uvs"], n * 2)
            for name, art in art_keys.items():
                obj(art, {"atlas", "uvs"}, {"atlas", "uvs"})
                atlas = integer(art["atlas"], 0, len(model["atlases"]) - 1)
                uv = f32_values(art["uvs"], n * 2)
                require(all(0 <= value <= 1 for value in uv), "registered eye UV is out of range")
                if name == "open":
                    require(atlas == d["atlas"] and uv == open_uv,
                            "registered open artwork must match drawable atlas and UV")
                elif atlas != d["atlas"] or uv != open_uv:
                    art_changes[side].add(name)
        else:
            require("artKeys" not in binding, "registered eye artwork requires a v2/v3 ring")
        pos = f32_values(d['positions'], n * 2)
        ix = binary(d['indices'], 'index')
        if role in ('ring', 'mask'):
            count = len(rest)
            guided = role == 'ring' and 'strokeGuides' in definition
            rest_controls = rest + definition['strokeGuides']['open'] if guided else rest
            controls, weights = binding.get('indices'), binding.get('weights')
            require(isinstance(controls, list) and isinstance(weights, list) and len(controls) == len(weights) == n,
                    'invalid continuous binding lengths')
            require(all(type(i) is int and 0 <= i < len(rest_controls) for i in controls) and all(_unit(w) for w in weights),
                    'invalid continuous controls/weights')
            for i, (control, weight) in enumerate(zip(controls, weights)):
                if role == 'mask' or weight == 1:
                    require(weight == 1 and matches(pos[2*i], rest_controls[control][0]) and matches(pos[2*i+1], rest_controls[control][1]),
                            'continuous inner vertex must match contour')
            if role == 'ring':
                require(not d.get('clip'), 'continuous ring cannot be clipped')
                count, mid = len(rest), len(rest) // 2
                lower = [0, *range(count - 1, mid, -1), mid]
                expected_controls = (list(range(2 * count + 6)) + list(range(count, 2 * count + 6)) if guided
                                     else list(range(count)) + list(range(mid + 1)) + lower + [0, mid, mid, 0])
                moving = 2 * count + 6 if guided else count
                require(n == len(expected_controls) and controls == expected_controls
                        and weights == [1.] * moving + [0.] * (count + 6), 'ring must use complete canonical rectangular annulus')
                top_ids = list(range(count, count + mid + 1))
                bottom_ids = list(range(count + mid + 1, 2 * count + 2))
                tl, tr, br, bl = range(2 * count + 2, 2 * count + 6)
                wanted = []
                for i in range(mid):
                    wanted.extend(((top_ids[i], top_ids[i+1], i+1), (top_ids[i], i+1, i),
                                   (lower[i], lower[i+1], bottom_ids[i+1]),
                                   (lower[i], bottom_ids[i+1], bottom_ids[i])))
                wanted.extend(((0, tl, top_ids[0]), (0, bl, tl), (0, bottom_ids[0], bl),
                               (mid, br, bottom_ids[-1]), (mid, tr, br), (mid, top_ids[-1], tr)))
                fixed_offset = count + 6 if guided else 0
                if guided:
                    perimeter = list(range(mid + 1)) + [count + 3, count + 4] + list(range(count + 1, mid, -1)) + [count + 5, count + 2]
                    for i, index in enumerate(perimeter):
                        a, b = count + index, count + perimeter[(i + 1) % len(perimeter)]
                        wanted.extend(((a, a + fixed_offset, b + fixed_offset), (a, b + fixed_offset, b)))
                require(sorted(tuple(sorted(ix[i:i+3])) for i in range(0, len(ix), 3))
                        == sorted(tuple(sorted(t)) for t in wanted), 'incomplete or incorrect ring triangles')
                if definition['version'] == 3:
                    require(all(area(pos, *triangle) > 1e-9 for triangle in wanted),
                            'continuous canonical triangle flip/collapse or overlap')
                top_ids = [i + fixed_offset for i in top_ids]
                bottom_ids = [i + fixed_offset for i in bottom_ids]
                tl, tr, br, bl = [i + fixed_offset for i in (tl, tr, br, bl)]
                top, bottom = pos[2*top_ids[0]+1], pos[2*bottom_ids[0]+1]
                left, right = pos[2*tl], pos[2*tr]
                require(left < rest[0][0] and right > rest[mid][0] and top < bottom,
                        'invalid fixed ring bounds')
                require(all(matches(pos[2*t], rest[i][0]) and matches(pos[2*t+1], top)
                            and matches(pos[2*b], rest[i][0]) and matches(pos[2*b+1], bottom)
                            for i, (t, b) in enumerate(zip(top_ids, bottom_ids))), 'invalid fixed ring rows')
                require(all(matches(pos[2*i], x) and matches(pos[2*i+1], y)
                            for i, x, y in ((tl, left, top), (tr, right, top), (br, right, bottom), (bl, left, bottom))),
                        'invalid fixed ring corners')
                if guided:
                    require(all(left < x < right and top < y < bottom
                                for points in definition['strokeGuides'].values() for x, y in points),
                            'fixed ring must enclose every stroke guide')
            else:
                require(d.get('isMask') is True and not d.get('clip') and n == len(rest) and len(set(controls)) == n,
                        'incomplete or clipped continuous mask')
                mid = n // 2
                lower = [0, *range(n - 1, mid, -1), mid]
                wanted = []
                for i in range(mid):
                    if i:
                        wanted.append(tuple(sorted((i, lower[i+1], lower[i]))))
                    if i < mid - 1:
                        wanted.append(tuple(sorted((i, i+1, lower[i+1]))))
                actual = [tuple(sorted(controls[j] for j in ix[i:i+3])) for i in range(0, len(ix), 3)]
                require(sorted(actual) == sorted(wanted), 'continuous mask must use semantic aperture strip')
            rounding_endpoints = []
            for name, points in contours.items():
                pose_controls = points + definition['strokeGuides'][name] if guided else points
                posed = list(pos)
                for i, (control, weight) in enumerate(zip(controls, weights)):
                    if weight == 1:
                        posed[2*i:2*i+2] = (pose_controls[control] if definition['version'] == 3
                                           else f32_point(pose_controls[control]))
                    else:
                        posed[2*i+1] += weight * (pose_controls[control][1] - rest_controls[control][1])
                if definition['version'] == 3:
                    rounding_endpoints.append(posed)
                    posed = f32_values(posed, n * 2)
                    if role == 'ring':
                        require(all(area(posed, *triangle) > 1e-9 for triangle in wanted),
                                'continuous canonical triangle flip/collapse or overlap')
                for i in range(0, len(ix), 3):
                    a, b, c = ix[i:i+3]
                    before, after = area(pos, a, b, c), area(posed, a, b, c)
                    require(abs(before) >= 1e-9, 'degenerate continuous rest triangle')
                    ratio = after / before
                    require(ratio >= -1e-6 and (role != 'ring' or ratio > 1e-8), 'continuous triangle flip/collapse')
                    if definition['version'] == 3 and role == 'mask':
                        require(ratio >= 0, 'crossed Float32 continuous mask triangle')
            if definition['version'] == 3 and role == 'ring':
                validate_continuous_rounding_clearance(pos, rounding_endpoints, wanted)
        else:
            require('indices' not in binding and 'weights' not in binding, 'rigid eye art cannot have contour weights')
            clip = d.get('clip', [])
            if isinstance(clip, str):
                clip = [clip]
            require(len(clip) == 1 and by_id[clip[0]].get('eyeRig', {}).get('side') == side
                    and by_id[clip[0]].get('eyeRig', {}).get('role') == 'mask', 'eye art requires shared continuous mask')
    for side, present in roles.items():
        require(present.count('mask') == 1 and 'ring' in present and 'iris' in present and 'white' in present,
                'continuous eye requires exactly one mask and ring/iris/white')
    for drawable in model["drawables"]:
        clips = drawable.get("clip", [])
        clips = [clips] if isinstance(clips, str) else clips
        require(not any(ident in registered_ring_ids for ident in clips),
                "registered eye rings cannot be used as clip targets")
    bound = set()
    for side, definition in enabled.items():
        bound.add("EyeLOpen" if side == "L" else "EyeROpen")
        contours = definition["contours"]
        if definition.get("browJitomeDrop", 0) and any(
            df.get("type") == "brow" and df.get("side") == side
            for drawable in model["drawables"] for df in drawable.get("deformers", [])
        ):
            bound.add("EyeJitome")
        for name, param in (("jitome", "EyeJitome"), ("happy", "EyeHappy"),
                            ("surprised", "EyeSurprised"), ("worried", "EyeWorried"), ("shy", "EyeShy")):
            guides = definition.get("strokeGuides", {})
            if name in contours and (contours[name] != contours["open"] or name in art_changes[side]
                                     or name in guides and guides[name] != guides["open"]):
                bound.add(param)
    return bound


def validate_model(model, files):
    obj(model, "version name canvas atlases rig drawables chains strands expressions views playbackSettings".split(),
        "version canvas atlases rig drawables".split())
    if model["version"] != 1 or type(model["version"]) is not int:
        fail("未対応のモデルversionです")
    obj(model["canvas"], {"width", "height"}, {"width", "height"})
    for v in model["canvas"].values():
        integer(v, 1, 8192)
    if "name" in model: label(model["name"])
    atlases = array(model["atlases"], 1, 16)
    resources = set()
    for atlas in atlases:
        obj(atlas, {"file", "width", "height"}, {"file", "width", "height"})
        filename = flat_name(atlas["file"])
        if filename in resources or filename not in files or not filename.endswith((".png", ".webp")):
            fail("atlas参照が不正です")
        resources.add(filename)
        dimensions = tuple(integer(atlas[k], 1, 8192) for k in ("width", "height"))
        if image_size(files[filename]) != dimensions:
            fail("atlas画像のサイズが一致しません")
    validate_rig(model["rig"])
    ids, strand_ids, bound, active_attrs = set(), set(), set(), set()
    for strand in array(model.get("strands", []), 0, 256):
        obj(strand, "id layer droop pts root physics phase gravity bendLen".split(), {"id", "pts"})
        sid = label(strand["id"])
        if sid in strand_ids:
            fail("strand idが重複しています")
        strand_ids.add(sid)
        for pt in array(strand["pts"], 2, 256):
            vector(pt)
        numeric_object(strand.get("root", {}), "z zb twist lean breath head".split())
        obj(strand.get("root", {}), "z zb twist lean breath head".split(), "zb twist lean breath".split())
        validate_physics(strand.get("physics", {}))
        for field in ("droop", "phase", "gravity", "bendLen"):
            if field in strand: number(strand[field])
    total_vertices = 0
    for d in array(model["drawables"], 1, 512):
        obj(d, "id tag atlas order kind positions uvs indices attrs deformers vertexCount pz pc strandIds overlap rigid opacity clip keyforms baseOpacity isMask sub fs pfollow eyeRig".split(),
            "id atlas order attrs positions uvs indices vertexCount".split())
        did = label(d["id"])
        if did in ids:
            fail("drawable idが重複しています")
        ids.add(did)
        integer(d["atlas"], 0, len(atlases) - 1)
        n = integer(d["vertexCount"], 3, 50000)
        total_vertices += n
        if total_vertices > 300000:
            fail("頂点総数の上限を超えています")
        binary(d["positions"], "f32", n * 2)
        uv = binary(d["uvs"], "f32", n * 2)
        if any(not 0 <= x <= 1 for x in uv):
            fail("UVが範囲外です")
        indices = binary(d["indices"], "index")
        if not indices or len(indices) % 3 or len(indices) > 300000 or any(type(x) is not int or x >= n for x in indices):
            fail("mesh indexが不正です")
        obj(d.get("attrs", {}), "z head zb twist lean breath stw sts sw pf ht".split(), "z head zb twist lean breath".split())
        for attr, value in d.get("attrs", {}).items():
            values = binary(value, "f32", n * len(d.get("strandIds", [])) if attr in {"stw", "sts"} else n * len(d.get("sub", [])) if attr == "sw" else n)
            if any(x != 0 for x in values): active_attrs.add(attr)
        bound.update(opacity(d.get("opacity", [])))
        if "order" in d: number(d["order"])
        if "baseOpacity" in d: number(d["baseOpacity"], 0, 1)
        for sub in array(d.get("sub", []), 0, 64):
            obj(sub, {"id", "c", "z"}, {"id", "c", "z"})
            label(sub["id"]); vector(sub["c"]); number(sub["z"])
        if "sub" in d and (not d["sub"] or "sw" not in d["attrs"]): fail("sub weightsが不足しています")
        if "fs" in d and type(d["fs"]) is not bool: fail("fsが不正です")
        obj(d.get("rigid", {}), {"c", "z"})
        if "rigid" in d:
            vector(d["rigid"].get("c")); number(d["rigid"].get("z"))
        if "isMask" in d and type(d["isMask"]) is not bool: fail("isMaskが不正です")
        if "pc" in d:
            vector(d["pc"])
        for field in ("tag", "kind"):
            if field in d: label(d[field])
        for field in ("pz", "overlap"):
            if field in d: number(d[field])
        if d.get("strandIds") and not {"stw", "sts"} <= set(d["attrs"]): fail("strand属性が不足しています")
        obj(d.get("keyforms", {}), PARAMS)
        for param, value in d.get("keyforms", {}).items():
            binary(value, "f32", n * 2)
        for df in array(d.get("deformers", []), 0, 32):
            obj(df, {"type", "eye", "side", "t", "w", "chain", "strand", "id", "travel", "lift", "anchor", "minScale", "halfWidth", "amount"}, {"type"})
            if df["type"] not in {"iris", "brow", "blink", "mouthOpen", "mouthForm", "chain"}:
                fail("未対応のdeformerです")
            schemas = {"iris": {"type", "eye"}, "brow": {"type", "side"},
                       "blink": {"type", "eye", "travel", "lift"}, "mouthOpen": {"type", "anchor", "minScale"},
                       "mouthForm": {"type", "anchor", "halfWidth", "amount"}, "chain": {"type", "chain", "t", "w"}}
            obj(df, schemas[df["type"]])
            if df["type"] == "chain": obj(df, schemas["chain"], schemas["chain"])
            if df["type"] == "mouthOpen":
                vector(df.get("anchor")); number(df.get("minScale"), 0, 1)
            if df["type"] == "mouthForm":
                vector(df.get("anchor")); number(df.get("halfWidth"), .000001); number(df.get("amount"))
            for key in ("eye", "side"):
                if key in df and df[key] not in {"L", "R"}:
                    fail("deformer sideが不正です")
            for key in ("t", "w"):
                if key in df:
                    values = binary(df[key], "f32", n)
                    if df["type"] == "chain" and key == "w" and any(x != 0 for x in values): active_attrs.add("chain")
            for field in ("travel", "lift", "minScale", "halfWidth", "amount"):
                if field in df: number(df[field])
            if "anchor" in df: vector(df["anchor"])
            if df["type"] in {"iris", "blink"} and df.get("eye") not in model["rig"].get("eyes", {}): fail("deformer eye参照が不正です")
            if df["type"] == "brow" and df.get("side") not in model["rig"].get("brows", {}): fail("deformer brow参照が不正です")
            if df["type"] == "iris":
                eye = model["rig"]["eyes"][df["eye"]]
                if eye["irisRange"][0]: bound.add("EyeBallX")
                if eye["irisRange"][1]: bound.add("EyeBallY")
            if df["type"] == "blink": bound.add("EyeLOpen" if df["eye"] == "L" else "EyeROpen")
            if df["type"] == "mouthOpen": bound.add("MouthOpenY")
            if df["type"] == "mouthForm" and df["amount"]: bound.add("MouthForm")
            if df["type"] == "brow":
                br = model["rig"]["brows"][df["side"]]
                if br["range"]: bound.add("BrowLY" if df["side"] == "L" else "BrowRY")
                if br["maxAngle"]: bound.add("BrowLAngle" if df["side"] == "L" else "BrowRAngle")
        for sid in array(d.get("strandIds", []), 0, 256):
            if sid not in strand_ids:
                fail("strand参照が不正です")
    for d in model["drawables"]:
        if "pfollow" in d and d["pfollow"] not in ids: fail("pfollow参照が不正です")
        if "clip" in d:
            clips = [d["clip"]] if isinstance(d["clip"], str) else array(d["clip"], 0, 32)
            if any(c not in ids or c == d["id"] for c in clips):
                fail("clip参照が不正です")
    sub_ids = set()
    clips_by_id = {}
    for d in model["drawables"]:
        for sub in d.get("sub", []):
            if sub["id"] in ids or sub["id"] in sub_ids:
                fail("sub idが重複しています")
            sub_ids.add(sub["id"])
        clips_by_id[d["id"]] = [d["clip"]] if isinstance(d.get("clip"), str) else d.get("clip", [])
    for strand in model.get("strands", []):
        if "layer" in strand and strand["layer"] not in ids:
            fail("strand layer参照が不正です")
    mask_depths = {}
    def visit_mask(ident, visiting):
        if ident in visiting: fail("clip参照が循環しています")
        if ident in mask_depths: return mask_depths[ident]
        height = 1 + max((visit_mask(child, visiting | {ident}) for child in clips_by_id[ident]), default=0)
        if height > 32: fail("clip参照の深さが上限を超えています")
        mask_depths[ident] = height
        return height
    for ident in ids:
        visit_mask(ident, set())
    chains = array(model.get("chains", []), 0, 256)
    chain_ids = set()
    for chain in chains:
        obj(chain, ("id pts points attrs keyforms physics root droop layer gravity bendLen phase "
                    "kind pid stiffness tipStiffness damping follow inertia wind maxOffset weight bendDamping").split(), {"id"})
        if ("points" in chain) == ("pts" in chain):
            fail("chain座標はpointsまたはptsの一方だけが必要です")
        cid = label(chain["id"])
        if cid in chain_ids:
            fail("chain idが重複しています")
        chain_ids.add(cid)
        pts = array(chain["points"] if "points" in chain else chain["pts"], 2, 256)
        for p in pts:
            vector(p)
        chain_attrs = "z head zb twist lean breath".split()
        obj(chain.get("attrs", {}), [*chain_attrs, "ht"] if "points" in chain else chain_attrs,
            chain_attrs if "points" in chain else ())
        for value in chain.get("attrs", {}).values():
            binary(value, "f32", len(pts))
        obj(chain.get("keyforms", {}), PARAMS)
        for value in chain.get("keyforms", {}).values():
            binary(value, "f32", len(pts) * 2)
        validate_physics(chain.get("physics", {}))
        numeric_object(chain.get("root", {}), "z zb twist lean breath head".split())
        for field in ("droop", "phase", "gravity", "bendLen"):
            if field in chain: number(chain[field])
        if "kind" in chain and chain["kind"] not in ("hair_back", "hair_front", "headwear", "body"):
            fail("chain kindが不正です")
        if "pid" in chain and chain["pid"] is not None:
            label(chain["pid"])
        # Bound producer overrides before handing them to the canonical runtime.
        for field in ("stiffness", "tipStiffness", "damping", "inertia", "maxOffset"):
            if field in chain: number(chain[field], 0)
        for field in ("follow", "weight", "bendDamping"):
            if field in chain: number(chain[field], 0, 1)
        if "wind" in chain:
            number(chain["wind"])
    for d in model["drawables"]:
        for df in d.get("deformers", []):
            if "chain" in df and df["chain"] not in chain_ids:
                fail("chain参照が不正です")
            if "strand" in df and df["strand"] not in strand_ids:
                fail("strand参照が不正です")
    exp_ids = expressions(model.get("expressions", []))
    views = model.get("views", {})
    if not isinstance(views, dict) or len(views) > 32:
        fail("viewsが不正です")
    for name, view in views.items():
        label(name)
        obj(view, {"x", "y", "zoom"}, {"x", "y", "zoom"})
        number(view["x"]); number(view["y"]); number(view["zoom"], .01, 100)
    head, body = model["rig"]["head"], model["rig"]["body"]
    if "head" in active_attrs:
        if head.get("rollScale", 0): bound.add("AngleZ")
        if head.get("mode") == "rigid":
            if head.get("bob", 4): bound.add("AngleY")
        else:
            if head.get("yawScale", 0) or head["shift"][0]: bound.add("AngleX")
            if head.get("pitchScale", 0) or head["shift"][1]: bound.add("AngleY")
    if body.get("rigid"):
        if body.get("sway", 0): bound.add("BodyAngleX")
        if body.get("depthScale", 0) or body.get("depthDrop", 0): bound.add("BodyAngleY")
        if body.get("leanScale", 0): bound.add("BodyAngleZ")
    else:
        if "twist" in active_attrs:
            if body.get("twistScale", 0) or body.get("shift", 0): bound.add("BodyAngleX")
            if body.get("pitchScale", 0): bound.add("BodyAngleY")
        if "lean" in active_attrs and body.get("leanScale", 0): bound.add("BodyAngleZ")
    if "breath" in active_attrs and (body.get("breathLift", 0) or body.get("breathWiden", 0)):
        bound.add("Breath")
    bound.update(validate_continuous_eyes(model))
    has_physics = bool((strand_ids and "stw" in active_attrs) or "chain" in active_attrs)
    if has_physics:
        bound.update({"Wind", "WindBlow", "PhysicsGain"})
    return {"expressions": sorted(exp_ids), "views": sorted(views),
            "blink": bool(bound & {"blink", "EyeLOpen", "EyeROpen"}),
            "gaze": bool(bound & {"EyeBallX", "EyeBallY"}), "mouth": bool(bound & {"mouthOpen", "MouthOpenY"}),
            "mouthVowels": mouth_vowels(model),
            "physics": has_physics,
            "headYaw": "AngleX" in bound,
            "parameters": sorted(bound & PARAMS), "deterministicSeek": True, "externalMouth": True}


@dataclass(frozen=True)
class ValidatedPackage:
    manifest: dict
    files: dict[str, bytes]
    capabilities: dict
    digest: str
    playback_defaults: dict


def _validate_files(files):
    if "manifest.json" not in files or len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
        fail("パッケージ構造が不正です")
    for name in files:
        flat_name(name)
    manifest = json_data(files["manifest.json"])
    obj(manifest, "schema_version model_format model_format_version runtime project_id revision name provenance source_sha256 model settings preview profile capabilities files playbackSettings".split(),
        "schema_version model_format model_format_version runtime project_id revision name provenance source_sha256 model preview profile capabilities files".split())
    if any(type(manifest[k]) is not int or manifest[k] != 1 for k in ("schema_version", "model_format_version")) or manifest["model_format"] != FORMAT:
        fail("未対応のpackage/model formatです")
    runtime = obj(manifest["runtime"], {"abi", "version", "core_ref", "requires"}, {"abi", "version"})
    if type(runtime["abi"]) is not int or runtime["abi"] != 1 or runtime["version"] not in {"0.1.0", "0.2.0", "0.3.0", "0.4.0"}:
        fail("未対応のruntime ABI/versionです")
    requirements = array(runtime.get("requires", []), 0, 1)
    if requirements not in ([], ["continuous-eyes-v1"], ["continuous-eyes-v2"], ["continuous-eyes-v3"]):
        fail("未対応のruntime featureです")
    if requirements != {"0.1.0": [], "0.2.0": ["continuous-eyes-v1"], "0.3.0": ["continuous-eyes-v2"], "0.4.0": ["continuous-eyes-v3"]}[runtime["version"]]:
        fail("runtime versionとfeatureが一致しません")
    for key in ("project_id", "revision", "name"):
        label(manifest[key], 200)
    provenance = manifest["provenance"]
    validate_provenance(provenance, files)
    for value in (provenance["commit"], runtime.get("core_ref"), provenance.get("core_ref"), provenance.get("source_commit"), provenance.get("model_commit")):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)):
            fail("provenance commitが不正です")
    if not isinstance(manifest["source_sha256"], str) or not HEX.fullmatch(manifest["source_sha256"]):
        fail("source hashが不正です")
    if manifest["model"] != "model.json" or manifest["preview"] != "preview.png" or manifest.get("settings") not in {None, "settings.json"}:
        fail("model/settings/preview参照が不正です")
    profile = obj(manifest["profile"], "id seed wind windBlow idle breath blink saccade lookAt follow".split(),
                  "id seed wind idle breath blink saccade lookAt follow".split())
    if profile["id"] not in {"comparison-v1", "conversation-v1"}:
        fail("未対応のprofileです")
    integer(profile["seed"], 0, 2**32 - 1)
    # wind: そよ風、windBlow: 髪・リボン・服が風になびく（現行エンジンのみ）。比較用profileは風なしで固定
    number(profile["wind"], 0, 1); number(profile.get("windBlow", 0), 0, 1); number(profile["follow"], 0, 1)
    if profile["id"] == "comparison-v1" and (profile["wind"] != 0 or "windBlow" in profile):
        fail("比較用profileには風を指定できません")
    if profile.get("windBlow", 0) and runtime.get("core_ref") is not None:
        fail("風のなびきは現行の再生エンジンでのみ使えます")
    for key in ("idle", "breath", "blink", "saccade", "lookAt"):
        if type(profile[key]) is not bool:
            fail("profileはbooleanが必要です")
    obj(manifest["capabilities"], {"parameters", "expressions", "views", "mouth", "gaze", "blink", "physics", "headYaw", "deterministicSeek", "externalMouth", "coreRef", "angleX", "playbackSettings", "mouthVowels"})
    for field, value in manifest["capabilities"].items():
        if field == "mouthVowels":
            items = array(value, 0, 5)
            if any(type(item) is not str or item not in {"a", "i", "u", "e", "o"} for item in items) or len(set(items)) != len(items):
                fail("Invalid mouthVowels capability")
        elif field == "playbackSettings":
            from .playback_settings import GROUPS
            allowed_paths = {f"{group}.{key}" for group, fields in GROUPS.items() for key in fields}
            for item in array(value, 0, len(allowed_paths)):
                if not isinstance(item, str) or item not in allowed_paths:
                    fail("未対応の再生設定capabilityです")
        elif field in {"parameters", "expressions", "views"}:
            for item in array(value, 0, 256): label(item)
        elif field == "coreRef":
            label(value)
        elif type(value) is not bool:
            fail("capability flagが不正です")
    listed = {}
    for entry in array(manifest["files"], 2, MAX_FILES - 1):
        obj(entry, {"path", "sha256", "size"}, {"path", "sha256", "size"})
        name = flat_name(entry["path"])
        if name.casefold() in {n.casefold() for n in listed} or name == "manifest.json":
            fail("file listが重複しています")
        if not name.endswith((".json", ".png", ".webp")) or name not in files:
            fail("未対応または不足のファイルです")
        if not isinstance(entry["sha256"], str) or not HEX.fullmatch(entry["sha256"]):
            fail("file hashが不正です")
        if integer(entry["size"], 1, MAX_TOTAL) != len(files[name]) or hashlib.sha256(files[name]).hexdigest() != entry["sha256"]:
            fail("file size/hashが一致しません")
        listed[name] = entry
    if set(listed) != set(files) - {"manifest.json"} or not {"model.json", "preview.png"} <= set(files):
        fail("file listとpackage内容が一致しません")
    image_size(files["preview.png"])
    model = json_data(files["model.json"])
    capabilities = validate_model(model, files)
    effective = model
    used = {"model.json", "preview.png", *[a["file"] for a in model["atlases"]]}
    settings = None
    if manifest.get("settings"):
        if "settings.json" not in files:
            fail("settingsがありません")
        settings = json_data(files["settings.json"])
        obj(settings, {"rig", "chains", "expressions", "playbackSettings"})
        if "rig" in settings:
            validate_rig(settings["rig"], partial=True)
        if "expressions" in settings:
            capabilities["expressions"] = sorted(expressions(settings["expressions"]))
        if "chains" in settings:
            if not isinstance(settings["chains"], dict):
                fail("settings chainsが不正です")
            by_id = {c["id"]: c for c in model.get("chains", [])}
            for key, patch in settings["chains"].items():
                if key not in by_id:
                    fail("settings chain参照が不正です")
                obj(patch, {"physics", "droop", "gravity", "bendLen", "stiffness", "tipStiffness",
                            "damping", "follow", "inertia", "wind", "maxOffset", "bendDamping"})
                if "physics" in patch:
                    validate_physics(patch["physics"])
        used.add("settings.json")
    for name in set(listed) - used:
        if not name.startswith("motion_") or not name.endswith(".json"):
            fail("未参照または未対応のresourceです")
        motion = json_data(files[name])
        validate_motion(motion)
        if not requirements and "EyeJitome" in motion["tracks"]:
            fail("EyeJitome motion requires continuous eye geometry")
    if settings:
        import copy
        effective = copy.deepcopy(model)
        def merge(dst, src):
            for key, value in src.items():
                if isinstance(value, dict) and isinstance(dst.get(key), dict): merge(dst[key], value)
                else: dst[key] = copy.deepcopy(value)
        if "rig" in settings: merge(effective["rig"], settings["rig"])
        if "expressions" in settings: effective["expressions"] = settings["expressions"]
        for c in effective.get("chains", []):
            if c["id"] in settings.get("chains", {}): merge(c, settings["chains"][c["id"]])
        capabilities = validate_model(effective, files)
    baseline_ref = "46c0037779fb5e59715030925d66abdc6c09ca12"
    core = runtime.get("core_ref") or provenance.get("core_ref")
    if runtime.get("core_ref") and provenance.get("core_ref") and runtime["core_ref"] != provenance["core_ref"]:
        fail("runtime core_refが競合しています")
    if core is not None and core != baseline_ref:
        fail("未対応のruntime core_refです")
    capabilities["coreRef"] = core or (baseline_ref if provenance["commit"] == baseline_ref else "canonical")
    definitions = [eye["continuous"] for eye in effective["rig"].get("eyes", {}).values() if "continuous" in eye]
    continuous = bool(definitions)
    highest = max((definition["version"] for definition in definitions), default=0)
    expected_features = [f"continuous-eyes-v{highest}"] if highest else []
    if requirements != expected_features:
        fail("目の連続変形には対応runtime featureが必要です")
    if continuous and capabilities["coreRef"] != "canonical":
        fail("目の連続変形は現行の再生エンジンでのみ使えます")
    if capabilities["coreRef"] != "canonical":
        capabilities["mouthVowels"] = []
        capabilities["parameters"] = [p for p in capabilities["parameters"] if p != "MouthVowel"]
    if any(v not in capabilities["mouthVowels"] for v in manifest["capabilities"].get("mouthVowels", [])):
        fail("Declared mouth vowel is unsupported by model/core")
    if not capabilities["mouthVowels"]:
        capabilities["parameters"] = [p for p in capabilities["parameters"] if p != "MouthVowel"]
    from .playback_settings import GROUPS, PlaybackSettingsError, package_defaults
    try:
        playback_defaults = package_defaults(manifest, model, settings)
    except PlaybackSettingsError as exc:
        raise PackageError(str(exc)) from exc
    # Never trust declared controls; derive support from the validated bindings.
    capabilities["playbackSettings"] = [f"{group}.{key}" for group, fields in GROUPS.items() for key, field in fields.items()
        if set(field["parameters"]) & set(capabilities["parameters"])] if capabilities["coreRef"] == "canonical" and profile["id"] == "conversation-v1" else []
    digest = hashlib.sha256(files["manifest.json"]).hexdigest()
    return ValidatedPackage(manifest, files, capabilities, digest, playback_defaults)


def validate_package(data):
    if len(data) > MAX_UPLOAD:
        fail("ZIPは64MiB以下にしてください")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not 3 <= len(infos) <= MAX_FILES:
                fail("ZIPのファイル数が不正です")
            files, names, total = {}, set(), 0
            for info in infos:
                name = flat_name(info.filename)
                if info.orig_filename != info.filename: fail("ZIPのNUL path aliasは禁止されています")
                mode = info.external_attr >> 16
                if (name.casefold() in names or info.is_dir() or info.flag_bits & 1
                        or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or info.external_attr & 0x400 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    fail("ZIPのalias/link/encryption/typeが不正です")
                names.add(name.casefold())
                total += info.file_size
                if info.file_size < 1 or info.file_size > MAX_TOTAL or total > MAX_TOTAL or info.file_size > max(info.compress_size, 1) * 200:
                    fail("ZIP展開量の上限を超えています")
                if name.endswith(".json") and info.file_size > MAX_JSON:
                    fail("JSONが大きすぎます")
                with archive.open(info) as stream:
                    payload = stream.read(min(info.file_size, MAX_TOTAL) + 1)
                if len(payload) != info.file_size:
                    fail("ZIPバイト長が不正です")
                files[name] = payload
            return validate_files(files)
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as exc:
        raise PackageError("ZIPを読み込めません") from exc


def assert_regular(path):
    from ..services.storage_roots import assert_no_links
    from ..services.storage_io import StorageError
    try:
        assert_no_links(path, allow_missing=False)
    except StorageError as exc:
        raise PackageError("保存assetのlink/reparseは禁止されています") from exc
    if not path.is_file():
        fail("保存assetが通常ファイルではありません")


def read_validated_directory(path):
    from ..services.storage_roots import assert_no_links
    assert_no_links(path, allow_missing=False)
    names = list(path.iterdir())
    if len(names) > MAX_FILES:
        fail("保存asset数が不正です")
    files, total = {}, 0
    for child in names:
        assert_regular(child)
        flat_name(child.name)
        size = child.stat().st_size
        total += size
        if size > (MAX_JSON if child.suffix == ".json" else MAX_TOTAL) or total > MAX_TOTAL:
            fail("保存assetが大きすぎます")
        files[child.name] = child.read_bytes()
    return validate_files(files)


def immutable_copy(package, destination):
    """Caller holds the rig transaction lock. Never overwrite an existing copy."""
    from ..services.storage_roots import assert_no_links
    assert_no_links(destination, allow_missing=True)
    if destination.exists():
        if read_validated_directory(destination).digest != package.digest:
            fail("immutable assetの内容が一致しません")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".import-" + uuid.uuid4().hex)
    try:
        temporary.mkdir()
        for name, payload in package.files.items():
            with (temporary / name).open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        read_validated_directory(temporary)
        assert_no_links(destination, allow_missing=True)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            assert_no_links(temporary, allow_missing=False)
            shutil.rmtree(temporary)

def validate_provenance(value, files):
    allowed = {"commit", "core_ref", "source_commit", "files", "cache_reuse", "producer", "model_commit",
               "commit_verified", "model_sha256", "preview", "artifact_files", "production", "new_inference", "approval",
               "build_key", "decomposition_key", "render_receipt_sha256", "quality"}
    obj(value, allowed, {"commit"})
    for key in ("build_key", "decomposition_key", "render_receipt_sha256"):
        if key in value and (not isinstance(value[key], str) or not HEX.fullmatch(value[key])):
            fail("provenance production hashが不正です")
    if "quality" in value:
        # Producer QA metadata never grants the host's separate activation approval.
        quality = obj(value["quality"], {"deterministic", "vision"}, {"deterministic", "vision"})
        if quality["deterministic"] != "passed" or quality["vision"] not in ("passed", "unreviewed"):
            fail("provenance qualityが不正です")
    for key in ("commit_verified", "cache_reuse", "new_inference"):
        if key in value and type(value[key]) is not bool:
            fail("provenance booleanが不正です")
    for key in ("producer", "production", "approval"):
        if key in value:
            label(value[key])
    if "model_sha256" in value and value["model_sha256"] != hashlib.sha256(files["model.json"]).hexdigest():
        fail("provenance model hashが一致しません")
    if "artifact_files" in value:
        inventory = value["artifact_files"]
        if not isinstance(inventory, dict):
            fail("provenance inventoryが不正です")
        for name, digest in inventory.items():
            flat_name(name)
            if name not in files or digest != hashlib.sha256(files[name]).hexdigest():
                fail("provenance artifact hashが一致しません")
    if "preview" in value:
        preview = obj(value["preview"], {"kind", "input_sha256", "artistic_quality_approved"})
        if preview.get("kind") not in {"explicit_model_preview", "build_rest_composite"}:
            fail("preview provenanceが不正です")
        if not HEX.fullmatch(str(preview.get("input_sha256", ""))):
            fail("preview input hashが不正です")
        if type(preview.get("artistic_quality_approved", False)) is not bool:
            fail("preview review flagが不正です")
    if "files" in value:
        if not isinstance(value["files"], dict):
            fail("provenance filesが不正です")
        for name, digest in value["files"].items():
            flat_name(name)
            if not isinstance(digest, str) or not HEX.fullmatch(digest):
                fail("provenance file hashが不正です")


def validate_motion(value):
    obj(value, {"version", "duration", "fps", "tracks"}, {"duration", "tracks"})
    number(value["duration"], .001, 3600)
    if "version" in value:
        integer(value["version"], 1, 1)
    if "fps" in value:
        number(value["fps"], 1, 240)
    tracks = obj(value["tracks"], PARAMS)
    for key, points in tracks.items():
        previous = -1
        for pt in array(points, 1, 10000):
            vector(pt)
            if not 0 <= pt[0] <= value["duration"] or pt[0] <= previous:
                fail("motion key時刻が不正です")
            if key == "MouthVowel":
                integer(pt[1], 0, 5)
            previous = pt[0]


def validate_files(files):
    try:
        return _validate_files(files)
    except PackageError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError, struct.error) as exc:
        raise PackageError("パッケージデータの型・構造が不正です") from exc
def validate_physics(value):
    allowed = "freqMid freqTip zeta length maxAngle wind windPush windFlutter kick flutterFreq".split()
    obj(value, [*allowed, "static"])
    if "static" in value and type(value["static"]) is not bool:
        fail("physics.static はbooleanで指定してください")
    numeric_object({key: item for key, item in value.items() if key != "static"}, allowed)
    for key in ("freqMid", "freqTip", "length"):
        if key in value:
            number(value[key], .000001, 100000)
    for key in ("zeta", "maxAngle"):
        if key in value:
            number(value[key], 0, 100)


def strict_image_container(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, ended = 8, False
        while offset < len(data):
            if offset + 12 > len(data):
                fail("PNG chunkが不足しています")
            n = int.from_bytes(data[offset:offset + 4], "big")
            tag = data[offset + 4:offset + 8]
            if offset + n + 12 > len(data):
                fail("PNG chunk長が不正です")
            chunk = data[offset + 8:offset + n + 8]
            crc = int.from_bytes(data[offset + n + 8:offset + n + 12], "big")
            if binascii.crc32(tag + chunk) & 0xffffffff != crc:
                fail("PNG CRCが一致しません")
            offset += n + 12
            if tag == b"IEND":
                if n != 0 or offset != len(data):
                    fail("PNG末尾の余分なデータは禁止されています")
                ended = True
        if not ended:
            fail("PNG終端がありません")
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            fail("WebP末尾の余分なデータは禁止されています")
    else:
        fail("画像containerが未対応です")
