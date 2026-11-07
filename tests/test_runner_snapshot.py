import json
import sys

import pytest

from traceforge.config import Settings
from traceforge.runner import VerificationError, repository_manifest, run_verification, snapshot_test_command


@pytest.fixture
def snapshot_repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'main.py').write_text('def answer():\n    return 1\n', encoding='utf-8')
    (root / 'test_main.py').write_text(
        'import unittest\nfrom main import answer\nclass Test(unittest.TestCase):\n'
        '    def test_answer(self): self.assertEqual(answer(), 2)\n', encoding='utf-8')
    settings = Settings(data_dir=tmp_path / 'state', allowed_roots=[root], test_timeout=15)
    project = {'root': str(root), 'runner': 'local', 'trusted': True,
               'test_command': [sys.executable, '-m', 'unittest', 'discover']}
    patch = {'path': 'main.py', 'before': 'return 1', 'after': 'return 2',
             'reason': 'Fix regression', 'evidence_ids': ['source']}
    return root, settings, project, patch


@pytest.mark.parametrize('style', ['unittest-options', 'unittest-positionals', 'unittest-file',
                                  'pytest-directory', 'pytest-node'])
def test_absolute_source_targets_run_only_snapshot_after_source_changes(snapshot_repo, style):
    root, settings, project, patch = snapshot_repo
    arguments = {
        'unittest-options': ['unittest', 'discover', '--start-directory=' + str(root), '-t' + str(root)],
        'unittest-positionals': ['unittest', 'discover', str(root), 'test*.py', str(root)],
        'unittest-file': ['unittest', str(root / 'test_main.py')],
        'pytest-directory': ['pytest', '-q', str(root)],
        'pytest-node': ['pytest', str(root / 'test_main.py') + '::Test::test_answer', '-q'],
    }[style]
    project['test_command'] = [sys.executable, '-m', *arguments]
    project['_expected_manifest'] = repository_manifest(root, settings)
    patch = {**patch, 'after': 'return 1  # cosmetic only'}

    def mutate_source(kind, payload):
        if kind == 'verification.result' and payload['phase'] == 'baseline':
            (root / 'main.py').write_text('def answer():\n    return 2  # independent external edit\n', encoding='utf-8')

    result = run_verification(project, [patch], settings, 'absolute-' + style, progress=mutate_source)
    assert not result['regression_fixed']
    assert result['baseline']['tests']['failed'] == result['patched']['tests']['failed'] == 1
    assert not (root / '__pycache__').exists()
    assert 'independent external edit' in (root / 'main.py').read_text(encoding='utf-8')


@pytest.mark.parametrize('framework,arguments', [
    ('unittest', ['discover', '-s', '../outside']),
    ('unittest', ['discover', '.', 'test*.py', '..']),
    ('unittest', ['package_that_is_not_in_snapshot.Tests']),
    ('unittest', ['discover', '-p', '../test*.py']),
    ('unittest', ['discover', '--unknown-option']),
    ('pytest', ['../outside/test_main.py']),
    ('pytest', ['--pyargs', 'installed_package']),
    ('pytest', ['-o', 'pythonpath=..']),
    ('pytest', ['--rootdir=..']),
    ('pytest', ['--confcutdir=..']),
    ('pytest', ['--import-mode=append']),
    ('pytest', ['-p', 'external_plugin']),
    ('pytest', ['-c', '../pytest.ini']),
    ('pytest', ['@arguments.txt']),
])
def test_escaping_or_uncontrolled_options_fail_before_execution(snapshot_repo, monkeypatch, framework, arguments):
    _root, settings, project, patch = snapshot_repo
    project['test_command'] = [sys.executable, '-m', framework, *arguments]

    def unexpected_execution(*args, **kwargs):
        pytest.fail('unsafe command reached subprocess execution')

    monkeypatch.setattr('traceforge.runner._execute', unexpected_execution)
    with pytest.raises(VerificationError):
        run_verification(project, [patch], settings, 'unsafe')


