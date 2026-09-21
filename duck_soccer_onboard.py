#!/usr/bin/env python3
"""
Microduck Onboard Autonomous Soccer Script (Standalone Real-Robot Execution)

Runs directly on the robot's onboard RK3566 Linux SBC.

Camera path:
- Preferred: official mediad local snapshot API (/run/mediad/media.sock, media.frame).
  The response is one JSON-RPC header followed by exactly `bytes` raw UYVY bytes.
- Fallback/legacy: V4L2 /dev/video0, selectable explicitly.

Motion path:
- robotd JSON-RPC over /run/robotd.sock.
- The soccer controller only consumes image detections + monotonic time; simulator
  world coordinates are never used here.
"""

import argparse
import json
import os
import socket
import time

import cv2
import numpy as np


ROBOT_SOCKET = "/run/robotd.sock"
MEDIA_SOCKET = "/run/mediad/media.sock"
MAX_FRAME_BYTES = 16 * 1024 * 1024


class DuckClient:
    """Client for communicating with the onboard robotd daemon via Unix socket."""

    def __init__(self, sock_path=ROBOT_SOCKET):
        self.sock_path = sock_path
        self.sock = None
        self._msg_id = 1
        self.connect()

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(self.sock_path)
            self.sock.settimeout(1.0)
            print(f"[DuckClient] Connected to {self.sock_path}")
        except Exception as exc:
            print(f"[DuckClient] Error connecting to {self.sock_path}: {exc}")
            self.sock = None

    def notify(self, method, params=None):
        """Send a JSON-RPC notification; robot.move uses this high-rate path."""
        if self.sock is None:
            self.connect()
            if self.sock is None:
                return
        req = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        try:
            self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        except Exception as exc:
            print(f"[DuckClient] Notify send error: {exc}")
            self.sock = None

    def request(self, method, params=None):
        """Send a JSON-RPC request and read its single-line JSON response."""
        if self.sock is None:
            self.connect()
            if self.sock is None:
                return None

        current_id = self._msg_id
        self._msg_id += 1
        req = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": current_id,
        }
        try:
            self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
            raw_resp = b""
            while not raw_resp.endswith(b"\n"):
                chunk = self.sock.recv(1024)
                if not chunk:
                    break
                raw_resp += chunk
            if raw_resp:
                return json.loads(raw_resp.decode("utf-8").strip())
        except Exception as exc:
            print(f"[DuckClient] Request error: {exc}")
            self.sock = None
        return None

    def move(self, vx=0.0, vy=0.0, vyaw=0.0):
        """Velocity intent: vx/vy in m/s, vyaw in rad/s (+ = left)."""
        self.notify(
            "robot.move",
            {"vx": float(vx), "vy": float(vy), "vyaw": float(vyaw)},
        )

    def stop(self):
        self.request("robot.stop")

    def kick(self, foot="right"):
        skill_name = "kick_right" if foot == "right" else "kick_left"
        return self.request("robot.do", {"skill": skill_name})

    def quack(self, tag="chirp"):
        self.notify("robot.sound", {"tag": tag})


def _rotate_clockwise(frame, degrees):
    degrees = int(degrees) % 360
    if degrees == 0:
        return frame
    if degrees == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise ValueError(f"Unsupported camera rotation: {degrees}")


def _fit_for_vision(frame, max_width=320, max_height=240):
    """Downscale without changing aspect ratio; never upscale."""
    height, width = frame.shape[:2]
    scale = min(max_width / width, max_height / height, 1.0)
    if scale >= 0.999:
        return frame
    out_width = max(1, int(round(width * scale)))
    out_height = max(1, int(round(height * scale)))
    return cv2.resize(frame, (out_width, out_height), interpolation=cv2.INTER_AREA)


