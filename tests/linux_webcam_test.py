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
"""Verifies advertised FPS from device as webcam."""

import argparse
from dataclasses import dataclass
import errno
import fcntl
import glob
import logging
import mmap
import os
import subprocess
import time

import v4l2

_TEST_DURATION_SECONDS = 10
_WAIT_MS = 10000  # 10 seconds
_REQUEST_BUFFER_COUNT = 10
_VIDEO_DEVICES_PATH = '/dev/video*'
_UNKNOWN_NAME = '__UNKNOWN_NAME__'


@dataclass
class DeviceInfo:
    # Advertised name of the V4L2 node. Ex. "Android Webcam"
    name: str
    # Serial of the USB device that mounted the V4L2 node. This is the same
    # serial configured via config.yml
    serial: str
    # V4L2 node mounted by this device. Nominally, this is /dev/videoXX.
    v4l2_node: str


def v4l2_fourcc_to_str(fourcc):
    return ''.join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])


def _get_device_info_for_v4l2_node(node: str) -> DeviceInfo | None:
    """Returns DeviceInfo associated with the passed node.

    Takes a v4l2 node like '/dev/video1' and returns the USB serial
    associated with it. For USB devices like Android Phones, this is the same
    serial that is used by adb.

    Returns:
      DeviceInfo if the serial number of the device that mounted a v4l2 node is
      found, or
      None if the serial number is missing

    Args:
      node: str; Path to the V4L2 node, for example: /dev/video11
    """
    try:
        # Use udevadm to get data associated with the V4L2 node.
        cmd = 'udevadm info --query=property -n /dev/video0'.split()
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,  # Raises an exception for non-zero exit codes
        )

        device_info: dict[str, str] = {}

        # Parse the output line by line
        for line in process.stdout.strip().split('\n'):
            line = line.strip()
            if '=' in line:
                key, value = line.split(
                    '=', 1
                )  # split at most once in case the value contains =
                if key == 'ID_V4L_PRODUCT':
                    # The product/model name for V4L2 devices
                    device_info['name'] = value
                elif key == 'ID_SERIAL_SHORT':
                    # ID_SERIAL_SHORT is the "unique" part of the USB device's
                    # serial.
                    device_info['serial'] = value

        if 'serial' not in device_info:
            logging.info('%s does not have an short serial.', node)
            return None

        if 'name' not in device_info:
            logging.info(
                '%s does not have an associated name. Proceeding with %s.',
                node,
                _UNKNOWN_NAME,
            )
            device_info['name'] = _UNKNOWN_NAME

        return DeviceInfo(device_info['name'], device_info['serial'], node)

    except subprocess.CalledProcessError as e:
        logging.error('Error executing udevadm for %s: %s', node, e.stderr)
        return None


def _find_v4l2_node_for_serial(dut_serial: str) -> DeviceInfo | None:
    """Looks for V4L2 node mounted by a USB device with the given serial number.

    Returns:
      DeviceInfo of the V4L2 node mounted by dut (as matched by dut_serial), or
      None if no such node is found.

    Args:
      dut_serial: str; Serial of the device under test that mounts the V4L2 node
        to be tested.
    """
    v4l2_nodes: list[str] = glob.glob(_VIDEO_DEVICES_PATH)
    for node in v4l2_nodes:
        fd: int | None = None
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)

            caps = v4l2.v4l2_capability()
            ioctl_retry_error(
                fd, v4l2.VIDIOC_QUERYCAP, caps, OSError, errno.EBUSY
            )

            if not caps.capabilities & v4l2.V4L2_CAP_VIDEO_CAPTURE:
                # webcam must support video capture capability
                continue

            # Devices can mount multiple nodes at /dev/video*
            # Check for one that is used for capturing by checking
            # if formats can be retrieved from it
            try:
                fmtdesc = v4l2.v4l2_fmtdesc()
                fmtdesc.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                ioctl_retry_error(
                    fd, v4l2.VIDIOC_ENUM_FMT, fmtdesc, OSError, errno.EBUSY
                )
            except OSError:
                # Can't enumerate formats. Not an error, but we can't test with
                # this. Looks for other nodes
                continue

            device_info = _get_device_info_for_v4l2_node(node)
            if device_info is None:
                # Could not get device info for the node. Likely to not be
                # an Android Device. The actual reason for missing device info
                # is logged in _get_device_info_for_v4l2_node
                continue

            if device_info.serial == dut_serial:
                logging.info(
                    "Found '%s' at '%s' mounted by device with serial '%s'",
                    device_info.name,
                    device_info.v4l2_node,
                    dut_serial,
                )
                return device_info

        except OSError as e:
            logging.info(
                "Error while opening %s. Error: '%s'", node, e.strerror
            )
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    # Failed to close FD after open was successful. Can't do
                    # much, so just ignore.
                    logging.warning(
                        "Failed to close open fd (%d) for node '%s'", fd, node
                    )

    logging.error(
        'Could not find a V4L2 node belonging to device with serial %s.',
        dut_serial,
    )
    return None


