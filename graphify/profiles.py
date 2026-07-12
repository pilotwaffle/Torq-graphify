# Project graph profiles - named, project-scoped graph configurations.
#
# A repository may commit a ``graphify.toml`` at its root describing one or
# more named graph "profiles" (e.g. a first-party ``product`` graph and an
# opt-in ``vendor`` graph), each with its own output directory, include /
# exclude patterns, and fitness thresholds. This makes the dual-graph pattern
# (keep dependency noise out of the default graph, park it in a second graph)
# a first-class, reusable capability instead of per-repo scripting.
#
# Back-compat contract: a repository with NO graphify.toml sees zero behavior
# change - every loader below degrades to an empty config, and the resolver in
# paths.resolve_out_dir falls back to the legacy GRAPHIFY_OUT env var and the
# literal "graphify-out". A parse failure in an existing graphify.toml warns
# and degrades the same way; it must never crash an unrelated build (a user
# asking for a specific --profile by name DOES get a hard error, because
# silently building into the wrong directory is worse than stopping).
#
# TOML loading matches the repo-wide pattern (cargo_introspect.py,
# manifest_ingest.py): stdlib tomllib on Python 3.11+, the tiny ``tomli``
# package as a fallback on 3.10, and a graceful message otherwise.
from __future__ import annotations

import fnmatch
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

CONFIG_NAME = "graphify.toml"

# Generic ownership defaults. Deliberately domain-neutral: these describe
# universal repo furniture (vendored deps, package managers, build output,
# tests), never a specific consumer's layout. Consumers refine them in their
# own committed graphify.toml.
DEFAULT_OWNERSHIP: dict[str, list[str]] = {
    "vendor": [
        "**/vendor/**", "**/vendors/**", "**/third_party/**",
        "**/third-party/**", "**/node_modules/**", "**/bower_components/**",
        "**/.venv/**", "**/site-packages/**",
    ],
    "generated": [
        "**/dist/**", "**/build/**", "**/out/**", "**/__pycache__/**",
        "**/*.min.js", "**/*.min.css", "**/generated/**", "**/*_pb2.py",
        "**/*.g.dart", "**/coverage/**",
    ],
    "test": [
        "**/tests/**", "**/test/**", "**/__tests__/**", "**/__test__/**",
        "**/*.test.*", "**/*.spec.*", "**/test_*.py", "**/*_test.go",
        "**/*_test.py",
    ],
}

# Fitness thresholds when a profile does not override them. A profile that
# declares ``kind = "vendor"`` opts out of vendor-dominance failure entirely
# (a vendor graph is SUPPOSED to be all vendor).
DEFAULT_FITNESS: dict[str, float] = {
    "max_vendor_share_low": 0.60,   # above this -> LOW confidence
    "max_vendor_share_fail": 0.85,  # above this -> FAIL (unless kind=vendor)
    "min_first_party_share": 0.0,   # disabled unless the profile sets it
    "min_labeled_share": 0.0,       # disabled unless the profile sets it
    "min_cross_package_edges": 0.0, # disabled unless the profile sets it
}

_KNOWN_PROFILE_KEYS = {
    "out", "include", "exclude", "directed", "kind", "fitness", "description",
}
_KNOWN_TOP_KEYS = {"default_profile", "profiles", "ownership", "package_roots"}
_KNOWN_OWNERSHIP_KEYS = set(DEFAULT_OWNERSHIP)
_KNOWN_FITNESS_KEYS = set(DEFAULT_FITNESS)


@dataclass
class Profile:
    name: str
    out: str
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    directed: bool = True
    kind: str = ""            # "" | "vendor" (vendor graphs skip dominance FAIL)
    fitness: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FITNESS))
    description: str = ""


@dataclass
class ProjectConfig:
    root: Path
    default_profile: str = ""
    profiles: dict[str, Profile] = field(default_factory=dict)
    ownership: dict[str, list[str]] = field(default_factory=lambda: {
        k: list(v) for k, v in DEFAULT_OWNERSHIP.items()
    })
    package_roots: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_path: Path | None = None   # the graphify.toml actually read, if any

    def profile(self, name: str) -> Profile | None:
        return self.profiles.get(name)


def _load_toml(path: Path) -> tuple[dict | None, str]:
    """Parse a TOML file. Returns (data, "") or (None, reason)."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None, (
                f"cannot read {path.name}: TOML support needs Python 3.11+ "
                "or `pip install tomli`"
            )
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh), ""
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc}"
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        return None, f"cannot parse {path.name}: {exc}"


def _norm(value: Any) -> str:
    """Normalize a path-ish config value to forward slashes (Windows-safe)."""
    return str(value).replace("\\", "/").strip()


def _out_is_unsafe(out: str) -> bool:
    """A normalized output path escapes the repository boundary."""
    return (
        not out
        or out.startswith("/")
        or re.match(r"^[A-Za-z]:", out) is not None
        or ".." in PurePosixPath(out).parts
    )


def _safe_slug(name: str) -> str:
    """Reduce an arbitrary profile name to a single safe path segment."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "profile"


