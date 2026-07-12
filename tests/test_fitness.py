"""Graph fitness: metrics, verdicts, edge-shape tolerance, and CLI exit codes.

Fixtures are synthetic and neutral. Covers: every reported metric, the
PASS/LOW/FAIL ladder, the vendor-kind exemption, links/edges + source/from
key-shape variance (#738 normalization), nodes without source_file
(unclassified bucket - must never inflate first-party share), missing
directed flag (reported unknown, LOW), missing/unparseable graphs (FAIL),
label coverage against the .graphify_labels.json sidecar, cross-package
counting with and without package_roots, and the fitness CLI exit codes.
"""
import json
import subprocess
import sys

import pytest

from graphify.fitness import (
    VERDICT_FAIL,
    VERDICT_LOW,
    VERDICT_PASS,
    compute_fitness,
    maybe_warn_vendor_dominance,
)
from graphify.profiles import load_config


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_OUT", raising=False)
    monkeypatch.delenv("GRAPHIFY_PROFILE", raising=False)


def _graph(tmp_path, nodes, edges, *, edge_key="links", directed=True, name="graph.json"):
    data = {"nodes": nodes, edge_key: edges}
    if directed is not None:
        data["directed"] = directed
    gp = tmp_path / name
    gp.write_text(json.dumps(data), encoding="utf-8")
    return gp


def _node(nid, src, community=0):
    node = {"id": nid, "community": community}
    if src is not None:
        node["source_file"] = src
    return node


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def test_all_metrics_reported(tmp_path):
    gp = _graph(
        tmp_path,
        nodes=[
            _node("a", "src/app.py", 0),
            _node("b", "lib/util.py", 0),
            _node("c", "vendor/dep/x.js", 1),
            _node("d", "tests/test_app.py", 1),
            _node("e", None, 2),  # unclassified: no source_file
        ],
        edges=[
            {"source": "a", "target": "b", "relation": "calls"},   # src != lib -> cross
            {"source": "a", "target": "a", "relation": "calls"},   # same package
        ],
    )
    report = compute_fitness(gp, root=tmp_path)
    m = report.metrics
    assert m["total_nodes"] == 5
    assert m["total_edges"] == 2
    assert m["vendor_nodes"] == 1
    assert m["test_nodes"] == 1
    assert m["first_party_nodes"] == 2
    assert m["unclassified_nodes"] == 1
    # Shares over CLASSIFIED nodes only (4), never inflated by unclassified.
    assert m["first_party_share"] == 0.5
    assert m["vendor_share"] == 0.25
    assert m["communities"] == 3
    assert m["cross_package_edges"] == 1
    assert m["graph_size_bytes"] > 0
    assert m["directed"] is True
    assert m["interpreter"] == "missing"
    assert report.verdict == VERDICT_PASS


def test_label_coverage_from_sidecar(tmp_path):
    gp = _graph(tmp_path, [_node("a", "src/a.py", 0), _node("b", "src/b.py", 1)], [])
    (tmp_path / ".graphify_labels.json").write_text(
        json.dumps({"0": "Auth Layer", "1": "Community 1"}), encoding="utf-8"
    )
    m = compute_fitness(gp, root=tmp_path).metrics
    assert m["labeled_communities"] == 1     # placeholder does not count
    assert m["labeled_share"] == 0.5


def test_placeholder_labels_are_note_not_low(tmp_path):
    gp = _graph(tmp_path, [_node("a", "src/a.py", 0)], [])
    report = compute_fitness(gp, root=tmp_path)
    assert report.verdict == VERDICT_PASS
    assert any("placeholder" in n for n in report.notes)


# --------------------------------------------------------------------------- #
# edge-shape variance (#738): links/edges, source/from, target/to
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("edge_key", ["links", "edges"])
def test_edge_container_key_variance(tmp_path, edge_key):
    gp = _graph(
        tmp_path,
        [_node("a", "pkg1/a.py"), _node("b", "pkg2/b.py")],
        [{"source": "a", "target": "b"}],
        edge_key=edge_key,
    )
    assert compute_fitness(gp, root=tmp_path).metrics["cross_package_edges"] == 1