class MediaFrameCamera:
    """Read fresh UYVY frames from mediad's official local media.frame endpoint."""

    def __init__(
        self,
        sock_path=MEDIA_SOCKET,
        max_width=320,
        max_height=240,
        timeout=3.0,
    ):
        self.sock_path = sock_path
        self.max_width = max_width
        self.max_height = max_height
        self.timeout = timeout
        self._msg_id = 1
        self.last_error = None

    @staticmethod
    def _read_exact(stream, byte_count):
        data = bytearray(byte_count)
        view = memoryview(data)
        offset = 0
        while offset < byte_count:
            read = stream.readinto(view[offset:])
            if not read:
                raise EOFError(
                    f"media.frame ended after {offset} of {byte_count} bytes"
                )
            offset += read
        return data

    def read(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.sock_path)
            request_id = self._msg_id
            self._msg_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "media.frame",
                "params": {},
            }
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))

            with sock.makefile("rb") as stream:
                header_line = stream.readline(4097)
                if not header_line:
                    raise RuntimeError("mediad closed without a media.frame header")
                if len(header_line) > 4096 or not header_line.endswith(b"\n"):
                    raise RuntimeError("invalid/oversized media.frame JSON header")

                response = json.loads(header_line.decode("utf-8"))
                if response.get("id") != request_id:
                    raise RuntimeError(
                        f"media.frame reply id mismatch: {response.get('id')} != {request_id}"
                    )
                if "error" in response:
                    raise RuntimeError(f"media.frame failed: {response['error']}")

                meta = response.get("result") or {}
                width = int(meta["width"])
                height = int(meta["height"])
                byte_count = int(meta["bytes"])
                pixel_format = str(meta["format"]).upper()
                rotate = int(meta.get("rotate", 0))

                if width <= 0 or height <= 0:
                    raise RuntimeError(
                        f"invalid media.frame geometry: {width}x{height}"
                    )
                if pixel_format != "UYVY":
                    raise RuntimeError(
                        f"unsupported media.frame format {pixel_format!r}; expected UYVY"
                    )

                expected = width * height * 2
                if byte_count != expected:
                    raise RuntimeError(
                        f"unexpected UYVY size: header={byte_count}, expected={expected}"
                    )
                if byte_count > MAX_FRAME_BYTES:
                    raise RuntimeError(
                        f"media.frame payload too large: {byte_count} bytes"
                    )

                raw = self._read_exact(stream, byte_count)

            uyvy = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 2)
            frame = cv2.cvtColor(uyvy, cv2.COLOR_YUV2BGR_UYVY)
            frame = _rotate_clockwise(frame, rotate)
            frame = _fit_for_vision(frame, self.max_width, self.max_height)
            self.last_error = None
            return True, frame

        except Exception as exc:
            self.last_error = exc
            return False, None
        finally:
            sock.close()

    def release(self):
        pass


class V4L2Camera:
    """Legacy camera path. Use only when mediad does not own the device."""

    def __init__(self, device=0, width=320, height=240):
        self.cap = cv2.VideoCapture(device)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.width = width
        self.height = height
        self.last_error = None

    def is_opened(self):
        return self.cap.isOpened()

    def read(self):
        ret, frame = self.cap.read()
        if not ret:
            self.last_error = RuntimeError("V4L2 camera read failed")
            return False, None
        frame = _fit_for_vision(frame, self.width, self.height)
        self.last_error = None
        return True, frame

    def release(self):
        self.cap.release()


