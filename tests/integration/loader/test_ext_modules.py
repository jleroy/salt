import os
import time

import pytest

from tests.support.case import ModuleCase


@pytest.mark.windows_whitelisted
class LoaderOverridesTest(ModuleCase):
    @pytest.fixture(autouse=True)
    def configure_minion_cache(self, salt_minion_factory):
        self.minion_cachedir = salt_minion_factory.config["cachedir"]

    def setUp(self):
        self.run_function("saltutil.sync_modules")

    @pytest.mark.slow_test
    @pytest.mark.timeout_unless_on_windows(120)
    def test_overridden_internal(self):
        # To avoid a race condition on Windows, we need to make sure the
        # `override_test.py` file is present in the _modules directory before
        # trying to list all functions. This test may execute before the
        # minion has finished syncing down the files it needs.
        module = os.path.join(
            self.minion_cachedir,
            "files",
            "base",
            "_modules",
            "override_test.py",
        )
        deadline = time.monotonic() + 60
        while not os.path.isfile(module):
            if time.monotonic() >= deadline:
                self.fail(f"Module was not synchronized within 60 seconds: {module}")
            time.sleep(1)

        funcs = self.run_function("sys.list_functions")

        # We placed a test module under _modules.
        # The previous functions should also still exist.
        self.assertIn("test.ping", funcs)

        # A non existing function should, of course, not exist
        self.assertNotIn("brain.left_hemisphere", funcs)

        # There should be a new function for the test module, recho
        self.assertIn("test.recho", funcs)

        text = "foo bar baz quo qux"
        self.assertEqual(
            self.run_function("test.echo", arg=[text])[::-1],
            self.run_function("test.recho", arg=[text]),
        )
