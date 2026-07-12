"""Project graph profiles: graphify.toml loading, precedence, and ownership.

Covers the back-compat contract (no toml -> zero behavior change), the
resolution precedence chain, explicit-request-fails-loudly semantics,
unknown-key forward compatibility, Windows path normalization, and the
ownership classifier. All fixtures are neutral (acme-style) by design.
"""
import os

import pytest

from graphify.paths import resolve_out_dir
from graphify.profiles import (
    DEFAULT_OWNERSHIP,
    ProfileError,
    classify_ownership,
    load_config,
    resolve_profile,
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_OUT", raising=False)
    monkeypatch.delenv("GRAPHIFY_PROFILE", raising=False)


def _write_toml(tmp_path, text):
    (tmp_path / "graphify.toml").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# load_config: absence, parsing, forward compatibility
# --------------------------------------------------------------------------- #
def test_no_toml_is_empty_noop_config(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.profiles == {}
    assert cfg.default_profile == ""
    assert cfg.source_path is None
    assert cfg.warnings == []
    assert cfg.ownership == DEFAULT_OWNERSHIP  # generic defaults intact


def test_basic_profiles_parse(tmp_path):
    _write_toml(tmp_path, (
        'default_profile = "app"\n'
        '[profiles.app]\n'
        'out = "graphify-app"\n'
        'exclude = ["third_party/"]\n'
        'description = "first-party graph"\n'
        '[profiles.deps]\n'
        'out = "graphify-deps"\n'
        'kind = "vendor"\n'
    ))
    cfg = load_config(tmp_path)
    assert set(cfg.profiles) == {"app", "deps"}
    assert cfg.default_profile == "app"
    assert cfg.profiles["app"].out == "graphify-app"
    assert cfg.profiles["app"].exclude == ["third_party/"]
    assert cfg.profiles["deps"].kind == "vendor"
    assert cfg.profiles["app"].directed is True
    assert cfg.warnings == []


def test_profile_out_defaults_to_prefixed_name(tmp_path):
    _write_toml(tmp_path, "[profiles.docs]\n")
    cfg = load_config(tmp_path)
    assert cfg.profiles["docs"].out == "graphify-docs"


def test_unparseable_toml_warns_and_degrades(tmp_path):
    _write_toml(tmp_path, "this is [ not toml = = =")
    cfg = load_config(tmp_path)
    assert cfg.profiles == {}
    assert cfg.warnings and "graphify.toml" in cfg.warnings[0]


def test_unknown_keys_warn_but_do_not_fail(tmp_path):
    _write_toml(tmp_path, (
        'future_top_level = 1\n'
        '[profiles.app]\n'
        'out = "graphify-app"\n'
        'future_key = "x"\n'
        '[profiles.app.fitness]\n'
        'future_threshold = 3\n'
        '[ownership]\n'
        'future_bucket = ["x/"]\n'
    ))
    cfg = load_config(tmp_path)
    assert "app" in cfg.profiles  # still parsed
    joined = " ".join(cfg.warnings)
    for fragment in ("future_top_level", "future_key", "future_threshold", "future_bucket"):
        assert fragment in joined


def test_default_profile_naming_missing_table_warns(tmp_path):
    _write_toml(tmp_path, 'default_profile = "ghost"\n[profiles.app]\nout = "o"\n')
    cfg = load_config(tmp_path)
    assert cfg.default_profile == ""
    assert any("ghost" in w for w in cfg.warnings)


def test_fitness_thresholds_parse_and_default(tmp_path):
    _write_toml(tmp_path, (
        '[profiles.app]\n'
        'out = "o"\n'
        '[profiles.app.fitness]\n'
        'max_vendor_share_fail = 0.5\n'
    ))
    cfg = load_config(tmp_path)
    fit = cfg.profiles["app"].fitness
    assert fit["max_vendor_share_fail"] == 0.5
    assert fit["max_vendor_share_low"] == 0.60  # untouched default


def test_windows_backslash_paths_normalized(tmp_path):
    _write_toml(tmp_path, (
        '[profiles.app]\n'
        'out = "graphs\\\\app"\n'
        'exclude = ["deps\\\\big"]\n'
    ))
    cfg = load_config(tmp_path)
    assert cfg.profiles["app"].out == "graphs/app"
    assert cfg.profiles["app"].exclude == ["deps/big"]


# --------------------------------------------------------------------------- #
# resolve_profile: explicit requests fail loudly
# --------------------------------------------------------------------------- #
def test_resolve_profile_found(tmp_path):
    _write_toml(tmp_path, '[profiles.app]\nout = "o"\n')
    assert resolve_profile("app", tmp_path).out == "o"


def test_resolve_profile_unknown_name_raises_with_known_list(tmp_path):
    _write_toml(tmp_path, '[profiles.app]\nout = "o"\n')
    with pytest.raises(ProfileError, match="app"):
        resolve_profile("tyop", tmp_path)


def test_resolve_profile_without_toml_raises(tmp_path):
    with pytest.raises(ProfileError, match="graphify.toml"):
        resolve_profile("app", tmp_path)


# --------------------------------------------------------------------------- #
# resolve_out_dir: the full precedence chain
# --------------------------------------------------------------------------- #
def test_precedence_explicit_out_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_OUT", "env-dir")
    assert resolve_out_dir(out="explicit-dir", root=tmp_path) == "explicit-dir"


def test_precedence_explicit_profile_beats_env(tmp_path, monkeypatch):
    _write_toml(tmp_path, '[profiles.app]\nout = "profile-dir"\n')
    monkeypatch.setenv("GRAPHIFY_OUT", "env-dir")
    assert resolve_out_dir(profile="app", root=tmp_path) == "profile-dir"


def test_precedence_env_out_beats_env_profile_and_toml(tmp_path, monkeypatch):
    _write_toml(tmp_path, (
        'default_profile = "app"\n[profiles.app]\nout = "toml-dir"\n'
    ))
    monkeypatch.setenv("GRAPHIFY_OUT", "env-dir")
    monkeypatch.setenv("GRAPHIFY_PROFILE", "app")
    assert resolve_out_dir(root=tmp_path) == "env-dir"


def test_precedence_env_profile_beats_toml_default(tmp_path, monkeypatch):
    _write_toml(tmp_path, (
        'default_profile = "app"\n'
        '[profiles.app]\nout = "default-dir"\n'
        '[profiles.alt]\nout = "alt-dir"\n'
    ))
    monkeypatch.setenv("GRAPHIFY_PROFILE", "alt")
    assert resolve_out_dir(root=tmp_path) == "alt-dir"


def test_precedence_toml_default_profile(tmp_path):
    _write_toml(tmp_path, 'default_profile = "app"\n[profiles.app]\nout = "toml-dir"\n')
    assert resolve_out_dir(root=tmp_path) == "toml-dir"


def test_precedence_legacy_fallback(tmp_path):
    assert resolve_out_dir(root=tmp_path) == "graphify-out"


def test_explicit_profile_typo_raises_never_falls_back(tmp_path):
    _write_toml(tmp_path, '[profiles.app]\nout = "o"\n')
    with pytest.raises(ProfileError):
        resolve_out_dir(profile="tyop", root=tmp_path)


# --------------------------------------------------------------------------- #
# classify_ownership
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,bucket", [
    ("src/app.py", "first_party"),
    ("vendor/lib/util.js", "vendor"),
    ("packages/web/node_modules/x/i.js", "vendor"),
    ("third_party/proto/gen.py", "vendor"),
    ("dist/bundle.min.js", "generated"),
    ("app/__pycache__/mod.pyc", "generated"),
    ("tests/test_app.py", "test"),
    ("src/feature.spec.ts", "test"),
    ("src\\win\\style.py", "first_party"),   # backslash input normalized
    ("vendor\\win\\lib.py", "vendor"),
])
def test_classify_ownership_defaults(path, bucket):
    assert classify_ownership(path) == bucket