def test_edge_endpoint_key_variance_from_to(tmp_path):
    gp = _graph(
        tmp_path,
        [_node("a", "pkg1/a.py"), _node("b", "pkg2/b.py")],
        [{"from": "a", "to": "b"}],
    )
    assert compute_fitness(gp, root=tmp_path).metrics["cross_package_edges"] == 1


# --------------------------------------------------------------------------- #
# cross-package segmentation
# --------------------------------------------------------------------------- #
def test_package_roots_split_shared_toplevel(tmp_path):
    (tmp_path / "graphify.toml").write_text(
        'package_roots = ["packages"]\n', encoding="utf-8"
    )
    gp = _graph(
        tmp_path,
        [_node("a", "packages/web/a.ts"), _node("b", "packages/api/b.ts")],
        [{"source": "a", "target": "b"}],
    )
    # Without package_roots both live under "packages" (0 cross); with it,
    # packages/web vs packages/api are distinct.
    cfg = load_config(tmp_path)
    assert compute_fitness(gp, config=cfg).metrics["cross_package_edges"] == 1
    gp2 = _graph(tmp_path, [_node("a", "packages/web/a.ts"), _node("b", "packages/api/b.ts")],
                 [{"source": "a", "target": "b"}], name="graph2.json")
    from graphify.profiles import ProjectConfig
    assert compute_fitness(gp2, config=ProjectConfig(root=tmp_path)).metrics[
        "cross_package_edges"] == 0


# --------------------------------------------------------------------------- #
# verdict ladder
# --------------------------------------------------------------------------- #
def _vendor_heavy(tmp_path, share_vendor, total=10, **kw):
    nodes = []
    for i in range(share_vendor):
        nodes.append(_node(f"v{i}", f"vendor/dep/f{i}.js", i))
    for i in range(total - share_vendor):
        nodes.append(_node(f"p{i}", f"src/f{i}.py", 100 + i))
    return _graph(tmp_path, nodes, [], **kw)


def test_vendor_dominance_low(tmp_path):
    gp = _vendor_heavy(tmp_path, 7)   # 70% vendor: > 60% low, < 85% fail
    report = compute_fitness(gp, root=tmp_path)
    assert report.verdict == VERDICT_LOW
    assert any("vendor share" in w for w in report.warnings)


def test_vendor_dominance_fail(tmp_path):
    gp = _vendor_heavy(tmp_path, 9)   # 90% vendor
    report = compute_fitness(gp, root=tmp_path)
    assert report.verdict == VERDICT_FAIL


def test_vendor_kind_profile_exempt_from_dominance(tmp_path):
    (tmp_path / "graphify.toml").write_text(
        '[profiles.deps]\nout = "graphify-deps"\nkind = "vendor"\n',
        encoding="utf-8",
    )
    gp = _vendor_heavy(tmp_path, 10)  # 100% vendor
    cfg = load_config(tmp_path)
    report = compute_fitness(gp, config=cfg, profile_name="deps")
    assert report.verdict == VERDICT_PASS


def test_missing_graph_fails(tmp_path):
    report = compute_fitness(tmp_path / "nope" / "graph.json", root=tmp_path)
    assert report.verdict == VERDICT_FAIL
    assert any("missing graph" in f for f in report.failures)


def test_unparseable_graph_fails(tmp_path):
    gp = tmp_path / "graph.json"
    gp.write_text("{not json", encoding="utf-8")
    report = compute_fitness(gp, root=tmp_path)
    assert report.verdict == VERDICT_FAIL


def test_missing_directed_flag_is_unknown_and_low(tmp_path):
    gp = _graph(tmp_path, [_node("a", "src/a.py")], [], directed=None)
    report = compute_fitness(gp, root=tmp_path)
    assert report.metrics["directed"] == "unknown"
    assert report.verdict == VERDICT_LOW


