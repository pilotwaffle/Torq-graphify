# Graph fitness - generic health metrics for a built knowledge graph.
#
# `graphify fitness` answers one question before an agent (or a human) trusts
# graph answers: is this graph actually representative of the code the asker
# cares about, or is it dominated by vendored/generated noise? The monorepo
# pilot that motivated this found a default graph where >97% of nodes came
# from one vendored dependency tree - structurally valid, semantically useless
# for product-architecture questions - and the failure mode was silent.
#
# Metrics (all computed post-hoc from graph.json + sidecars; no graph-format
# change): total nodes, ownership shares (vendor / generated / test /
# first-party over CLASSIFIED nodes, with nodes lacking a source_file counted
# separately as `unclassified` so they can never inflate first-party share),
# community-label coverage, cross-package edges, graph file size, directed
# status (true/false/unknown - a missing flag is reported, not defaulted), and
# interpreter availability (.graphify_python sidecar).
#
# Verdict: PASS / LOW / FAIL.
#   FAIL - graph missing/unparseable, or vendor share above the fail
#          threshold on a profile that is not declared `kind = "vendor"`.
#   LOW  - vendor share above the low threshold, or any enabled min_*
#          threshold unmet. LOW exits 0 (warnings) unless --strict.
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graphify.diagnostics import _canonical_edge, _edge_list
from graphify.profiles import (
    DEFAULT_FITNESS,
    ProjectConfig,
    classify_ownership,
    load_config,
)

_PLACEHOLDER_LABEL = re.compile(r"^Community\s+\d+$")

VERDICT_PASS = "PASS"
VERDICT_LOW = "LOW"
VERDICT_FAIL = "FAIL"


@dataclass
class FitnessReport:
    graph_path: str
    verdict: str = VERDICT_PASS
    failures: list[str] = field(default_factory=list)
    # warnings affect the verdict (LOW); notes are informational only.
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph": self.graph_path,
            "verdict": self.verdict,
            "failures": self.failures,
            "warnings": self.warnings,
            "notes": self.notes,
            "metrics": self.metrics,
        }


def _norm_path(value: str) -> str:
    return str(value).replace("\\", "/").lstrip("/")


def _segment(source_file: str, package_roots: list[str]) -> str:
    """Package segment for the cross-package metric.

    With ``package_roots`` configured, the longest matching root prefix wins
    (so "packages/gateway" and "packages/bridge" are distinct packages even
    though they share a top-level dir). Without it, the repo-wide convention
    applies: the top-level path segment (same rule analyze.py uses for its
    cross-repo bonus).
    """
    path = _norm_path(source_file)
    best = ""
    for root in package_roots:
        r = _norm_path(root).rstrip("/")
        if r and (path == r or path.startswith(r + "/")) and len(r) > len(best):
            best = r
    if best:
        # One segment BELOW the matched root identifies the package.
        rest = path[len(best):].lstrip("/")
        first = rest.split("/", 1)[0] if rest else ""
        return f"{best}/{first}" if first else best
    return path.split("/", 1)[0]


def _interpreter_status(out_dir: Path) -> str:
    """'.graphify_python' sidecar health: ok | stale | missing."""
    sidecar = out_dir / ".graphify_python"
    if not sidecar.is_file():
        return "missing"
    try:
        target = sidecar.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "stale"
    if target and Path(target).exists():
        return "ok"
    return "stale"


