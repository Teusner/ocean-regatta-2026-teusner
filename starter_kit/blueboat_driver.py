#!/usr/bin/env python3
"""
Ocean Regatta
Official BlueBoat Communication Driver & Hardware Abstraction Layer (HAL)

This module handles:
- Connection to Gazebo Transport (gz.transport)
- Protobuf message serialization/deserialization for IMU, GPS, Ping2 (Sonar), and Semantic Buoys
- Safe motor commands with strict physical clamping [-50.0 N, +50.0 N]
- Normalized, clean Python dataclasses for observation data

NOTE FOR PARTICIPANTS:
During automated evaluation on the server, your local copy of this file is NOT used.
The evaluation container loads its own official, immutable version of this driver.
Put all your control and perception algorithms in 'student_controller.py'.
"""

import sys
import math
import time
import signal
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable

# Gazebo Transport binding resolution (Gazebo Jetty -> Harmonic -> Garden)
try:
    from gz.transport14 import Node
except ImportError:
    try:
        from gz.transport13 import Node
    except ImportError:
        try:
            from gz.transport12 import Node
        except ImportError:
            try:
                from gz.transport import Node
            except ImportError:
                raise ImportError(
                    "Gazebo Transport Python bindings not found.\n"
                    "Please install 'python3-gz-transport14' (Gazebo Jetty) or run within the provided Docker container."
                )

# Gazebo Protobuf message types resolution (Gazebo Jetty -> Harmonic -> Garden)
try:
    from gz.msgs11.double_pb2 import Double
    from gz.msgs11.imu_pb2 import IMU
    from gz.msgs11.navsat_pb2 import NavSat
    from gz.msgs11.laserscan_pb2 import LaserScan
    from gz.msgs11.pose_v_pb2 import Pose_V
except ImportError:
    try:
        from gz.msgs10.double_pb2 import Double
        from gz.msgs10.imu_pb2 import IMU
        from gz.msgs10.navsat_pb2 import NavSat
        from gz.msgs10.laserscan_pb2 import LaserScan
        from gz.msgs10.pose_v_pb2 import Pose_V
    except ImportError:
        try:
            from gz.msgs9.double_pb2 import Double
            from gz.msgs9.imu_pb2 import IMU
            from gz.msgs9.navsat_pb2 import NavSat
            from gz.msgs9.laserscan_pb2 import LaserScan
            from gz.msgs9.pose_v_pb2 import Pose_V
        except ImportError:
            from gz.msgs.double_pb2 import Double
            from gz.msgs.imu_pb2 import IMU
            from gz.msgs.navsat_pb2 import NavSat
            from gz.msgs.laserscan_pb2 import LaserScan
            from gz.msgs.pose_v_pb2 import Pose_V


@dataclass
class IMUData:
    """On-board Inertial Measurement Unit readings (50 Hz)."""
    yaw: float = 0.0              # USV heading in radians (-pi to +pi, 0 = East / X axis)
    yaw_deg: float = 0.0          # USV heading in degrees (-180 to +180)
    yaw_rate: float = 0.0         # Yaw angular velocity in rad/s (r)
    roll: float = 0.0             # Roll angle in radians (phi)
    pitch: float = 0.0            # Pitch angle in radians (theta)
    linear_accel_x: float = 0.0   # Longitudinal acceleration in m/s^2 (surge)
    linear_accel_y: float = 0.0   # Transverse acceleration in m/s^2 (sway)
    timestamp: float = 0.0


