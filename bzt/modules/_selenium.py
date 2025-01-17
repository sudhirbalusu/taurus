"""
Copyright 2015 BlazeMeter Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import copy
import os
import time
from abc import abstractmethod

from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from urwid import Text, Pile
from requests.exceptions import ConnectionError

from bzt import TaurusConfigError, ToolError
from bzt.modules import ReportableExecutor
from bzt.modules.console import PrioritizedWidget
from bzt.utils import get_files_recursive, get_full_path, RequiredTool, is_windows


class AbstractSeleniumExecutor(ReportableExecutor):
    @abstractmethod
    def subscribe_to_transactions(self, listener):
        pass


class SeleniumExecutor(ReportableExecutor):
    """
    Selenium executor
    :type runner: bzt.modules.SubprocessedExecutor
    """

    SUPPORTED_RUNNERS = ["apiritif", "junit", "testng", "rspec", "mocha", "nunit", "xunit", "pytest", "wdio", "robot"]
    SELENIUM_TOOLS_DIR = "~/.bzt/selenium-taurus/tools"

    def __init__(self):
        super(SeleniumExecutor, self).__init__()
        self.end_time = None
        self.runner = None
        self.script = None
        self.runner_working_dir = None
        self.register_reader = True
        self.webdrivers = []

    def add_env(self, env):  # compatibility with taurus-cloud
        self.env.set(env)

    def get_runner_working_dir(self):
        if self.runner_working_dir is None:
            self.runner_working_dir = self.engine.create_artifact("classes", "")
        return self.runner_working_dir

    def subscribe_to_transactions(self, listener):
        self.runner.subscribe_to_transactions(listener)
        self.runner.set_source(self)

    def create_runner(self):
        runner_type = self.get_runner_type()
        self.runner = self.engine.instantiate_module(runner_type)
        self.runner.env = self.env
        self.runner.parameters = self.parameters
        self.runner.provisioning = self.provisioning
        self.runner.execution = copy.deepcopy(self.execution)
        self.runner.execution['files'] = self.execution.get('files', [], force_set=True)
        self.runner.execution['executor'] = runner_type
        self.runner.register_reader = self.register_reader

        self.runner.settings = copy.deepcopy(self.settings).merge(self.runner.settings)

        if runner_type == "apiritif":
            self.runner.execution["test-mode"] = "selenium"

    def get_virtual_display(self):
        pass  # for compatibility with taurus server

    def install_required_tools(self):
        self.webdrivers = [self._get_tool(ChromeDriver, tool_path=self.settings.get('chromedriver').get('path')),
                           self._get_tool(GeckoDriver, tool_path=self.settings.get('geckodriver').get('path'))]

        for tool in self.webdrivers:
            if not tool.check_if_installed():
                self.log.info("Installing %s...", tool.tool_name)
                tool.install()

    def prepare(self):
        super(SeleniumExecutor, self).prepare()
        self.install_required_tools()
        for driver in self.webdrivers:
            self.env.add_path({"PATH": driver.get_driver_dir()})

        self.create_runner()
        self.runner.prepare()
        self.script = self.runner.script

    def get_runner_type(self):
        runner = self.execution.get("runner")
        if runner:
            if runner in SeleniumExecutor.SUPPORTED_RUNNERS:
                self.log.debug("Using script type: %s", runner)
                return runner
            else:
                msg = "Runner '%s' is not supported. Supported runners: %s"
                raise TaurusConfigError(msg % (runner, SeleniumExecutor.SUPPORTED_RUNNERS))

        script_name = self.get_script_path()
        if script_name:
            return self.detect_script_type(script_name)
        else:
            if "requests" in self.get_scenario():
                return "apiritif"
            else:
                raise TaurusConfigError("You must specify either script or list of requests to run Selenium")

    def resource_files(self):
        self.create_runner()
        return self.runner.resource_files()

    def detect_script_type(self, script_name):
        if not os.path.exists(script_name):
            raise TaurusConfigError("Script '%s' doesn't exist" % script_name)

        file_types = set()

        # gather file extensions and choose script_type according to priority
        if os.path.isfile(script_name):  # regular file received
            file_types.add(os.path.splitext(script_name)[1].lower())
        else:  # dir received: check contained files
            for file_name in get_files_recursive(script_name):
                file_types.add(os.path.splitext(file_name)[1].lower())

        if '.java' in file_types or '.jar' in file_types:
            # todo: next detection logic is duplicated in TestNGTester - can we avoid it?
            script_dir = get_full_path(self.get_script_path(), step_up=1)
            if os.path.exists(os.path.join(script_dir, 'testng.xml')) or self.execution.get('testng-xml'):
                script_type = 'testng'
            else:
                script_type = 'junit'
        elif '.py' in file_types:
            script_type = 'apiritif'
        elif '.rb' in file_types:
            script_type = 'rspec'
        elif '.js' in file_types:
            script_type = 'mocha'
        elif '.dll' in file_types or '.exe' in file_types:
            script_type = 'nunit'
        else:
            if os.path.isfile(script_name):
                message = "Unsupported script type: %r" % script_name
            else:
                message = "Directory %r doesn't contain supported scripts" % script_name
            raise TaurusConfigError(message)

        self.log.debug("Detected script type: %s", script_type)

        return script_type

    def startup(self):
        """
        Start runner
        :return:
        """
        self.start_time = time.time()
        self.runner.startup()

    def check(self):
        """
        check if test completed
        :return:
        """
        if self.widget:
            self.widget.update()

        return self.runner.check()

    def report_test_duration(self):
        if self.start_time:
            self.end_time = time.time()
            self.log.debug("Selenium tests ran for %s seconds", self.end_time - self.start_time)

    def shutdown(self):
        """
        shutdown test_runner
        :return:
        """
        self.runner.shutdown()
        self.report_test_duration()

    def post_process(self):
        self.runner.post_process()

        if os.path.exists("geckodriver.log"):
            self.engine.existing_artifact("geckodriver.log", True)

        super(SeleniumExecutor, self).post_process()

    def has_results(self):
        return self.runner.has_results()

    def get_widget(self):
        if not self.widget:
            self.widget = SeleniumWidget(self.script, self.runner.stdout.name)
        return self.widget

    def get_error_diagnostics(self):
        diagnostics = []
        if self.runner:
            diagnostics.extend(self.runner.get_error_diagnostics())
        gecko_logs = ["geckodriver.log", os.path.join(self.engine.artifacts_dir, "geckodriver.log")]
        for possible_log in gecko_logs:
            if os.path.exists(possible_log):
                with open(possible_log) as fds:
                    diagnostics.append("Geckodriver log:\n" + fds.read())
        return diagnostics


class SeleniumWidget(Pile, PrioritizedWidget):
    def __init__(self, script, runner_output):
        widgets = []
        self.script_name = Text("Selenium: %s" % os.path.basename(script))
        self.summary_stats = Text("Delayed...")
        self.runner_output = runner_output
        widgets.append(self.script_name)
        widgets.append(self.summary_stats)
        super(SeleniumWidget, self).__init__(widgets)
        PrioritizedWidget.__init__(self, priority=10)

    def update(self):
        reader_summary = ''
        if self.runner_output is not None and os.path.exists(self.runner_output):
            with open(self.runner_output, "rt") as fds:
                lines = fds.readlines()
                if lines:
                    line = lines[-1]
                    if not line.endswith("\n") and len(lines) > 1:
                        line = lines[-2]
                    if line and "," in line:
                        reader_summary = line.split(",")[-1]

        if reader_summary:
            self.summary_stats.set_text(reader_summary)
        else:
            self.summary_stats.set_text('In progress...')

        self._invalidate()


class ChromeDriver(RequiredTool):

    def __init__(self, tool_path="", log=None, **kwargs):
        self.webdriver_manager = None
        self.log = log
        base_dir = get_full_path(SeleniumExecutor.SELENIUM_TOOLS_DIR)
        filename = 'chromedriver.exe' if is_windows() else 'chromedriver'
        if not tool_path:
            try:
                self.webdriver_manager = ChromeDriverManager(path=base_dir, print_first_line=False)
                tool_path = os.path.join(base_dir,
                                         'drivers/chromedriver',
                                         self.webdriver_manager.driver.get_os_type(),
                                         f'{self.webdriver_manager.driver.get_version()}',
                                         filename)
            except (ValueError, ConnectionError) as err:
                tool_path = os.path.join(base_dir, 'drivers/chromedriver', filename)
                self.log.warning(err)
        super().__init__(tool_path=tool_path, installable=False, mandatory=False, **kwargs)

    def get_driver_dir(self):
        return get_full_path(self.tool_path, step_up=1)

    def install(self):
        if self.webdriver_manager:
            self.log.info(f"Will install {self.tool_name} into {self.tool_path}")

            self.webdriver_manager.install()
        else:
            super().install()


class GeckoDriver(RequiredTool):

    def __init__(self, tool_path="", log=None, **kwargs):
        self.webdriver_manager = None
        self.log = log
        base_dir = get_full_path(SeleniumExecutor.SELENIUM_TOOLS_DIR)
        filename = 'geckodriver.exe' if is_windows() else 'geckodriver'
        if not tool_path:
            try:
                self.webdriver_manager = GeckoDriverManager(path=base_dir, print_first_line=False)
                tool_path = os.path.join(base_dir,
                                         'drivers/geckodriver',
                                         self.webdriver_manager.driver.get_os_type(),
                                         f'{self.webdriver_manager.driver.get_version()}',
                                         filename)
            except (ValueError, ConnectionError) as err:
                tool_path = os.path.join(base_dir, 'drivers/geckodriver', filename)
                self.log.warning(err)
        super().__init__(tool_path=tool_path, installable=False, mandatory=False, **kwargs)

    def get_driver_dir(self):
        return get_full_path(self.tool_path, step_up=1)

    def install(self):
        if self.webdriver_manager:
            self.log.info(f"Will install {self.tool_name} into {self.tool_path}")

            self.webdriver_manager.install()
        else:
            super().install()
