"""External test observer launched before repository code.

This is an execution integrity check, not cryptographic attestation against
hostile Python reflection or monkey-patching inside the same interpreter.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

REPORT_LIMIT = 500_000
TEST_LIMIT = 2500
IDENTITY_LIMIT = 500


class SnapshotImports:
    """Reject accidental editable/installed imports of the project under test.

    Installed third-party dependencies remain available. This checks ordinary
    module provenance; arbitrary trusted Python code can still mutate sys.modules.
    """

    def __init__(self, policy):
        self.snapshot = Path.cwd().resolve()
        self.source = Path(policy['source_root']).resolve()
        self.runtime = Path(sys.prefix).resolve()
        self.observer = Path(__file__).resolve()
        self.files = {(self.snapshot / relative).resolve() for relative in policy['files']}
        self.modified = {(self.snapshot / relative).resolve() for relative in policy.get('patch_files', [])}
        self.expected = {}
        self.errors = set()
        relative_files = {Path(relative) for relative in policy['files']}
        import_roots = {Path('.'), Path('src'), *(Path(value) for value in policy.get('import_roots', []))}
        for relative in policy['files']:
            path = Path(relative)
            if path.suffix != '.py':
                continue
            variants = [path.relative_to(base).parts for base in import_roots
                        if path != base and path.is_relative_to(base)]
            # A regular package can live under lib/, python/, etc. Its actual
            # import name starts at the continuous __init__.py package chain,
            # not at an arbitrary ancestor or every matching basename.
            package = path.parent
            while package.parts and package / '__init__.py' in relative_files:
                package = package.parent
            if package != path.parent:
                variants.append(path.relative_to(package).parts)
            if Path('__init__.py') in relative_files:
                variants.append((self.source.name, *path.parts))
            for parts in variants:
                parts = [*parts[:-1], Path(parts[-1]).stem]
                if parts[-1] == '__init__':
                    parts.pop()
                if parts and all(part.isidentifier() for part in parts):
                    self.expected.setdefault('.'.join(parts), set()).add((self.snapshot / relative).resolve())

    @staticmethod
    def within(path, parent):
        return path == parent or parent in path.parents

    def check(self):
        for name, module in list(sys.modules.items()):
            if module is None:
                continue
            location = getattr(module, '__file__', None)
            locations = [location] if isinstance(location, str) else []
            namespace = getattr(module, '__path__', ())
            if not location:
                try:
                    locations.extend(str(item) for item in namespace)
                except (TypeError, RuntimeError):
                    continue
            for value in locations:
                try:
                    path = Path(value).resolve()
                except (OSError, ValueError):
                    self.errors.add(f'{name}: 无法确认模块来源')
                    continue
                in_runtime = (self.runtime != self.source and self.within(self.runtime, self.source)
                              and self.within(path, self.runtime))
                in_snapshot = self.within(path, self.snapshot)
                if self.within(path, self.source) and not (in_runtime or in_snapshot or path == self.observer):
                    self.errors.add(f'{name}: 导入了原仓库 {path}')
                elif name in self.expected and location and path not in self.expected[name]:
                    self.errors.add(f'{name}: 项目模块来自快照之外 {path}')
        return not self.errors

    def test_file(self, location, identity):
        try:
            path = Path(location).resolve() if location is not None else None
            valid = path in self.files
        except (OSError, ValueError, TypeError):
            valid = False
        if not valid:
            self.errors.add(f'{identity}: 测试定义文件不在获批快照中')
        elif path in self.modified:
            self.errors.add(f'{identity}: 补丁不得修改实际测试定义文件')

    def test_module(self, module_name, identity):
        module = sys.modules.get(module_name)
        self.test_file(getattr(module, '__file__', None), identity)

    def test_callable(self, function, identity, *, hook=False):
        # Follow functools.wraps without rejecting an ordinary installed
        # decorator's wrapper. Every repository wrapper remains protected.
        seen = set()
        while function is not None and id(function) not in seen:
            seen.add(id(function))
            function = getattr(function, '__func__', function)
            code = getattr(function, '__code__', None)
            wrapped = getattr(function, '__wrapped__', None)
            if code is not None:
                location = code.co_filename
                try:
                    modified = Path(location).resolve() in self.modified
                except (OSError, ValueError, TypeError):
                    modified = False
                if modified or (not hook and wrapped is None):
                    self.test_file(location, identity)
            elif not hook and wrapped is None:
                self.errors.add(f'{identity}: 无法确认实际测试方法的定义文件')
            function = wrapped

    def unittest_case(self, test):
        identity = test.id()
        self.test_module(type(test).__module__, identity)
        self.test_callable(getattr(test, test._testMethodName, None), identity)
        for name in ('setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'asyncSetUp', 'asyncTearDown'):
            self.test_callable(getattr(test, name, None), identity + ':' + name, hook=True)
        module = sys.modules.get(type(test).__module__)
        for name in ('setUpModule', 'tearDownModule'):
            self.test_callable(getattr(module, name, None), identity + ':' + name, hook=True)


class Recorder:
    def __init__(self):
        self.tests = {}
        self.finished = set()
        self.invalid = False

    def start(self, identity):
        identity = str(identity)
        if len(identity) > IDENTITY_LIMIT or len(self.tests) >= TEST_LIMIT or identity in self.tests:
            self.invalid = True
        else:
            self.tests[identity] = 'running'

    def outcome(self, identity, outcome):
        identity = str(identity)
        if identity not in self.tests:
            self.start(identity)
        previous = self.tests.get(identity)
        if previous != 'failed' and (previous != 'skipped' or outcome == 'failed'):
            self.tests[identity] = outcome

    def stop(self, identity):
        self.finished.add(str(identity))

    def report(self, framework, exit_code, completed=True):
        complete = completed and not self.invalid and set(self.tests) == self.finished
        entries = [{'id': identity, 'outcome': outcome} for identity, outcome in sorted(self.tests.items())]
        complete = complete and all(item['outcome'] in {'passed', 'failed', 'skipped'} for item in entries)
        return {'schema': 1, 'framework': framework, 'completed': complete, 'exit_code': exit_code,
                'tests': entries, 'executed': len(entries),
                **{name: sum(item['outcome'] == name for item in entries) for name in ('passed', 'failed', 'skipped')}}


def run_unittest(arguments, imports):
    recorder = Recorder()

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            imports.unittest_case(test)
            recorder.start(test.id())
            super().startTest(test)

        def stopTest(self, test):
            recorder.stop(test.id())
            super().stopTest(test)

        def addSuccess(self, test):
            recorder.outcome(test.id(), 'passed')
            super().addSuccess(test)

        def addFailure(self, test, err):
            recorder.outcome(test.id(), 'failed')
            super().addFailure(test, err)

        def addError(self, test, err):
            recorder.outcome(test.id(), 'failed')
            super().addError(test, err)

        def addSkip(self, test, reason):
            recorder.outcome(test.id(), 'skipped')
            # Class/module setup skips are recorded without startTest/stopTest.
            recorder.stop(test.id())
            super().addSkip(test, reason)

        def addExpectedFailure(self, test, err):
            recorder.outcome(test.id(), 'skipped')
            super().addExpectedFailure(test, err)

        def addUnexpectedSuccess(self, test):
            recorder.outcome(test.id(), 'failed')
            super().addUnexpectedSuccess(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                recorder.outcome(test.id(), 'failed')
            super().addSubTest(test, subtest, err)

    class Runner(unittest.TextTestRunner):
        resultclass = Result

    sys.path.insert(0, os.getcwd())
    program = unittest.main(module=None, argv=['python -m unittest', *arguments], exit=False, testRunner=Runner)
    code = 0 if program.result.wasSuccessful() else 1
    return code, recorder.report('unittest', code, completed=imports.check())


def run_pytest(arguments, imports):
    # Import the installed runner before adding the repository to sys.path.
    import pytest
    recorder = Recorder()

    class Plugin:
        def __init__(self):
            self.finished = False
            self.collected = set()

        def pytest_collection_finish(self, session):
            self.collected = {item.nodeid for item in session.items}
            for item in session.items:
                imports.test_file(item.path, item.nodeid)
                module_name = getattr(getattr(item, 'obj', None), '__module__', None)
                if module_name:
                    imports.test_module(module_name, item.nodeid)
            imports.check()

        def pytest_runtest_logstart(self, nodeid, location):
            recorder.start(nodeid)

        def pytest_runtest_logreport(self, report):
            if report.failed:
                recorder.outcome(report.nodeid, 'failed')
            elif report.skipped:
                recorder.outcome(report.nodeid, 'skipped')
            elif report.when == 'call' and report.passed:
                recorder.outcome(report.nodeid, 'passed')

        def pytest_runtest_logfinish(self, nodeid, location):
            recorder.stop(nodeid)

        def pytest_sessionfinish(self, session, exitstatus):
            self.finished = True

    plugin = Plugin()
    sys.path.insert(0, os.getcwd())
    code = int(pytest.main(arguments, plugins=[plugin]))
    complete = imports.check() and plugin.finished and plugin.collected == set(recorder.tests)
    return code, recorder.report('pytest', code, completed=complete)


def main():
    # Isolated mode ignores PYTHONIOENCODING; align Windows pipes with the
    # parent's UTF-8 decoder so provenance failures remain actionable.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8', errors='replace')
    if len(sys.argv) < 3 or sys.argv[1] not in {'unittest', 'pytest'}:
        return 2
    framework, report_path = sys.argv[1:3]
    path = Path(report_path)
    # Capture reporting primitives before repository imports. os._exit still
    # bypasses this completion write, intentionally yielding no valid report.
    dumps, open_file, replace = json.dumps, open, os.replace
    policy = json.loads(Path(__file__).with_name('policy.json').read_text(encoding='utf-8'))
    imports = SnapshotImports(policy)
    imports.check()
    code, report = (run_unittest(sys.argv[3:], imports) if framework == 'unittest'
                    else run_pytest(sys.argv[3:], imports))
    if imports.errors:
        code = 2
        report.update(completed=False, exit_code=code, integrity_errors=sorted(imports.errors)[:20])
        print('TRACEFORGE_SNAPSHOT_IMPORT_ERROR: 无法确认测试只使用获批快照。'
              '请使用快照相对路径（src 项目可配置 pythonpath = src），'
              '并在独立环境中安装第三方依赖，移除指向原仓库的 editable 安装。', file=sys.stderr)
        for message in sorted(imports.errors)[:20]:
            print(message, file=sys.stderr)
    encoded = dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(encoded) > REPORT_LIMIT:
        return 3
    temporary = path.with_suffix('.partial')
    with open_file(temporary, 'xb') as stream:
        stream.write(encoded)
        stream.flush()
    replace(temporary, path)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