def test_min_thresholds_drive_low(tmp_path):
    (tmp_path / "graphify.toml").write_text(
        'default_profile = "app"\n'
        '[profiles.app]\nout = "graphify-app"\n'
        '[profiles.app.fitness]\nmin_cross_package_edges = 5\n',
        encoding="utf-8",
    )
    gp = _graph(tmp_path, [_node("a", "src/a.py")], [])
    cfg = load_config(tmp_path)
    report = compute_fitness(gp, config=cfg, profile_name="app")
    assert report.verdict == VERDICT_LOW
    assert any("cross-package" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# interpreter sidecar
# --------------------------------------------------------------------------- #
def test_interpreter_ok_and_stale(tmp_path):
    gp = _graph(tmp_path, [_node("a", "src/a.py")], [])
    sidecar = tmp_path / ".graphify_python"
    sidecar.write_text(sys.executable, encoding="utf-8")
    assert compute_fitness(gp, root=tmp_path).metrics["interpreter"] == "ok"
    sidecar.write_text(str(tmp_path / "gone" / "python"), encoding="utf-8")
    assert compute_fitness(gp, root=tmp_path).metrics["interpreter"] == "stale"


# --------------------------------------------------------------------------- #
# post-build advisory
# --------------------------------------------------------------------------- #
def _opt_in_toml(tmp_path, extra=""):
    (tmp_path / "graphify.toml").write_text(
        'default_profile = "app"\n[profiles.app]\nout = "graphify-app"\n' + extra,
        encoding="utf-8",
    )


def test_maybe_warn_vendor_dominance_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _opt_in_toml(tmp_path)  # advisory activates only for profile-opted repos
    gp = _vendor_heavy(tmp_path, 9)
    import io
    buf = io.StringIO()
    maybe_warn_vendor_dominance(gp, stream=buf)
    assert "vendored/dependency code" in buf.getvalue()


def test_maybe_warn_silent_without_toml_backcompat(tmp_path, monkeypatch):
    # The byte-identical promise: repos with NO graphify.toml must see zero
    # new output, even on a maximally vendor-dominated graph.
    monkeypatch.chdir(tmp_path)
    gp = _vendor_heavy(tmp_path, 10)
    import io
    buf = io.StringIO()
    maybe_warn_vendor_dominance(gp, stream=buf)
    assert buf.getvalue() == ""


def test_maybe_warn_env_vendor_profile_exempt(tmp_path, monkeypatch):
    # GRAPHIFY_PROFILE naming a kind=vendor profile must suppress the advisory
    # (same resolution chain as the path resolver, not just default_profile).
    monkeypatch.chdir(tmp_path)
    _opt_in_toml(tmp_path, '[profiles.deps]\nout = "graphify-deps"\nkind = "vendor"\n')
    monkeypatch.setenv("GRAPHIFY_PROFILE", "deps")
    gp = _vendor_heavy(tmp_path, 10)
    import io
    buf = io.StringIO()
    maybe_warn_vendor_dominance(gp, stream=buf)
    assert buf.getvalue() == ""


def test_maybe_warn_skips_oversized_graph(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _opt_in_toml(tmp_path)
    import graphify.fitness as fit
    monkeypatch.setattr(fit, "_ADVISORY_MAX_GRAPH_BYTES", 10)
    gp = _vendor_heavy(tmp_path, 9)  # file is bigger than 10 bytes
    import io
    buf = io.StringIO()
    maybe_warn_vendor_dominance(gp, stream=buf)
    assert buf.getvalue() == ""


def test_maybe_warn_vendor_dominance_silent_when_healthy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _opt_in_toml(tmp_path)
    gp = _vendor_heavy(tmp_path, 1)
    import io
    buf = io.StringIO()
    maybe_warn_vendor_dominance(gp, stream=buf)
    assert buf.getvalue() == ""


def test_maybe_warn_never_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _opt_in_toml(tmp_path)
    maybe_warn_vendor_dominance(tmp_path / "missing.json")  # must not raise


def test_active_profile_name_env_beats_default(tmp_path, monkeypatch):
    from graphify.fitness import active_profile_name
    _opt_in_toml(tmp_path, '[profiles.deps]\nout = "graphify-deps"\n')
    cfg = load_config(tmp_path)
    assert active_profile_name(cfg) == "app"
    monkeypatch.setenv("GRAPHIFY_PROFILE", "deps")
    assert active_profile_name(cfg) == "deps"
    monkeypatch.setenv("GRAPHIFY_PROFILE", "ghost")  # unknown -> default
    assert active_profile_name(cfg) == "app"


# --------------------------------------------------------------------------- #
# CLI: exit codes and dispatch (real subprocess)
# --------------------------------------------------------------------------- #
def _run_cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "graphify", *args],
        capture_output=True, text=True, cwd=str(cwd),
    )