def test_normalized_relative_paths_and_dotted_unittest_targets_remain_supported(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    (root / 'tests').mkdir()
    manifest = repository_manifest(root, settings)
    command = snapshot_test_command([sys.executable, '-m', 'pytest', './tests/../test_main.py', '-k', 'test_answer'],
                                    root, manifest)
    assert 'test_main.py' in command
    assert command[command.index('-k') + 1] == 'test_answer'
    project['test_command'] = [sys.executable, '-m', 'unittest', 'test_main.Test.test_answer', '-v']
    assert run_verification(project, [patch], settings, 'dotted')['regression_fixed']


@pytest.mark.parametrize('filename,body', [
    ('pytest.ini', '[pytest]\npythonpath = {source}\n'),
    ('.pytest.ini', '[pytest]\ntestpaths = ../outside\n'),
    ('tox.ini', '[pytest]\naddopts = --override-ini pythonpath=..\n'),
    ('setup.cfg', '[tool:pytest]\ncache_dir = ../cache\n'),
    ('setup.cfg', '[pytest]\npythonpath = ../outside\n'),
    ('pytest.toml', '[pytest]\npythonpath = ["../outside"]\n'),
    ('.pytest.toml', '[pytest]\naddopts = ["--pyargs", "installed_package"]\n'),
    ('pyproject.toml', '[tool.pytest.ini_options]\nlog_file = "../test.log"\n'),
    ('pyproject.toml', '[tool.pytest]\ntestpaths = ["../outside"]\n'),
])
def test_pytest_config_cannot_redirect_snapshot_imports_or_outputs(snapshot_repo, monkeypatch, filename, body):
    root, settings, project, patch = snapshot_repo
    (root / filename).write_text(body.format(source=root.as_posix()), encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest', '-q']
    monkeypatch.setattr('traceforge.runner._execute', lambda *args, **kwargs: pytest.fail('config reached execution'))
    with pytest.raises(VerificationError):
        run_verification(project, [patch], settings, 'config')


def test_explicit_custom_toml_config_also_checks_tool_pytest(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    (root / 'custom.toml').write_text('[tool.pytest.ini_options]\npythonpath = ["../outside"]\n', encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest', '-c', str(root / 'custom.toml')]
    with pytest.raises(VerificationError):
        run_verification(project, [patch], settings, 'custom')


def test_ancestor_pytest_config_and_conftest_are_not_loaded(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    (root.parent / 'pytest.ini').write_text('[pytest]\naddopts = --pyargs never_load_ancestor_config\n', encoding='utf-8')
    (root.parent / 'conftest.py').write_text('raise RuntimeError("ancestor conftest was loaded")\n', encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest', '-q']
    result = run_verification(project, [patch], settings, 'ancestor')
    assert result['regression_fixed'], result


@pytest.mark.parametrize('filename,body', [
    ('pytest.ini', '[pytest]\npythonpath = src\ntestpaths = .\naddopts = -q\n'),
    ('pyproject.toml', '[tool.pytest.ini_options]\npythonpath = ["src"]\naddopts = "-q"\n'),
])
def test_src_project_with_relative_pythonpath_verifies(snapshot_repo, filename, body):
    root, settings, project, patch = snapshot_repo
    package = root / 'src' / 'product'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'main.py').write_text((root / 'main.py').read_text(encoding='utf-8'), encoding='utf-8')
    (root / 'main.py').unlink()
    (root / 'test_main.py').write_text((root / 'test_main.py').read_text(encoding='utf-8').replace(
        'from main', 'from product.main'), encoding='utf-8')
    (root / filename).write_text(body, encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest']
    result = run_verification(project, [{**patch, 'path': 'src/product/main.py'}], settings, 'src')
    assert result['regression_fixed'], result
    assert not list((root / 'src').rglob('__pycache__'))


@pytest.mark.parametrize('framework', ['unittest', 'pytest'])
def test_original_repository_module_import_invalidates_observation(snapshot_repo, framework):
    root, settings, project, patch = snapshot_repo
    # Reproduce ordinary sys.path/editable redirection without mutating the host environment.
    test = root / 'test_main.py'
    test.write_text('import sys\nsys.path.insert(0, ' + repr(str(root)) + ')\n' +
                    test.read_text(encoding='utf-8'), encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', framework, *(['discover'] if framework == 'unittest' else ['-q'])]

    def mutate_source(kind, payload):
        if kind == 'verification.result' and payload['phase'] == 'baseline':
            (root / 'main.py').write_text('def answer():\n    return 2  # independent external edit\n', encoding='utf-8')

    result = run_verification(project, [{**patch, 'after': 'return 1  # cosmetic only'}], settings,
                              'import-' + framework, progress=mutate_source)
    assert not result['regression_fixed']
    for phase in ('baseline', 'patched'):
        assert result[phase]['tests'] is None
        assert result[phase]['exit_code'] == 2
        assert 'TRACEFORGE_SNAPSHOT_IMPORT_ERROR' in result[phase]['stderr']
        assert 'editable' in result[phase]['stderr']
    assert not (root / '__pycache__').exists()


def test_editable_meta_path_finder_does_not_bypass_snapshot_provenance(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    source_path = json.dumps(str(root / 'main.py'))
    test = root / 'test_main.py'
    test.write_text(
        'import sys, importlib.util\n'
        'class EditableFinder:\n'
        '    @staticmethod\n'
        '    def find_spec(fullname, path=None, target=None):\n'
        '        if fullname == "main":\n'
        f'            return importlib.util.spec_from_file_location(fullname, {source_path})\n'
        'sys.meta_path.insert(0, EditableFinder)\n' + test.read_text(encoding='utf-8'), encoding='utf-8')
    result = run_verification(project, [patch], settings, 'editable')
    assert not result['regression_fixed']
    assert result['baseline']['tests'] is None
    assert 'TRACEFORGE_SNAPSHOT_IMPORT_ERROR' in result['patched']['stderr']


@pytest.mark.parametrize('framework', ['unittest', 'pytest'])
def test_test_definitions_imported_from_outside_snapshot_are_rejected(snapshot_repo, framework):
    root, settings, project, patch = snapshot_repo
    (root.parent / 'external_suite.py').write_text((root / 'test_main.py').read_text(encoding='utf-8'), encoding='utf-8')
    (root / 'test_main.py').write_text('import sys\nsys.path.append(' + repr(str(root.parent)) + ')\n'
                                     'from external_suite import Test\n', encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', framework, *(['discover'] if framework == 'unittest' else ['-q'])]
    result = run_verification(project, [patch], settings, 'external-test-' + framework)
    assert not result['regression_fixed']
    assert result['patched']['tests'] is None
    assert '测试定义文件不在获批快照中' in result['patched']['stderr']


def test_dotted_discovery_package_uses_snapshot_top_level(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    package = root / 'suite' / 'inner'
    package.mkdir(parents=True)
    (package.parent / '__init__.py').write_text('', encoding='utf-8')
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'test_main.py').write_text((root / 'test_main.py').read_text(encoding='utf-8'), encoding='utf-8')
    (root / 'test_main.py').unlink()
    project['test_command'] = [sys.executable, '-m', 'unittest', 'discover', '-s', 'suite.inner']
    result = run_verification(project, [patch], settings, 'discovery-package')
    assert result['regression_fixed'], result
    assert result['patched']['tests']['identities'] == ['suite.inner.test_main.Test.test_answer']


def test_absolute_pytest_path_cannot_turn_into_an_option(snapshot_repo):
    root, settings, _project, _patch = snapshot_repo
    (root / '-x').write_text('an ordinary repository file', encoding='utf-8')
    command = snapshot_test_command([sys.executable, '-m', 'pytest', str(root / '-x')],
                                    root, repository_manifest(root, settings))
    assert command[-1] == './-x'


def test_patch_cannot_change_test_definitions_under_an_implementation_filename(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    (root / 'main.py').write_text((root / 'main.py').read_text(encoding='utf-8') + '\n' +
                                 (root / 'test_main.py').read_text(encoding='utf-8').replace(
                                     'from main import answer\n', ''), encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'unittest', 'main.Test.test_answer']
    result = run_verification(project, [patch], settings, 'implementation-test')
    assert not result['regression_fixed']
    assert result['patched']['tests'] is None
    assert '补丁不得修改实际测试定义文件' in result['patched']['stderr']


@pytest.mark.parametrize('framework,regular_package', [('unittest', True), ('pytest', True), ('pytest', False)])
def test_lib_package_installed_copy_cannot_prove_a_snapshot_fix(snapshot_repo, framework, regular_package):
    root, settings, project, patch = snapshot_repo
    package = root / 'lib' / 'product'
    package.mkdir(parents=True)
    if regular_package:
        (package / '__init__.py').write_text('', encoding='utf-8')
    else:
        # An installed regular package wins over a local namespace package,
        # even when its parent directory appears later in sys.path.
        (root / 'pytest.ini').write_text('[pytest]\npythonpath = lib\n', encoding='utf-8')
    (root / 'main.py').replace(package / 'main.py')
    external = root.parent / 'installed' / 'product'
    external.mkdir(parents=True)
    (external / '__init__.py').write_text('', encoding='utf-8')
    (external / 'main.py').write_text((package / 'main.py').read_text(encoding='utf-8'), encoding='utf-8')
    test = root / 'test_main.py'
    test.write_text('import sys\nsys.path.append(' + repr(str(external.parent)) + ')\n' +
                    test.read_text(encoding='utf-8').replace('from main', 'from product.main'), encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', framework, *(['discover'] if framework == 'unittest' else ['-q'])]

    def mutate_installed_copy(kind, payload):
        if kind == 'verification.result' and payload['phase'] == 'baseline':
            (external / 'main.py').write_text('def answer():\n    return 2\n', encoding='utf-8')

    result = run_verification(project, [{**patch, 'path': 'lib/product/main.py',
                                         'after': 'return 1  # cosmetic only'}], settings,
                              'lib-installed-' + framework, progress=mutate_installed_copy)
    assert not result['regression_fixed']
    assert result['baseline']['tests'] is result['patched']['tests'] is None
    assert 'product.main: 项目模块来自快照之外' in result['patched']['stderr']
    assert (package / 'main.py').read_text(encoding='utf-8') == 'def answer():\n    return 1\n'


@pytest.mark.parametrize('regular_package', [True, False])
def test_explicit_non_src_import_root_verifies_without_basename_collisions(snapshot_repo, regular_package):
    root, settings, project, patch = snapshot_repo
    package = root / 'python' / 'product'
    package.mkdir(parents=True)
    if regular_package:
        (package / '__init__.py').write_text('', encoding='utf-8')
    (root / 'main.py').replace(package / 'main.py')
    # These are product.json and examples.json, never the installed stdlib json.
    (package / 'json.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'examples').mkdir()
    (root / 'examples' / 'json.py').write_text('VALUE = 2\n', encoding='utf-8')
    test = root / 'test_main.py'
    test.write_text('import json\n' + test.read_text(encoding='utf-8').replace('from main', 'from product.main'),
                    encoding='utf-8')
    (root / 'pytest.ini').write_text('[pytest]\npythonpath = python\n', encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest', '-q']
    result = run_verification(project, [{**patch, 'path': 'python/product/main.py'}], settings, 'python-layout')
    assert result['regression_fixed'], result


@pytest.mark.parametrize('filename,body', [
    ('pytest.ini', '[pytest]\npythonpath = ../src\ntestpaths = tests\ncache_dir = cache\nlog_file = logs/run.log\n'),
    ('pyproject.toml', ('[tool.pytest.ini_options]\npythonpath = ["../src"]\ntestpaths = ["tests"]\n'
                        'cache_dir = "cache"\nlog_file = "logs/run.log"\n')),
])
def test_nested_pytest_config_preserves_each_path_base(snapshot_repo, filename, body):
    root, settings, project, patch = snapshot_repo
    (root / 'src').mkdir()
    (root / 'config').mkdir()
    (root / 'tests').mkdir()
    (root / 'main.py').replace(root / 'src' / 'main.py')
    (root / 'test_main.py').replace(root / 'tests' / 'test_main.py')
    (root / 'tests' / 'test_paths.py').write_text(
        'from pathlib import Path\n'
        'def test_paths(pytestconfig):\n'
        '    assert pytestconfig.getini("pythonpath")[0].resolve() == Path.cwd() / "src"\n'
        '    assert pytestconfig.cache._cachedir == Path.cwd() / "cache"\n'
        '    assert (Path.cwd() / "logs" / "run.log").is_file()\n', encoding='utf-8')
    (root / 'config' / filename).write_text(body, encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest', '-q', '-c', 'config/' + filename]
    result = run_verification(project, [{**patch, 'path': 'src/main.py'}], settings, 'nested-config')
    assert result['regression_fixed'], result
    assert result['patched']['tests']['executed'] == 2
    assert not (root / 'cache').exists()
    assert not (root / 'logs').exists()


def test_nested_pytest_pythonpath_still_rejects_escape(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    (root / 'config').mkdir()
    (root / 'config' / 'pytest.ini').write_text('[pytest]\npythonpath = ../../outside\n', encoding='utf-8')
    project['test_command'] = [sys.executable, '-m', 'pytest', '-c', 'config/pytest.ini']
    with pytest.raises(VerificationError, match='越出仓库快照'):
        run_verification(project, [patch], settings, 'nested-escape')


@pytest.mark.parametrize('change_test', [False, True])
def test_unittest_inherited_test_method_protects_actual_definition(snapshot_repo, change_test):
    root, settings, project, patch = snapshot_repo
    test = root / 'test_main.py'
    (root / 'checks.py').write_text(test.read_text(encoding='utf-8').replace('class Test(', 'class SharedAssertions('),
                                  encoding='utf-8')
    test.write_text('import checks\nclass Test(checks.SharedAssertions):\n    pass\n', encoding='utf-8')
    if change_test:
        patch = {**patch, 'path': 'checks.py', 'before': 'self.assertEqual(answer(), 2)',
                 'after': 'self.assertEqual(answer(), 1)'}
    result = run_verification(project, [patch], settings, 'inherited-definition')
    assert result['regression_fixed'] is not change_test, result
    if change_test:
        assert result['patched']['tests'] is None
        assert '补丁不得修改实际测试定义文件' in result['patched']['stderr']
    else:
        assert result['patched']['tests']['identities'] == ['test_main.Test.test_answer']


@pytest.mark.parametrize('hook', ['setUp', 'tearDown', 'setUpClass', 'tearDownClass'])
def test_unittest_inherited_hooks_cannot_be_modified_to_pass(snapshot_repo, hook):
    root, settings, project, patch = snapshot_repo
    decorator = '    @classmethod\n' if hook.endswith('Class') else ''
    argument = 'cls' if hook.endswith('Class') else 'self'
    (root / 'checks.py').write_text('import unittest\nclass SharedAssertions(unittest.TestCase):\n' + decorator +
                                  f'    def {hook}({argument}):\n        pass  # hook\n', encoding='utf-8')
    test = root / 'test_main.py'
    test.write_text('import checks\n' + test.read_text(encoding='utf-8').replace(
        'class Test(unittest.TestCase):', 'class Test(checks.SharedAssertions):'), encoding='utf-8')
    # Even a harmless hook edit is a test-definition change and cannot receive
    # credit alongside a real implementation fix.
    hook_patch = {**patch, 'path': 'checks.py', 'before': 'pass  # hook', 'after': 'pass  # changed hook'}
    result = run_verification(project, [patch, hook_patch], settings, 'inherited-hook-' + hook)
    assert not result['regression_fixed']
    assert result['patched']['tests'] is None
    assert '补丁不得修改实际测试定义文件' in result['patched']['stderr']


def test_unittest_external_wraps_decorator_preserves_snapshot_test_provenance(snapshot_repo):
    root, settings, project, patch = snapshot_repo
    (root.parent / 'decorators.py').write_text(
        'from functools import wraps\ndef decorate(function):\n'
        '    @wraps(function)\n    def wrapper(*args, **kwargs):\n'
        '        return function(*args, **kwargs)\n    return wrapper\n', encoding='utf-8')
    test = root / 'test_main.py'
    test.write_text('import sys\nsys.path.append(' + repr(str(root.parent)) + ')\nfrom decorators import decorate\n' +
                    test.read_text(encoding='utf-8').replace('    def test_answer', '    @decorate\n    def test_answer'),
                    encoding='utf-8')
    result = run_verification(project, [patch], settings, 'external-decorator')
    assert result['regression_fixed'], result


@pytest.mark.parametrize('framework', ['unittest', 'pytest'])
def test_state_inside_source_does_not_misidentify_approved_snapshot(snapshot_repo, framework):
    root, settings, project, patch = snapshot_repo
    settings.data_dir = root / '.traceforge'
    project['test_command'] = [sys.executable, '-m', framework, *(['discover'] if framework == 'unittest' else ['-q'])]
    result = run_verification(project, [patch], settings, 'nested-state')
    assert result['regression_fixed'], result
    assert result['patched']['tests']['passed'] == 1
    assert (root / 'main.py').read_text(encoding='utf-8') == 'def answer():\n    return 1\n'
    assert not (root / '__pycache__').exists()
