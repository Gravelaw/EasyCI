"""Run the run-heavy EasyCI tests in parallel worker processes (spec criterion 18: under 60 s).

Every EasyCITest gets its own temporary EASYCI_HOME, queue lock, fake gh and repositories, so tests
in different processes cannot see each other. ParallelSuite splits those tests across workers,
each worker runs its share with this module as `python parallel.py ID...` and prints one JSON line
per test, and the parent replays the outcomes into the normal unittest result, so the usual
`python -m unittest discover -s tests` output, counts and exit code are unchanged.

Opt out with EASYCI_TEST_SERIAL=1. Integration tests (real WSL, Task Scheduler) always run serially
in the parent because they share one distro and the host's scheduler.
"""
import json
import os
import pathlib
import subprocess
import sys
import traceback
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MARK = '##easyci-test## '


def workers():
    wanted = os.environ.get('EASYCI_TEST_WORKERS')
    return max(1, int(wanted)) if wanted else max(1, min(8, (os.cpu_count() or 2) - 1))


class _Remote(Exception):
    """Carries a worker's formatted traceback into the parent's result."""


def _remote_exc(text):
    try:
        raise _Remote('\n' + text)
    except _Remote:
        return sys.exc_info()


class ParallelSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        tests = list(_flatten(self))
        if not tests:
            return result
        count = min(workers(), len(tests))
        shards = [tests[i::count] for i in range(count)]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(HERE), str(HERE.parents[1]),
                                                            os.environ.get('PYTHONPATH', '')])}
        procs = [subprocess.Popen([sys.executable, str(HERE / 'parallel.py'), *[t.id() for t in shard]],
                                  cwd=str(HERE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL) for shard in shards]
        by_id = {t.id(): t for t in tests}
        seen = set()
        for proc, shard in zip(procs, shards):
            try:
                out, _ = proc.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            text = out.decode('utf-8', errors='replace')
            for line in text.splitlines():
                if not line.startswith(MARK):
                    continue
                item = json.loads(line[len(MARK):])
                test = by_id[item['id']]
                seen.add(item['id'])
                result.startTest(test)
                if item['outcome'] == 'success':
                    result.addSuccess(test)
                elif item['outcome'] == 'skip':
                    result.addSkip(test, item['detail'])
                elif item['outcome'] == 'failure':
                    result.addFailure(test, _remote_exc(item['detail']))
                else:
                    result.addError(test, _remote_exc(item['detail']))
                result.stopTest(test)
            for test in shard:
                if test.id() not in seen:  # the worker died before reporting this test
                    result.startTest(test)
                    result.addError(test, _remote_exc(f'worker exited {proc.returncode} without a result:\n{text[-3000:]}'))
                    result.stopTest(test)
        return result


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


class _Reporter(unittest.TestResult):
    def _emit(self, test, outcome, detail=''):
        print(MARK + json.dumps({'id': test.id(), 'outcome': outcome, 'detail': detail}), flush=True)

    def addSuccess(self, test):
        super().addSuccess(test)
        if not getattr(test, '_easyci_failed', False):
            self._emit(test, 'success')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._emit(test, 'failure', self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._emit(test, 'error', self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._emit(test, 'skip', reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            test._easyci_failed = True
            kind = 'failure' if issubclass(err[0], test.failureException) else 'error'
            self._emit(test, kind, f'{subtest}\n' + self._exc_info_to_string(err, test))


def main(ids):
    sys.path[:0] = [str(HERE), str(HERE.parents[1])]
    suite = unittest.TestSuite()
    for test_id in ids:
        try:
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(test_id))
        except Exception:  # noqa: BLE001 - report the import failure as that test's error
            print(MARK + json.dumps({'id': test_id, 'outcome': 'error', 'detail': traceback.format_exc()}), flush=True)
    suite.run(_Reporter())


if __name__ == '__main__':
    main(sys.argv[1:])