def initialize_formats_and_resolutions(video_device):
    """Gets a list of the supported formats, resolutions and frame rates.

    Args:
      video_device: Device to be checked

    Returns:
      List of formats, resolutions, and frame rates:
        [ (Format (fmtdesc), [ (Resolution (frmsize),
            [ FrameRates (v4l2_frmivalenum) ]) ]) ]
    """
    # [(Format (fmtdesc),
    #     [(Resolution(frmsize),
    #         [FrameRates(v4l2_frmivalenum)])])]
    formats_and_resolutions = []

    # Retrieve supported formats
    format_index = 0
    while True:
        try:
            fmtdesc = v4l2.v4l2_fmtdesc()
            fmtdesc.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
            fmtdesc.index = format_index
            ioctl_retry_error(
                video_device,
                v4l2.VIDIOC_ENUM_FMT,
                fmtdesc,
                OSError,
                errno.EBUSY,
            )
        except OSError:
            break
        else:
            formats_and_resolutions.append((fmtdesc, []))
            format_index += 1

    # Use the found formats to retrieve the supported
    # resolutions per format
    for index, elem in enumerate(formats_and_resolutions):
        fmtdesc = elem[0]
        frmsize_index = 0

        while True:
            try:
                frmsize = v4l2.v4l2_frmsizeenum()
                frmsize.pixel_format = fmtdesc.pixelformat
                frmsize.index = frmsize_index
                ioctl_retry_error(
                    video_device,
                    v4l2.VIDIOC_ENUM_FRAMESIZES,
                    frmsize,
                    OSError,
                    errno.EBUSY,
                )
            except OSError:
                break
            else:
                if frmsize.type == v4l2.V4L2_FRMSIZE_TYPE_DISCRETE:
                    formats_and_resolutions[index][1].append((frmsize, []))
                frmsize_index += 1

    # Get advertised frame rates supported per format and resolution
    for format_index, elem in enumerate(formats_and_resolutions):
        fmtdesc = elem[0]
        frmsize_list = elem[1]

        for frmsize_index, frmsize_elem in enumerate(frmsize_list):
            curr_frmsize = frmsize_elem[0]
            frmival_index = 0
            while True:
                try:
                    frmivalenum = v4l2.v4l2_frmivalenum()
                    frmivalenum.index = frmival_index
                    frmivalenum.pixel_format = fmtdesc.pixelformat
                    frmivalenum.width = curr_frmsize.discrete.width
                    frmivalenum.height = curr_frmsize.discrete.height
                    ioctl_retry_error(
                        video_device,
                        v4l2.VIDIOC_ENUM_FRAMEINTERVALS,
                        frmivalenum,
                        OSError,
                        errno.EBUSY,
                    )
                except OSError:
                    break
                else:
                    formats_and_resolutions[format_index][1][frmsize_index][
                        1
                    ].append(frmivalenum)
                    frmival_index += 1

    return formats_and_resolutions


def print_formats_and_resolutions(formats_and_resolutions):
    """Helper function to print out device capabilities for debugging.

    Args:
      formats_and_resolutions: List to be printed
    """
    for elem in formats_and_resolutions:
        fmtdesc = elem[0]
        print(f"""Format - {fmtdesc.description},
        {fmtdesc.pixelformat} ({v4l2_fourcc_to_str(fmtdesc.pixelformat)})""")
        frmsize_list = elem[1]
        for frmsize_elem in frmsize_list:
            frmsize = frmsize_elem[0]
            print(
                '-Resolution:'
                f' {frmsize.discrete.width}x{frmsize.discrete.height}'
            )
            frmivalenum_list = frmsize_elem[1]
            for frmivalenum in frmivalenum_list:
                print(f"""\t{fmtdesc.description} ({fmtdesc.pixelformat}),
            {frmivalenum.discrete.denominator / frmivalenum.discrete.numerator}
            fps""")


def ioctl_retry_error(video_device, request, arg, error, errno_code):
    """Adds wait check for specified ioctl call.

    Args:
      video_device: the device the ioctl call will interface with
      request: request for the ioctl call
      arg: arguments for ioctl
      error: the error to be catched and waited on
      errno_code: errno code of error to be waited on
    """
    wait_time = _WAIT_MS
    while True:
        try:
            fcntl.ioctl(video_device, request, arg)
            break
        except error as e:
            # if the error is a blocking I/O error, wait a short time and try
            # again
            if e.errno == errno_code and wait_time >= 0:
                time.sleep(0.01)  # wait for 10 milliseconds
                wait_time -= 10
                continue
            else:
                raise  # otherwise, re-raise the exception