def compute_fitness(
    graph_path: "str | Path",
    *,
    config: ProjectConfig | None = None,
    profile_name: str = "",
    root: "Path | None" = None,
) -> FitnessReport:
    """Compute the fitness report for one graph.json.

    Never raises for a bad graph - a missing or unparseable graph IS the FAIL
    verdict, because "unable to assess" must not read as healthy.
    """
    gp = Path(graph_path)
    report = FitnessReport(graph_path=str(gp))
    cfg = config if config is not None else load_config(root)
    profile = cfg.profile(profile_name) if profile_name else None
    thresholds = dict(profile.fitness) if profile else dict(DEFAULT_FITNESS)
    is_vendor_kind = bool(profile and profile.kind == "vendor")

    if not gp.is_file():
        report.verdict = VERDICT_FAIL
        report.failures.append(f"missing graph: {gp}")
        return report

    size_bytes = gp.stat().st_size
    try:
        from graphify.security import check_graph_file_size_cap
        check_graph_file_size_cap(gp)
    except ValueError as exc:
        report.verdict = VERDICT_FAIL
        report.failures.append(str(exc))
        return report
    except ImportError:
        pass

    try:
        data = json.loads(gp.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        report.verdict = VERDICT_FAIL
        report.failures.append(f"unreadable graph: {exc}")
        return report
    if not isinstance(data, dict):
        report.verdict = VERDICT_FAIL
        report.failures.append("graph.json is not a JSON object")
        return report

    nodes = data.get("nodes")
    nodes = nodes if isinstance(nodes, list) else []
    total = len(nodes)

    # Ownership buckets over nodes that carry a usable source_file; nodes
    # without one (synthetic/phantom/external) are counted but never share-d.
    buckets = {"vendor": 0, "generated": 0, "test": 0, "first_party": 0}
    unclassified = 0
    src_by_id: dict[str, str] = {}
    communities: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            unclassified += 1
            continue
        community = node.get("community")
        if community is not None:
            communities.add(str(community))
        src = node.get("source_file")
        if not src or not isinstance(src, str):
            unclassified += 1
            continue
        nid = node.get("id")
        if nid is not None:
            src_by_id[str(nid)] = src
        buckets[classify_ownership(src, cfg.ownership)] += 1

    classified = sum(buckets.values())
    def share(n: int) -> float:
        return round(n / classified, 4) if classified else 0.0

    vendor_share = share(buckets["vendor"])
    generated_share = share(buckets["generated"])
    first_party_share = share(buckets["first_party"])

    # Community-label coverage: labels sidecar next to the graph; a community
    # counts as labeled only when its label exists and is not the "Community N"
    # placeholder. No sidecar -> zero coverage (reported, not fatal).
    labels_path = gp.parent / ".graphify_labels.json"
    labeled = 0
    if labels_path.is_file():
        try:
            labels = json.loads(
                labels_path.read_text(encoding="utf-8", errors="replace")
            )
            if isinstance(labels, dict):
                for cid, label in labels.items():
                    if (
                        str(cid) in communities
                        and isinstance(label, str)
                        and label.strip()
                        and not _PLACEHOLDER_LABEL.match(label.strip())
                    ):
                        labeled += 1
        except (OSError, json.JSONDecodeError):
            report.warnings.append(f"unreadable labels sidecar: {labels_path.name}")
    labeled_share = round(labeled / len(communities), 4) if communities else 0.0

    # Cross-package edges: both endpoints resolvable to a source_file and the
    # package segments differ. Edge shape variance (links/edges, source/from,
    # target/to) is normalized by the shared diagnostics helpers (#738).
    cross = 0
    edges = _edge_list(data)
    for raw in edges:
        edge = _canonical_edge(raw)
        s = src_by_id.get(edge["source"])
        t = src_by_id.get(edge["target"])
        if not s or not t:
            continue
        if _segment(s, cfg.package_roots) != _segment(t, cfg.package_roots):
            cross += 1

    directed = data.get("directed")
    directed_status = directed if isinstance(directed, bool) else "unknown"

    report.metrics = {
        "total_nodes": total,
        "total_edges": len(edges),
        "vendor_nodes": buckets["vendor"],
        "generated_nodes": buckets["generated"],
        "test_nodes": buckets["test"],
        "first_party_nodes": buckets["first_party"],
        "unclassified_nodes": unclassified,
        "vendor_share": vendor_share,
        "generated_share": generated_share,
        "first_party_share": first_party_share,
        "communities": len(communities),
        "labeled_communities": labeled,
        "labeled_share": labeled_share,
        "cross_package_edges": cross,
        "graph_size_bytes": size_bytes,
        "directed": directed_status,
        "interpreter": _interpreter_status(gp.parent),
        "profile": profile.name if profile else "",
        "profile_kind": profile.kind if profile else "",
    }

    if labeled_share == 0.0 and communities:
        report.notes.append(
            "community labels are placeholders (Community N); do not use them "
            "as navigation categories"
        )
    if unclassified:
        report.notes.append(
            f"{unclassified} node(s) have no source_file and are excluded "
            "from ownership shares"
        )
    if directed_status == "unknown":
        report.warnings.append(
            "graph has no directed flag (older build); reverse traversals "
            "may be unreliable"
        )
    if report.metrics["interpreter"] != "ok":
        report.notes.append(
            f"interpreter sidecar is {report.metrics['interpreter']}; "
            "subcommands may need the interpreter re-resolved"
        )

    # Verdict.
    if is_vendor_kind:
        pass  # a vendor graph is supposed to be vendor-dominated
    elif vendor_share > thresholds["max_vendor_share_fail"]:
        report.failures.append(
            f"vendor share {vendor_share:.1%} exceeds fail threshold "
            f"{thresholds['max_vendor_share_fail']:.0%} - product questions "
            "will surface vendor noise; split a vendor profile out "
            "(see docs/profiles.md)"
        )
    elif vendor_share > thresholds["max_vendor_share_low"]:
        report.warnings.append(
            f"vendor share {vendor_share:.1%} exceeds {thresholds['max_vendor_share_low']:.0%} "
            "- consider a dedicated vendor profile"
        )
    if thresholds["min_first_party_share"] > 0 and first_party_share < thresholds["min_first_party_share"]:
        report.warnings.append(
            f"first-party share {first_party_share:.1%} below configured "
            f"minimum {thresholds['min_first_party_share']:.0%}"
        )
    if thresholds["min_labeled_share"] > 0 and labeled_share < thresholds["min_labeled_share"]:
        report.warnings.append(
            f"labeled-community share {labeled_share:.1%} below configured "
            f"minimum {thresholds['min_labeled_share']:.0%}"
        )
    if thresholds["min_cross_package_edges"] > 0 and cross < thresholds["min_cross_package_edges"]:
        report.warnings.append(
            f"cross-package edges {cross} below configured minimum "
            f"{int(thresholds['min_cross_package_edges'])}"
        )

    if report.failures:
        report.verdict = VERDICT_FAIL
    elif report.warnings:
        report.verdict = VERDICT_LOW
    return report


# Advisory cost guards: the post-build advisory is best-effort and must never
# meaningfully extend a build. Repos without a graphify.toml skip it entirely
# (byte-identical legacy behavior); oversized graph files are skipped rather
# than re-parsed; and classification samples evenly instead of touching every
# node - dominance is a ratio, so a few thousand nodes answer it.
_ADVISORY_MAX_GRAPH_BYTES = 32 * 1024 * 1024
_ADVISORY_SAMPLE_NODES = 5000


def active_profile_name(cfg: ProjectConfig) -> str:
    """The profile governing this process, mirroring the resolver precedence:
    ``GRAPHIFY_PROFILE`` env (when it names a known profile) beats
    ``default_profile``. Explicit CLI selections are handled by callers."""
    import os

    env_name = os.environ.get("GRAPHIFY_PROFILE", "").strip()
    if env_name and cfg.profile(env_name) is not None:
        return env_name
    return cfg.default_profile


def maybe_warn_vendor_dominance(graph_path: "str | Path", *, stream=None) -> None:
    """One-line post-build advisory when vendor noise dominates the graph.

    Fail-open AND cheap by design: runs at the tail of a successful build, must
    never turn a good build into a failure (any exception is swallowed), and
    must never noticeably extend it. It therefore activates only for repos
    that opted into profiles (a graphify.toml exists), skips oversized graph
    files, and samples nodes for the share estimate. Profiles declared
    ``kind = "vendor"`` are exempt (dominance is their job) - the active
    profile follows the same env-then-default resolution as the path resolver.
    """
    try:
        cfg = load_config(None)
        if cfg.source_path is None:
            return  # no graphify.toml: legacy repo, keep output byte-identical
        name = active_profile_name(cfg)
        prof = cfg.profile(name) if name else None
        if prof is not None and prof.kind == "vendor":
            return
        gp = Path(graph_path)
        if not gp.is_file() or gp.stat().st_size > _ADVISORY_MAX_GRAPH_BYTES:
            return
        data = json.loads(gp.read_text(encoding="utf-8", errors="replace"))
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list) or not nodes:
            return
        step = max(1, len(nodes) // _ADVISORY_SAMPLE_NODES)
        vendor = classified = 0
        for node in nodes[::step]:
            if not isinstance(node, dict):
                continue
            src = node.get("source_file")
            if not src or not isinstance(src, str):
                continue
            classified += 1
            if classify_ownership(src, cfg.ownership) == "vendor":
                vendor += 1
        if not classified:
            return
        share = vendor / classified
        thresholds = prof.fitness if prof else DEFAULT_FITNESS
        if share > thresholds["max_vendor_share_low"]:
            out = stream if stream is not None else sys.stderr
            print(
                f"[graphify] warning: ~{share:.0%} of graph nodes are "
                "vendored/dependency code; product questions may surface "
                "noise. Consider a product/vendor profile split - run "
                "`graphify fitness` or see docs/profiles.md.",
                file=out,
            )
    except Exception:
        pass


def format_report(report: FitnessReport) -> str:
    m = report.metrics
    lines = [f"Graph fitness: {report.graph_path}"]
    if m:
        lines += [
            f"  nodes: {m['total_nodes']} total | first-party {m['first_party_nodes']} "
            f"({m['first_party_share']:.1%}) | vendor {m['vendor_nodes']} "
            f"({m['vendor_share']:.1%}) | generated {m['generated_nodes']} | "
            f"test {m['test_nodes']} | unclassified {m['unclassified_nodes']}",
            f"  edges: {m['total_edges']} total | cross-package {m['cross_package_edges']}",
            f"  communities: {m['communities']} | labeled {m['labeled_communities']} "
            f"({m['labeled_share']:.1%})",
            f"  size: {m['graph_size_bytes']:,} bytes | directed: {m['directed']} | "
            f"interpreter: {m['interpreter']}",
        ]
        if m.get("profile"):
            lines.append(f"  profile: {m['profile']}"
                         + (f" (kind={m['profile_kind']})" if m['profile_kind'] else ""))
    for f in report.failures:
        lines.append(f"  FAIL: {f}")
    for w in report.warnings:
        lines.append(f"  warning: {w}")
    for n in report.notes:
        lines.append(f"  note: {n}")
    lines.append(f"VERDICT: {report.verdict}")
    return "\n".join(lines)