def _safe_out(name: str, raw: Any, warnings: list[str]) -> str:
    """Validate a profile's output dir: must stay a relative path inside the
    repository. graphify.toml is REPO-AUTHOR-controlled (a different trust
    boundary from the user-set GRAPHIFY_OUT env var, which legitimately allows
    absolute shared paths), so absolute paths, drive letters, and ``..``
    traversal are rejected with a warning and the safe default is used.

    The synthesized fallback goes through the SAME validation: TOML quoted
    table names may contain slashes and dots ([profiles."a/../../outside"]),
    so 'graphify-' + name is not inherently safe - an unsafe synthesis is
    reduced to a sanitized single-segment slug instead.
    """
    if not raw:
        candidate = _norm(f"graphify-{name}")
        if _out_is_unsafe(candidate):
            slug = _safe_slug(name)
            warnings.append(
                f"{CONFIG_NAME}: profile name {name!r} is not a safe path "
                f"segment; using 'graphify-{slug}'"
            )
            return f"graphify-{slug}"
        return candidate
    out = _norm(raw)
    if _out_is_unsafe(out):
        warnings.append(
            f"{CONFIG_NAME}: profiles.{name}.out {raw!r} must be a relative "
            f"path inside the repository; using 'graphify-{_safe_slug(name)}'"
        )
        return f"graphify-{_safe_slug(name)}"
    return out


