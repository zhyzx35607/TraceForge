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


def run_unittest(arguments):
    recorder = Recorder()

    class Result(unittest.TextTestResult):
        def startTest(self, test):
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
    return code, recorder.report('unittest', code)


def run_pytest(arguments):
    # Import the installed runner before adding the repository to sys.path.
    import pytest
    recorder = Recorder()

    class Plugin:
        def __init__(self):
            self.finished = False
            self.collected = set()

        def pytest_collection_finish(self, session):
            self.collected = {item.nodeid for item in session.items}

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
    complete = plugin.finished and plugin.collected == set(recorder.tests)
    return code, recorder.report('pytest', code, completed=complete)


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in {'unittest', 'pytest'}:
        return 2
    framework, report_path = sys.argv[1:3]
    path = Path(report_path)
    # Capture reporting primitives before repository imports. os._exit still
    # bypasses this completion write, intentionally yielding no valid report.
    dumps, open_file, replace = json.dumps, open, os.replace
    code, report = (run_unittest(sys.argv[3:]) if framework == 'unittest' else run_pytest(sys.argv[3:]))
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