def open_camera(source, media_socket, v4l2_device, width=320, height=240):
    if source == "mediad":
        print(f"[Camera] Using mediad media.frame at {media_socket}")
        return MediaFrameCamera(media_socket, width, height)

    if source == "v4l2":
        camera = V4L2Camera(v4l2_device, width, height)
        if not camera.is_opened():
            raise RuntimeError(f"failed to open V4L2 camera {v4l2_device!r}")
        print(f"[Camera] Using legacy V4L2 device {v4l2_device!r}")
        return camera

    # auto: current firmware first. If the official socket does not exist, try
    # legacy V4L2. We intentionally do not fall back after a media.frame timeout:
    # if mediad owns /dev/video0, trying to open it would only fight the daemon.
    if os.path.exists(media_socket):
        print(f"[Camera] Auto-selected mediad media.frame at {media_socket}")
        return MediaFrameCamera(media_socket, width, height)

    camera = V4L2Camera(v4l2_device, width, height)
    if not camera.is_opened():
        raise RuntimeError(
            f"{media_socket} does not exist and V4L2 device {v4l2_device!r} "
            "could not be opened"
        )
    print(
        f"[Camera] {media_socket} not found; using legacy V4L2 device "
        f"{v4l2_device!r}"
    )
    return camera


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Microduck visual soccer directly on the robot"
    )
    parser.add_argument(
        "--camera-source",
        choices=("auto", "mediad", "v4l2"),
        default="auto",
        help="camera backend (default: current-firmware mediad when available)",
    )
    parser.add_argument(
        "--media-socket",
        default=MEDIA_SOCKET,
        help="mediad media.frame Unix socket",
    )
    parser.add_argument(
        "--v4l2-device",
        default=0,
        type=int,
        help="legacy OpenCV camera index",
    )
    parser.add_argument(
        "--loop-sleep",
        default=0.08,
        type=float,
        help="minimum delay between control iterations",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("🦆 Microduck Onboard Autonomous Soccer System ⚽")
    print("   robotd control + mediad media.frame camera")
    print("=" * 60)

    duck = DuckClient()
    camera = None
    try:
        camera = open_camera(
            args.camera_source,
            args.media_socket,
            args.v4l2_device,
            width=320,
            height=240,
        )

        # Probe once before constructing perception. Current Microduck camera
        # metadata may require a 90-degree turn, which swaps width/height.
        ret, first_frame = camera.read()
        if not ret:
            detail = getattr(camera, "last_error", None)
            raise RuntimeError(f"failed to obtain first camera frame: {detail}")

        cam_height, cam_width = first_frame.shape[:2]
        print(f"[Camera] Vision frame: {cam_width}x{cam_height} BGR")

        from microduck_soccer.perception import BallDetector, GoalDetector
        from microduck_soccer.control import SoccerStateMachine

        # No simulator state enters this controller. Camera FOV, HSV thresholds
        # and terminal blind-advance duration still need physical calibration.
        ball_detector = BallDetector(cam_width, cam_height, fovy_deg=90.0)
        goal_detector = GoalDetector(cam_width, cam_height, fovy_deg=90.0)
        controller = SoccerStateMachine(image_height=cam_height)

        previous_mode = None
        frame_failures = 0
        pending_frame = first_frame

        while True:
            if pending_frame is not None:
                frame = pending_frame
                pending_frame = None
                ret = True
            else:
                ret, frame = camera.read()

            if not ret:
                duck.stop()
                previous_mode = "stand"
                frame_failures += 1
                detail = getattr(camera, "last_error", None)
                print(
                    f"[Camera] Frame failure {frame_failures}/5"
                    + (f": {detail}" if detail else "")
                )
                if frame_failures >= 5:
                    raise RuntimeError("camera stream lost")
                controller = SoccerStateMachine(image_height=cam_height)
                time.sleep(0.05)
                continue

            frame_failures = 0
            if frame.shape[:2] != (cam_height, cam_width):
                raise RuntimeError(
                    "camera geometry changed while running: "
                    f"{frame.shape[1]}x{frame.shape[0]} != "
                    f"{cam_width}x{cam_height}"
                )

            ball = ball_detector.detect(frame)
            goal = goal_detector.detect(frame)
            state, vx, vy, vyaw, mode, trigger = controller.update(
                ball,
                goal,
                time.monotonic(),
            )

            if trigger:
                response = duck.kick("right")
                if response is None or "error" in response:
                    raise RuntimeError(f"Kick request failed: {response}")
                print("Right-foot kick requested; contact/goal not verified.")
            elif mode == "walk":
                duck.move(vx=vx, vy=vy, vyaw=vyaw)
            elif mode == "stand" and previous_mode != "stand":
                duck.stop()

            previous_mode = mode
            time.sleep(max(0.0, args.loop_sleep))

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        duck.stop()
        if camera is not None:
            camera.release()
        if duck.sock is not None:
            duck.sock.close()


if __name__ == "__main__":
    main()