@dataclass
class GPSData:
    """On-board GNSS/GPS receiver data (5 Hz)."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    x: float = 0.0                # Local Cartesian X coordinate (meters, projected from origin 48.0 N, -4.5 E)
    y: float = 0.0                # Local Cartesian Y coordinate (meters)
    is_valid: bool = False
    timestamp: float = 0.0


@dataclass
class Ping2Data:
    """Ping2 single-beam echosounder data mounted at +90 deg (port / left side)."""
    distance: float = 999.0       # Distance measured to port obstacle (meters, 0.5m to 30.0m)
    is_valid: bool = False        # True if reading is recent (< 0.5s) and within valid range
    timestamp: float = 0.0


@dataclass
class BuoyData:
    """Buoy or marker detected by the semantic perception sensor."""
    name: str
    x: float                      # Relative X coordinate to BlueBoat (meters forward if > 0)
    y: float                      # Relative Y coordinate to BlueBoat (meters left if > 0, right if < 0)
    z: float                      # Relative Z coordinate (meters)
    distance: float               # Direct Euclidean range (meters)
    bearing: float                # Relative bearing to vessel heading (-pi to +pi)


@dataclass
class Observation:
    """Complete instantaneous state perceived by USV sensors."""
    imu: IMUData = field(default_factory=IMUData)
    gps: GPSData = field(default_factory=GPSData)
    ping2: Ping2Data = field(default_factory=Ping2Data)
    buoys: Dict[str, BuoyData] = field(default_factory=dict)
    time: float = 0.0


class BlueBoatDriver:
    """
    Hardware abstraction and simulation communication driver for the BlueBoat USV.
    Manages Gazebo Transport subscriptions, Protobuf frame decoding,
    and physical safety clamping of differential thruster commands.
    """
    MAX_THRUST = 50.0  # Maximum thrust per motor in Newtons
    MIN_THRUST = -50.0

    LAT_ORIGIN = 48.0
    LON_ORIGIN = -4.5
    METERS_PER_LAT = 111139.0

    def __init__(self):
        self._node = Node()
        self._running = True

        # Internal sensor buffers
        self._imu = IMUData()
        self._gps = GPSData()
        self._ping2 = Ping2Data()
        self._buoys: Dict[str, BuoyData] = {}

        # Longitude conversion factor based on origin latitude
        self._meters_per_lon = self.METERS_PER_LAT * math.cos(math.radians(self.LAT_ORIGIN))

        # Thruster command publishers
        self._pub_thrust_left = self._node.advertise("/blueboat/cmd_thrust_left", Double)
        self._pub_thrust_right = self._node.advertise("/blueboat/cmd_thrust_right", Double)

        # Gazebo topic subscriptions
        self._node.subscribe(IMU, "/blueboat/imu", self._on_imu)
        self._node.subscribe(NavSat, "/blueboat/gps", self._on_gps)
        self._node.subscribe(LaserScan, "/blueboat/ping2_port", self._on_ping2)
        self._node.subscribe(Pose_V, "/blueboat/detected_buoys", self._on_buoys)

        print("[BlueBoatDriver] Connected to Gazebo Transport topics (/blueboat/*).")

    # --- Internal Protobuf Callbacks ---

    def _on_imu(self, msg: IMU):
        q = msg.orientation
        siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        sinr_cosp = 2.0 * (q.w * q.x + q.y * q.z)
        cosr_cosp = 1.0 - 2.0 * (q.x * q.x + q.y * q.y)
        roll = math.atan2(sinr_cosp, cosr_cosp)

        sinp = 2.0 * (q.w * q.y - q.z * q.x)
        pitch = math.copysign(math.pi / 2.0, sinp) if abs(sinp) >= 1.0 else math.asin(sinp)

        self._imu = IMUData(
            yaw=yaw,
            yaw_deg=math.degrees(yaw),
            yaw_rate=msg.angular_velocity.z,
            roll=roll,
            pitch=pitch,
            linear_accel_x=msg.linear_acceleration.x,
            linear_accel_y=msg.linear_acceleration.y,
            timestamp=time.time()
        )

    def _on_gps(self, msg: NavSat):
        x = (msg.latitude - self.LAT_ORIGIN) * self.METERS_PER_LAT
        y = (msg.longitude - self.LON_ORIGIN) * self._meters_per_lon
        self._gps = GPSData(
            latitude=msg.latitude,
            longitude=msg.longitude,
            altitude=msg.altitude,
            x=x,
            y=y,
            is_valid=True,
            timestamp=time.time()
        )

    def _on_ping2(self, msg: LaserScan):
        now = time.time()
        if len(msg.ranges) > 0:
            val = float(msg.ranges[0])
            if 0.5 <= val <= 30.0:
                self._ping2 = Ping2Data(distance=val, is_valid=True, timestamp=now)
                return
        # Out-of-range measurement
        self._ping2 = Ping2Data(distance=999.0, is_valid=False, timestamp=now)

    def _on_buoys(self, msg: Pose_V):
        new_buoys = {}
        for p in msg.pose:
            bx = p.position.x
            by = p.position.y
            bz = p.position.z
            dist = math.hypot(bx, by)
            bearing = math.atan2(by, bx)
            new_buoys[p.name] = BuoyData(
                name=p.name,
                x=bx,
                y=by,
                z=bz,
                distance=dist,
                bearing=bearing
            )
        self._buoys = new_buoys

    # --- Student / Controller API ---

    def get_observation(self) -> Observation:
        """
        Returns the latest snapshot of all sensor readings.
        """
        now = time.time()
        ping_valid = self._ping2.is_valid and (now - self._ping2.timestamp < 0.5)
        ping_data = Ping2Data(
            distance=self._ping2.distance if ping_valid else 999.0,
            is_valid=ping_valid,
            timestamp=self._ping2.timestamp
        )

        return Observation(
            imu=self._imu,
            gps=self._gps,
            ping2=ping_data,
            buoys=dict(self._buoys),
            time=now
        )

    def set_thrust(self, left_thrust: float, right_thrust: float):
        """
        Commands differential thrust in Newtons.
        Values are strictly clamped between -50.0 N and +50.0 N.
        Any non-numeric value (NaN / Inf) is automatically neutralized to 0.0.
        """
        if math.isnan(left_thrust) or math.isinf(left_thrust):
            left_thrust = 0.0
        if math.isnan(right_thrust) or math.isinf(right_thrust):
            right_thrust = 0.0

        clamped_left = max(self.MIN_THRUST, min(self.MAX_THRUST, float(left_thrust)))
        clamped_right = max(self.MIN_THRUST, min(self.MAX_THRUST, float(right_thrust)))

        msg_l = Double()
        msg_l.data = clamped_left
        self._pub_thrust_left.publish(msg_l)

        msg_r = Double()
        msg_r.data = clamped_right
        self._pub_thrust_right.publish(msg_r)

    def stop(self):
        """Immediately neutralizes thrust on both motors."""
        self._running = False
        self.set_thrust(0.0, 0.0)
        print("[BlueBoatDriver] Emergency motor stop.")

    def run(self, step_callback: Callable[[Observation], None], rate_hz: float = 10.0):
        """
        Executes the main control loop at the specified frequency (default 10 Hz).
        Handles termination signals (SIGINT/SIGTERM) and clean shutdown automatically.

        :param step_callback: Function accepting a single Observation argument.
        :param rate_hz: Control loop update rate in Hz.
        """
        dt = 1.0 / rate_hz

        def handle_signal(sig, frame):
            print("\n[BlueBoatDriver] Shutdown signal received. Stopping vessel cleanly...")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        print(f"[BlueBoatDriver] Starting control loop ({rate_hz} Hz)...")

        while self._running:
            t_start = time.time()
            obs = self.get_observation()

            try:
                step_callback(obs)
            except Exception as e:
                print(f"[BlueBoatDriver] Error in student controller: {e}", file=sys.stderr)

            elapsed = time.time() - t_start
            sleep_duration = max(0.0, dt - elapsed)
            time.sleep(sleep_duration)
