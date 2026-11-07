"""Writable defaults stay outside installed resources on each supported platform."""
from pathlib import Path

import pytest

from traceforge import paths
from traceforge.config import Settings


@pytest.fixture
def installed_layout(tmp_path, monkeypatch):
    package = tmp_path / 'venv' / 'site-packages'
    assets = package / 'traceforge' / '_assets'
    assets.mkdir(parents=True)
    monkeypatch.setattr(paths, 'PACKAGE_ROOT', package)
    monkeypatch.setattr(paths, 'RESOURCE_ROOT', assets)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.delenv('TRACEFORGE_DATA_DIR', raising=False)
    return tmp_path


def test_source_checkout_keeps_local_state_directory(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="source"\n', encoding='utf-8')
    monkeypatch.setattr(paths, 'PACKAGE_ROOT', tmp_path)
    monkeypatch.setattr(paths, 'RESOURCE_ROOT', tmp_path)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'elsewhere'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'another-place'))
    assert paths.default_data_dir() == tmp_path / '.traceforge'


@pytest.mark.parametrize(('platform', 'suffix'), [
    ('win32', ('AppData', 'Local', 'TraceForge')),
    ('darwin', ('Library', 'Application Support', 'TraceForge')),
    ('linux', ('.local', 'share', 'traceforge')),
])
def test_installed_defaults_follow_platform_user_data(installed_layout, monkeypatch, platform, suffix):
    monkeypatch.setattr(paths.sys, 'platform', platform)
    actual = paths.default_data_dir()
    assert actual == installed_layout / 'home' / Path(*suffix)
    assert not actual.is_relative_to(paths.PACKAGE_ROOT)


@pytest.mark.parametrize(('platform', 'variable', 'name'), [
    ('win32', 'LOCALAPPDATA', 'TraceForge'), ('linux', 'XDG_DATA_HOME', 'traceforge'),
])
def test_installed_respects_platform_data_override(installed_layout, monkeypatch, platform, variable, name):
    base = installed_layout / 'custom-data'
    monkeypatch.setattr(paths.sys, 'platform', platform)
    monkeypatch.setenv(variable, str(base))
    assert paths.default_data_dir() == base / name


def test_packaged_assets_win_over_neighboring_pyproject(installed_layout, monkeypatch):
    (paths.PACKAGE_ROOT / 'pyproject.toml').write_text('[project]\nname="unrelated"\n', encoding='utf-8')
    monkeypatch.setattr(paths.sys, 'platform', 'linux')
    assert paths.default_data_dir() == installed_layout / 'home' / '.local' / 'share' / 'traceforge'


def test_non_checkout_layout_uses_user_data_without_pyproject(installed_layout, monkeypatch):
    monkeypatch.setattr(paths, 'RESOURCE_ROOT', paths.PACKAGE_ROOT)
    monkeypatch.setattr(paths.sys, 'platform', 'linux')
    assert paths.default_data_dir() == installed_layout / 'home' / '.local' / 'share' / 'traceforge'


def test_settings_environment_override_is_explicit_and_resolved(installed_layout, monkeypatch):
    target = installed_layout / 'explicit-state'
    monkeypatch.setenv('TRACEFORGE_DATA_DIR', str(target))
    monkeypatch.setenv('TRACEFORGE_ALLOWED_ROOTS', '[]')
    assert Settings.from_env().data_dir == target.resolve()
    assert not target.exists()


def test_settings_default_factory_uses_current_installation_layout(installed_layout, monkeypatch):
    monkeypatch.setattr(paths.sys, 'platform', 'linux')
    assert Settings().data_dir == installed_layout / 'home' / '.local' / 'share' / 'traceforge'