def _str_list(value: Any, *, ctx: str, warnings: list[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_norm(value)]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [_norm(v) for v in value]
    warnings.append(f"{ctx}: expected a string or list of strings, ignored")
    return []


def load_config(root: "Path | str | None" = None) -> ProjectConfig:
    """Load ``graphify.toml`` from ``root`` (default: cwd).

    Absent file -> empty config (pure no-op, legacy behavior preserved).
    Unreadable/unparseable file -> empty config carrying a warning; callers
    that received an EXPLICIT profile request should escalate that warning to
    an error (see resolve_profile).
    """
    base = Path(root) if root is not None else Path(os.getcwd())
    cfg = ProjectConfig(root=base)
    path = base / CONFIG_NAME
    if not path.is_file():
        return cfg
    data, err = _load_toml(path)
    if data is None:
        cfg.warnings.append(err)
        return cfg
    cfg.source_path = path

    for key in data:
        if key not in _KNOWN_TOP_KEYS:
            cfg.warnings.append(f"{CONFIG_NAME}: unknown key '{key}' ignored")

    dp = data.get("default_profile", "")
    cfg.default_profile = dp if isinstance(dp, str) else ""

    ownership = data.get("ownership")
    if isinstance(ownership, dict):
        for key, val in ownership.items():
            if key not in _KNOWN_OWNERSHIP_KEYS:
                cfg.warnings.append(
                    f"{CONFIG_NAME}: unknown ownership key '{key}' ignored"
                )
                continue
            # EXTENSION semantics (the documented contract): project patterns
            # append to the built-in generic defaults, deduplicated with
            # stable ordering (defaults first, then project order). A project
            # list never replaces or erases the defaults - dropping
            # **/node_modules/** because a repo added extern/** would silently
            # reclassify dependency noise as first-party.
            extra = _str_list(val, ctx=f"ownership.{key}", warnings=cfg.warnings)
            merged = list(DEFAULT_OWNERSHIP[key])
            for pat in extra:
                if pat not in merged:
                    merged.append(pat)
            cfg.ownership[key] = merged

    cfg.package_roots = _str_list(
        data.get("package_roots"), ctx="package_roots", warnings=cfg.warnings
    )

    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        for name, body in profiles.items():
            if not isinstance(body, dict):
                cfg.warnings.append(
                    f"{CONFIG_NAME}: profiles.{name} is not a table, ignored"
                )
                continue
            for key in body:
                if key not in _KNOWN_PROFILE_KEYS:
                    cfg.warnings.append(
                        f"{CONFIG_NAME}: unknown key profiles.{name}.{key} ignored"
                    )
            out = _safe_out(name, body.get("out"), cfg.warnings)
            fitness = dict(DEFAULT_FITNESS)
            fit_body = body.get("fitness")
            if isinstance(fit_body, dict):
                for key, val in fit_body.items():
                    if key not in _KNOWN_FITNESS_KEYS:
                        cfg.warnings.append(
                            f"{CONFIG_NAME}: unknown key "
                            f"profiles.{name}.fitness.{key} ignored"
                        )
                        continue
                    try:
                        fitness[key] = float(val)
                    except (TypeError, ValueError):
                        cfg.warnings.append(
                            f"{CONFIG_NAME}: profiles.{name}.fitness.{key} "
                            "is not a number, ignored"
                        )
            cfg.profiles[name] = Profile(
                name=name,
                out=out,
                include=_str_list(
                    body.get("include"),
                    ctx=f"profiles.{name}.include", warnings=cfg.warnings,
                ),
                exclude=_str_list(
                    body.get("exclude"),
                    ctx=f"profiles.{name}.exclude", warnings=cfg.warnings,
                ),
                directed=bool(body.get("directed", True)),
                kind=str(body.get("kind", "") or ""),
                fitness=fitness,
                description=str(body.get("description", "") or ""),
            )

    if cfg.default_profile and cfg.default_profile not in cfg.profiles:
        cfg.warnings.append(
            f"{CONFIG_NAME}: default_profile '{cfg.default_profile}' "
            "names no [profiles.*] table, ignored"
        )
        cfg.default_profile = ""
    return cfg


class ProfileError(Exception):
    """An explicitly requested profile cannot be resolved."""


def resolve_profile(
    name: str, root: "Path | str | None" = None,
    config: ProjectConfig | None = None,
) -> Profile:
    """Resolve an EXPLICITLY requested profile name -> Profile, or raise.

    Explicit requests fail loudly: building into the wrong directory because a
    typo'd profile silently fell back to defaults is the worst outcome.
    """
    cfg = config if config is not None else load_config(root)
    prof = cfg.profile(name)
    if prof is not None:
        return prof
    if cfg.source_path is None:
        detail = cfg.warnings[0] if cfg.warnings else f"no {CONFIG_NAME} found"
        raise ProfileError(f"profile '{name}' requested but {detail}")
    known = ", ".join(sorted(cfg.profiles)) or "(none defined)"
    raise ProfileError(
        f"profile '{name}' not found in {cfg.source_path}; known profiles: {known}"
    )


def classify_ownership(
    source_file: str, ownership: dict[str, list[str]] | None = None,
) -> str:
    """Bucket a repo-relative source path: vendor | generated | test | first_party.

    Matching is done on a forward-slash-normalized path (Windows-safe) with
    fnmatch semantics per pattern; ``**/`` prefixes are also tried against the
    bare path so ``**/vendor/**`` matches ``vendor/lib/a.js`` at the root.
    Order matters: vendor beats generated beats test (a vendored test file is
    vendor noise, not signal).
    """
    own = ownership or DEFAULT_OWNERSHIP
    path = _norm(source_file).lstrip("/")
    for bucket in ("vendor", "generated", "test"):
        for pat in own.get(bucket, ()):
            p = _norm(pat)
            if fnmatch.fnmatch(path, p):
                return bucket
            # "**/x/**" should also match "x/..." at the repo root, where
            # there is no leading segment for the "**/" to consume.
            if p.startswith("**/") and fnmatch.fnmatch(path, p[3:]):
                return bucket
    return "first_party"


_VCS_MARKERS = (".git", ".hg", ".svn")


def config_root_for(start: "Path | str") -> Path:
    """Nearest directory at or above ``start`` holding a graphify.toml.

    Profile filtering must follow the PROJECT BEING SCANNED, not the process
    CWD - `graphify extract /work/repo` from a CI parent workspace has to
    honor /work/repo's config. The walk mirrors the ignore-file convention:
    check ``start`` and each ancestor, stopping AFTER the first directory
    that carries a VCS marker (the repo root - configs above it belong to
    someone else). Falls back to ``start`` when nothing is found.
    """
    base = Path(start).resolve()
    if base.is_file():
        base = base.parent
    current = base
    while True:
        if (current / CONFIG_NAME).is_file():
            return current
        if any((current / marker).exists() for marker in _VCS_MARKERS):
            return base  # repo root without a config: stop, no config applies
        if current.parent == current:
            return base
        current = current.parent


def effective_profile_excludes(root: "Path | str | None" = None) -> list[str]:
    """Exclude patterns of the ACTIVE profile, for scan-time application.

    The single source of truth for profile-driven corpus exclusion - the
    extract command and every update/watch/hook rebuild path apply THIS set
    through detect's anchored ``extra_excludes`` channel, so there is exactly
    one exclusion mechanism.

    ``root`` should be the SCAN TARGET: the governing graphify.toml is
    discovered from the target upward (bounded by the VCS root), never from
    the process CWD, so cross-directory scans honor the scanned project's
    config. Callers omitting ``root`` keep CWD semantics.

    Active profile: ``GRAPHIFY_PROFILE`` env when it names a known profile,
    else ``default_profile``. Returns [] (legacy-identical, fail-open) when
    ``GRAPHIFY_OUT`` overrides output resolution (a profile that lost output
    resolution must not silently filter the scan), when no graphify.toml
    exists, or on any load problem.
    """
    try:
        if os.environ.get("GRAPHIFY_OUT", "").strip():
            return []
        cfg = load_config(config_root_for(root) if root is not None else None)
        if cfg.source_path is None:
            return []
        name = os.environ.get("GRAPHIFY_PROFILE", "").strip() or cfg.default_profile
        prof = cfg.profile(name) if name else None
        return list(prof.exclude) if prof is not None else []
    except Exception:
        return []


def emit_warnings(cfg: ProjectConfig, *, stream=None) -> None:
    out = stream if stream is not None else sys.stderr
    for w in cfg.warnings:
        print(f"[graphify] warning: {w}", file=out)