def test_cli_fitness_pass_exit_zero(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    _graph(out, [_node("a", "src/a.py")], [])
    result = _run_cli(["fitness"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "VERDICT: PASS" in result.stdout


def test_cli_fitness_fail_exit_one(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    nodes = [_node(f"v{i}", f"vendor/x/f{i}.js", i) for i in range(9)]
    nodes.append(_node("p", "src/a.py", 99))
    _graph(out, nodes, [])
    result = _run_cli(["fitness"], tmp_path)
    assert result.returncode == 1
    assert "VERDICT: FAIL" in result.stdout


def test_cli_fitness_strict_makes_low_exit_one(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    nodes = [_node(f"v{i}", f"vendor/x/f{i}.js", i) for i in range(7)]
    nodes += [_node(f"p{i}", f"src/f{i}.py", 100 + i) for i in range(3)]
    _graph(out, nodes, [])
    assert _run_cli(["fitness"], tmp_path).returncode == 0
    assert _run_cli(["fitness", "--strict"], tmp_path).returncode == 1


def test_cli_fitness_json_shape(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir()
    _graph(out, [_node("a", "src/a.py")], [])
    result = _run_cli(["fitness", "--json"], tmp_path)
    data = json.loads(result.stdout)
    assert data["verdict"] == "PASS"
    assert "metrics" in data and data["metrics"]["total_nodes"] == 1


def test_cli_fitness_unknown_profile_exit_two(tmp_path):
    result = _run_cli(["fitness", "--profile", "ghost"], tmp_path)
    assert result.returncode == 2
    assert "ghost" in result.stderr


def test_cli_fitness_graph_override_still_validates_profile(tmp_path):
    # --graph short-circuits path resolution but must NOT bypass explicit
    # profile validation: a typo'd --profile hard-errors, never silently
    # grades with default thresholds.
    out = tmp_path / "graphify-out"
    out.mkdir()
    gp = _graph(out, [_node("a", "src/a.py")], [])
    result = _run_cli(["fitness", "--graph", str(gp), "--profile", "ghost"], tmp_path)
    assert result.returncode == 2
    assert "ghost" in result.stderr


def test_cli_fitness_env_vendor_profile_keeps_exemption(tmp_path):
    # GRAPHIFY_PROFILE selecting a kind=vendor profile must grade with that
    # profile's rules (dominance-exempt), not default_profile's.
    import os as _os
    (tmp_path / "graphify.toml").write_text(
        'default_profile = "app"\n'
        '[profiles.app]\nout = "graphify-app"\n'
        '[profiles.deps]\nout = "graphify-deps"\nkind = "vendor"\n',
        encoding="utf-8",
    )
    deps = tmp_path / "graphify-deps"
    deps.mkdir()
    nodes = [_node(f"v{i}", f"vendor/x/f{i}.js", i) for i in range(10)]
    _graph(deps, nodes, [])
    env = dict(_os.environ)
    env.pop("GRAPHIFY_OUT", None)
    env["GRAPHIFY_PROFILE"] = "deps"
    result = subprocess.run(
        [sys.executable, "-m", "graphify", "fitness"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERDICT: PASS" in result.stdout


def test_cli_profiles_lists_default_and_kind(tmp_path):
    (tmp_path / "graphify.toml").write_text(
        'default_profile = "app"\n'
        '[profiles.app]\nout = "graphify-app"\n'
        '[profiles.deps]\nout = "graphify-deps"\nkind = "vendor"\n',
        encoding="utf-8",
    )
    result = _run_cli(["profiles"], tmp_path)
    assert result.returncode == 0
    assert "app" in result.stdout and "default" in result.stdout
    assert "kind=vendor" in result.stdout


def test_cli_profiles_empty_message(tmp_path):
    result = _run_cli(["profiles"], tmp_path)
    assert result.returncode == 0
    assert "No graph profiles defined" in result.stdout