def setup_for_test_fps(video_device, formats_and_resolutions):
    """Sets up and calls fps test for device.

    Args:
      video_device: device to be tested
      formats_and_resolutions: device capabilities to be tested

    Returns:
      List of fps test results with expected fps and actual tested fps
        [ (Expected, Actual )]
    """
    res = []
    for elem in formats_and_resolutions:
        fmtdesc = elem[0]

        fmt = v4l2.v4l2_format()
        fmt.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
        fmt.fmt.pix.pixelformat = fmtdesc.pixelformat

        frmsize_list = elem[1]
        for frmsize_elem in frmsize_list:
            frmsize = frmsize_elem[0]
            fmt.fmt.pix.width = frmsize.discrete.width
            fmt.fmt.pix.height = frmsize.discrete.height

            ioctl_retry_error(
                video_device, v4l2.VIDIOC_S_FMT, fmt, OSError, errno.EBUSY
            )

            ioctl_retry_error(
                video_device, v4l2.VIDIOC_G_FMT, fmt, OSError, errno.EBUSY
            )

            frmivalenum_list = frmsize_elem[1]
            for frmivalenum_elem in frmivalenum_list:
                streamparm = v4l2.v4l2_streamparm()
                streamparm.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
                streamparm.parm.capture.timeperframe.numerator = (
                    frmivalenum_elem.discrete.numerator
                )
                streamparm.parm.capture.timeperframe.denominator = (
                    frmivalenum_elem.discrete.denominator
                )
                ioctl_retry_error(
                    video_device,
                    v4l2.VIDIOC_S_PARM,
                    streamparm,
                    OSError,
                    errno.EBUSY,
                )

                res.append((
                    frmivalenum_elem.discrete.denominator,
                    test_fps(
                        video_device, frmivalenum_elem.discrete.denominator
                    ),
                ))
    return res


def test_fps(video_device, fps):
    """Runs fps test.

    Args:
      video_device: device to be tested
      fps: fps being tested

    Returns:
      Actual fps achieved from device
    """
    # Request buffers
    req = v4l2.v4l2_requestbuffers()
    req.count = _REQUEST_BUFFER_COUNT
    req.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
    req.memory = v4l2.V4L2_MEMORY_MMAP

    ioctl_retry_error(
        video_device, v4l2.VIDIOC_REQBUFS, req, OSError, errno.EBUSY
    )

    buffers = []
    for i in range(req.count):
        buf = v4l2.v4l2_buffer()
        buf.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = v4l2.V4L2_MEMORY_MMAP
        buf.index = i

        ioctl_retry_error(
            video_device, v4l2.VIDIOC_QUERYBUF, buf, OSError, errno.EBUSY
        )

        buf.buffer = mmap.mmap(
            video_device,
            buf.length,
            mmap.PROT_READ,
            mmap.MAP_SHARED,
            offset=buf.m.offset,
        )
        buffers.append(buf)
        ioctl_retry_error(
            video_device, v4l2.VIDIOC_QBUF, buf, OSError, errno.EBUSY
        )

    # Stream on
    buf_type = v4l2.v4l2_buf_type(v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE)
    ioctl_retry_error(
        video_device, v4l2.VIDIOC_STREAMON, buf_type, OSError, errno.EBUSY
    )

    # Test FPS
    num_frames = fps * _TEST_DURATION_SECONDS
    start_time = time.time()

    for x in range(num_frames):
        buf = buffers[x % _REQUEST_BUFFER_COUNT]
        ioctl_retry_error(
            video_device,
            v4l2.VIDIOC_DQBUF,
            buf,
            BlockingIOError,
            errno.EWOULDBLOCK,
        )
        ioctl_retry_error(
            video_device, v4l2.VIDIOC_QBUF, buf, OSError, errno.EBUSY
        )

    end_time = time.time()
    elapsed_time = end_time - start_time
    fps_res = num_frames / elapsed_time

    # Stream off and clean up
    ioctl_retry_error(
        video_device, v4l2.VIDIOC_STREAMOFF, buf_type, OSError, errno.EBUSY
    )
    req.count = 0
    ioctl_retry_error(
        video_device, v4l2.VIDIOC_REQBUFS, req, OSError, errno.EBUSY
    )

    for buf in buffers:
        buf.buffer.close()

    return fps_res


def main(dut_serial: str):
    # Open the webcam device
    device_info = _find_v4l2_node_for_serial(dut_serial)
    if device_info is None:
        logging.error(
            'Could not find a V4L2 node that corresponds to a device with'
            " serial '%s'",
            dut_serial,
        )
        return []

    try:
        video_device = os.open(device_info.v4l2_node, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        logging.error(
            'Error: failed to open device %s: error %s',
            device_info.v4l2_node,
            e.strerror,
        )
        return []

    formats_and_resolutions = initialize_formats_and_resolutions(video_device)
    if not formats_and_resolutions:
        logging.error('Error retrieving formats and resolutions')
        return []

    res = setup_for_test_fps(video_device, formats_and_resolutions)

    os.close(video_device)

    return res


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=(
            'Runs the Webcam test on Linux. For reporting the result to'
            ' CTSVerifier, use run_webcam_test.py instead.'
        )
    )
    parser.add_argument(
        '-s',
        '--serial',
        type=str,
        default=os.getenv('ANDROID_SERIAL'),
        help=(
            'Serial number of the device being tested. Defaults to'
            ' ANDROID_SERIAL environment variable if not provided.'
        ),
    )
    args = parser.parse_args()

    if args.serial is None or args.serial == '':
        logging.error('No serial serial provided for the DUT.')
        logging.error(parser.format_help())
        exit(1)

    main(args.serial)
