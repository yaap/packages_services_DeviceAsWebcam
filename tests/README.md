# Device As Webcam CTSVerifier Test

## Setup

### Device Setup

Connect the device over USB to a host machine, and grant permissions for the
host to access the device over adb.

Update the `config.yml` file in `packages/services/DeviceAsWebcam/tests` with
the device ID of the device under test. Ex:

```yaml
TestBeds:
  # A test bed where adb will find Android devices.
  - Name: WebcamTestBed
    Controllers:
      AndroidDevice:
        - serial: 15081FDEE00053 # Update to match ID of device
          label: dut
```

### Host Machine Setup

> **Warning:** The Windows and macOS tests are deprecated and will be removed in
> a future release. It is highly recommended to run the script on Linux to
> minimize any potential compatibility issues. If the test fails on Windows or
> macOS, please try running on a Linux system before filing a bug or
> requesting an exception.

Ensure Python version 3.10 or newer, adb and Mobly are installed.

-   Python 3.10+: https://www.python.org/downloads/
-   adb: https://developer.android.com/studio/releases/platform-tools
-   Mobly:

    ```bash
    $ pip install mobly
    ```

#### Linux Setup

```bash
$ pip install git+https://github.com/aspotton/python3-v4l2.git
```

#### Mac Setup (deprecated)

No Mac specific setup required.

#### Windows Setup (deprecated)

```bash
$ pip install opencv-python
$ pip install windows-capture-device-list
```

### Scene Setup

Point the device at a well lit scene to ensure stable FPS for testing.
Low-light conditions may result in variable FPS rates and cause the test to
fail.

## Run Test

The test script can be found at `packages/services/DeviceAsWebcam/tests` and
ran as follows:

```bash
$ python run_webcam_test.py -c config.yml
```

During the test run, the webcam service activity will open to preview the webcam
stream. Upon completion of the test script, the CTSVerifier test will prompt for
confirmation regarding the quality of the frames that were previewed. Confirm
either "Yes" or "No". The script's results will be automatically sent to the
CTSVerifier test. To pass the test, both the automated test results and the
manual frame check must pass.
