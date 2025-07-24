# Copyright 2023 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Runs the webcam verification test and reports the result to CTSVerifier"""

import ast
import io
import logging
import platform
import subprocess
import time

from mobly import asserts
from mobly import base_test
from mobly import test_runner
from mobly.controllers import android_device


class DeviceAsWebcamTest(base_test.BaseTestClass):
    # Tests device as webcam functionality with Mobly base test class to run.

    _ACTION_WEBCAM_RESULT = (
        'com.android.cts.verifier.camera.webcam.ACTION_WEBCAM_RESULT'
    )
    _WEBCAM_RESULTS = 'camera.webcam.extra.RESULTS'
    _WEBCAM_TEST_ACTIVITY = (
        'com.android.cts.verifier/.camera.webcam.WebcamTestActivity'
    )
    # TODO(373791776): Find a way to discover PreviewActivity for vendors that
    # change the webcam service.
    # pylint: disable-next=line-too-long
    _DAC_PREVIEW_ACTIVITY = 'com.android.DeviceAsWebcam/com.android.deviceaswebcam.DeviceAsWebcamPreview'
    _ACTIVITY_START_WAIT = 1.5  # seconds
    _ADB_RESTART_WAIT = 9  # seconds
    _FPS_TOLERANCE = 0.15  # 15 percent
    _RESULT_PASS = 'PASS'
    _RESULT_FAIL = 'FAIL'
    _RESULT_NOT_EXECUTED = 'NOT_EXECUTED'
    _MANUAL_FRAME_CHECK_DURATION = 8  # seconds
    _WINDOWS_OS = 'Windows'
    _MAC_OS = 'Darwin'
    _LINUX_OS = 'Linux'

    def run_os_specific_test(self):
        """Runs the os specific webcam test script.

        Returns:
          A result list of tuples (tested_fps, actual_fps)
        """
        results = []
        current_os = platform.system()

        if current_os == self._WINDOWS_OS:
            logging.info('Starting test on Windows')
            # Due to compatibility issues directly running the windows
            # main function, the results from the windows_webcam_test script
            # are printed to the stdout and retrieved
            output = subprocess.check_output(
                ['python', 'windows_webcam_test.py']
            )
            output_str = output.decode('utf-8')
            results = ast.literal_eval(output_str.strip())
        elif current_os == self._LINUX_OS:
            # pylint: disable-next=import-outside-toplevel
            import linux_webcam_test

            logging.info('Starting test on Linux')
            results = linux_webcam_test.main()
        elif current_os == self._MAC_OS:
            # pylint: disable-next=import-outside-toplevel
            import mac_webcam_test

            logging.info('Starting test on Mac')
            results = mac_webcam_test.main()
        else:
            logging.info('Running on an unknown OS')

        return results

    def validate_fps(self, results):
        """Verifies the webcam FPS falls within the acceptable range of the

        tested FPS.

        Args:
            results: A result list of tuples (tested_fps, actual_fps)

        Returns:
            True if all FPS are within tolerance range, False otherwise
        """
        result = True

        for elem in results:
            tested_fps = elem[0]
            actual_fps = elem[1]

            max_diff = tested_fps * self._FPS_TOLERANCE

            if abs(tested_fps - actual_fps) > max_diff:
                logging.error(
                    'FPS is out of tolerance range!  Tested: %d Actual FPS: %d',
                    tested_fps,
                    actual_fps,
                )
                result = False

        return result

    def setup_class(self):
        # Registering android_device controller module declares the test
        # dependencies on Android device hardware. By default, we expect at
        # least one object is created from this.
        devices = self.register_controller(android_device, min_number=1)
        self.dut = devices[0]
        self.dut.adb.root()

    def test_webcam(self):

        # Keep device on while testing since it requires a manual check on the
        # webcam frames
        # '7' is a combination of flags ORed together to keep the device on
        # in all cases
        self.dut.adb.shell(
            'settings put global stay_on_while_plugged_in 7'.split()
        )

        cmd = (
            f'am start {self._WEBCAM_TEST_ACTIVITY} --activity-brought-to-front'
        )
        self.dut.adb.shell(cmd.split())

        # Check if webcam feature is enabled
        dut_webcam_enabled = self.dut.adb.getprop('ro.usb.uvc.enabled')
        if 'true' == dut_webcam_enabled:
            logging.info('Webcam enabled, testing webcam')
        else:
            logging.info('Webcam not enabled, skipping webcam test')

            # Notify CTSVerifier test that the webcam test was skipped,
            # the test will be marked as PASSED for this case
            cmd = (
                f'am broadcast -a {self._ACTION_WEBCAM_RESULT} --es'
                f' {self._WEBCAM_RESULTS} {self._RESULT_NOT_EXECUTED}'
            )
            self.dut.adb.shell(cmd.split())
            return

        # Set USB preference option to webcam
        # 'handle_usb_disconnect' reinitializes any mobly specific services that
        # may have been disrupted by the disconnection.
        with self.dut.handle_usb_disconnect():
            # Set USB preference option to webcam
            try:
                self.dut.adb.shell('svc usb setFunctions uvc'.split())
            except android_device.adb.AdbError as e:
                # error code 255 may be returned because adb lost connection as
                # part of switching to UVC. Other error codes are unexpected.
                if e.ret_code != 255:
                    # unhandled exception. crash and burn
                    raise e
            finally:
                # adb disconnects when changing usb function and reconnects
                # after a while. Wait for device to come back. Will throw a
                # AdbTimeoutError exception if adb does not recover in
                # _ADB_RESTART_WAIT seconds.
                self.dut.adb.wait_for_device(
                    timeout=DeviceAsWebcamTest._ADB_RESTART_WAIT
                )

        # Check if device came back with uvc mode active.
        stderr = io.BytesIO()
        stdout = self.dut.adb.shell(
            'svc usb getFunctions'.split(), stderr=stderr
        )

        # For whatever reason, this call outputs to stderr instead of stdout
        # despite there being no error. This will likely change in the future.
        # For now, just check both stdout and stderr.
        stderr = stderr.getvalue().decode('utf-8')
        stdout = stdout.decode('utf-8')
        if 'uvc' not in stdout and 'uvc' not in stderr:
            logging.error('USB preference option to set webcam unsuccessful')

            # Notify CTSVerifier test that setting webcam option was
            # unsuccessful
            cmd = (
                f'am broadcast -a {self._ACTION_WEBCAM_RESULT} --es'
                f' {self._WEBCAM_RESULTS} {self._RESULT_FAIL}'
            )
            self.dut.adb.shell(cmd.split())
            return

        fps_results = self.run_os_specific_test()
        logging.info('FPS test results (Expected, Actual): %s', fps_results)
        result = self.validate_fps(fps_results)

        test_status = self._RESULT_PASS
        if not result or not fps_results:
            logging.error('FPS testing failed')
            test_status = self._RESULT_FAIL

        # Send result to CTSVerifier test
        time.sleep(self._ACTIVITY_START_WAIT)
        cmd = (
            f'am broadcast -a {self._ACTION_WEBCAM_RESULT} --es'
            f' {self._WEBCAM_RESULTS} {test_status}'
        )
        self.dut.adb.shell(cmd.split())

        # Enable the webcam service preview activity for a manual
        # check on webcam frames
        cmd = f'am start {self._DAC_PREVIEW_ACTIVITY} --activity-no-history'
        self.dut.adb.shell(cmd.split())
        time.sleep(self._MANUAL_FRAME_CHECK_DURATION)

        cmd = (
            f'am start {self._WEBCAM_TEST_ACTIVITY} --activity-brought-to-front'
        )
        self.dut.adb.shell(cmd.split())

        asserts.assert_true(test_status == self._RESULT_PASS, 'Results: Failed')

        self.dut.adb.shell(
            'settings put global stay_on_while_plugged_in 0'.split()
        )


if __name__ == '__main__':
    test_runner.main()