def test_classify_ownership_custom_patterns_and_priority():
    own = {"vendor": ["deps/**"], "generated": [], "test": ["deps/**"]}
    # vendor is checked before test: a vendored test file is vendor noise.
    assert classify_ownership("deps/pkg/test_x.py", own) == "vendor"
    assert classify_ownership("src/x.py", own) == "first_party"


def test_classify_ownership_root_level_match():
    # "**/vendor/**" must also match a root-level "vendor/..." path.
    assert classify_ownership("vendor/x.js") == "vendor"


# --------------------------------------------------------------------------- #
# out-path validation: graphify.toml is repo-author input, not user input
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_out", [
    "/abs/dir",
    "C:/graphs",
    "c:\\\\graphs",
    "../outside",
    "safe/../../outside",
])
def test_profile_out_traversal_rejected_with_fallback(tmp_path, bad_out):
    _write_toml(tmp_path, f'[profiles.app]\nout = "{bad_out}"\n')
    cfg = load_config(tmp_path)
    assert cfg.profiles["app"].out == "graphify-app"
    assert any("relative path" in w for w in cfg.warnings)


def test_profile_out_nested_relative_is_allowed(tmp_path):
    _write_toml(tmp_path, '[profiles.app]\nout = "graphs/app"\n')
    cfg = load_config(tmp_path)
    assert cfg.profiles["app"].out == "graphs/app"
    assert cfg.warnings == []


def test_env_profile_typo_warns_on_resolve(tmp_path, monkeypatch, capsys):
    _write_toml(tmp_path, '[profiles.app]\nout = "o"\n')
    monkeypatch.setenv("GRAPHIFY_PROFILE", "tyop")
    assert resolve_out_dir(root=tmp_path) == "graphify-out"
    assert "GRAPHIFY_PROFILE" in capsys.readouterr().err
