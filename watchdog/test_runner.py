from __future__ import annotations

import sys
import time
import unittest
from dataclasses import dataclass
from typing import Any, Optional

from .ansi import fmt_fail, fmt_ok, fmt_skip


@dataclass
class TestCounts:
    run: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0


class CheckmarkTestResult(unittest.TextTestResult):
    """A minimal, readable unittest result with colored checkmarks.

    - Prints one line per test.
    - Prints full tracebacks only for failures/errors.
    """

    def __init__(self, *args: Any, verbose: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._verbose = bool(verbose)
        self._counts = TestCounts()
        self._t0 = time.time()

    def getDescription(self, test: unittest.case.TestCase) -> str:  # noqa: N802 (unittest API)
        # Prefer explicit shortDescription() if present.
        desc = test.shortDescription()
        if desc:
            return desc
        return str(test)

    def startTest(self, test: unittest.case.TestCase) -> None:  # noqa: N802
        super().startTest(test)
        self._counts.run += 1

    def addSuccess(self, test: unittest.case.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self.stream.writeln(f"{fmt_ok('✓')} {self.getDescription(test)}")

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._counts.skipped += 1
        # Always print skips; include reason only when verbose to keep output tight.
        if self._verbose:
            self.stream.writeln(f"{fmt_skip('↷')} {self.getDescription(test)}  ({reason})")
        else:
            self.stream.writeln(f"{fmt_skip('↷')} {self.getDescription(test)}")

    def addFailure(self, test: unittest.case.TestCase, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._counts.failures += 1
        self.stream.writeln(f"{fmt_fail('✗')} {self.getDescription(test)}")

    def addError(self, test: unittest.case.TestCase, err: Any) -> None:  # noqa: N802
        super().addError(test, err)
        self._counts.errors += 1
        self.stream.writeln(f"{fmt_fail('✗')} {self.getDescription(test)}")

    def summary_line(self) -> str:
        dt = time.time() - self._t0
        parts = [f"{self._counts.run} run"]
        if self._counts.failures:
            parts.append(f"{self._counts.failures} failures")
        if self._counts.errors:
            parts.append(f"{self._counts.errors} errors")
        if self._counts.skipped:
            parts.append(f"{self._counts.skipped} skipped")
        parts.append(f"{dt:.2f}s")
        return ", ".join(parts)


class CheckmarkTestRunner(unittest.TextTestRunner):
    resultclass = CheckmarkTestResult

    def __init__(self, *, verbose: bool = False, stream=None) -> None:
        # verbosity=0 suppresses the default "test ... ok" lines from TextTestRunner.
        super().__init__(verbosity=0, stream=stream)
        self._verbose = bool(verbose)

    def _makeResult(self) -> CheckmarkTestResult:  # noqa: N802 (unittest API)
        return self.resultclass(
            self.stream,
            self.descriptions,
            self.verbosity,
            verbose=self._verbose,
        )

    def run(self, test: unittest.suite.TestSuite) -> CheckmarkTestResult:  # type: ignore[override]
        # Custom runner to avoid the default unittest "Ran X tests" and "OK" noise.
        result: CheckmarkTestResult = self._makeResult()
        result.startTestRun()
        try:
            test(result)
        finally:
            result.stopTestRun()

        # Only print tracebacks if something failed.
        if not result.wasSuccessful():
            result.printErrors()
            self.stream.writeln(result.summary_line())
        elif self._verbose:
            self.stream.writeln(result.summary_line())
        return result


def run_unittest_suite(suite: unittest.TestSuite, *, verbose: bool = False) -> int:
    runner = CheckmarkTestRunner(verbose=verbose, stream=sys.stdout)
    res = runner.run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    import os
    verbose = "-v" in sys.argv
    # Discover tests in the watchdog/tests directory
    start_dir = os.path.join(os.path.dirname(__file__), "tests")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir, pattern="test_*.py", top_level_dir=os.path.dirname(os.path.dirname(__file__)))
    sys.exit(run_unittest_suite(suite, verbose=verbose))
