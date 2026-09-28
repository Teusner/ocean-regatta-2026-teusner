#!/usr/bin/env python3
"""
Ocean Regatta
Starter Kit - Student Strategy Controller
Target: Blue Robotics BlueBoat Autonomous Surface Vessel (Differential Catamaran)

THIS IS WHERE YOU DEVELOP YOUR AUTONOMOUS NAVIGATION AND CONTROL ALGORITHMS.
This script uses the 'BlueBoatDriver' hardware abstraction layer which provides:
- Cleaned sensor observations (IMU, GPS, Ping2 Sonar, Semantic Buoys)
- Safe differential motor commands [-50.0 N, +50.0 N]
- A regulated 10 Hz control loop

REGATTA RULES & WAYPOINT CAPSULES:
1. CHANNEL GATES (3 Gates):
   - 3 pairs of buoys (red port, green starboard) drifting with sea currents.
   - Transparent capsules (2.0 m wide) are located at the exact center of each gate.
   - Pure green (100% points) awarded if the vessel passes within 0.50 m of the capsule center.

2. CARDINAL BUOY (IALA Maritime Convention):
   - No capsule! You must round the buoy on the legally mandated quadrant (e.g. North).
   - Passing on an illegal side yields 0 points for this obstacle.

3. PIER HARBOR ZONE (ENTRY GATE -> 5.0 M WALL TRACKING -> EXIT GATE):
   - Entry gate (2 buoys + center capsule) guides vessel into straight alignment.
   - Wall following via port Ping2 single-beam echosounder at 5.0 m setpoint distance.
   - Exit gate (2 buoys + center capsule) marks the end of pier tracking and the finish line!
"""

import math
from blueboat_driver import BlueBoatDriver, Observation


class StudentController:
    """
    Autonomous navigation controller for the Blue Robotics BlueBoat USV.
    Finite State Machine (FSM) managing all regatta phases.
    """
    STATE_GATES = "NAVIGATE_GATES"
    STATE_CARDINAL = "ROUND_CARDINAL"
    STATE_PIER_ENTRY = "ENTER_PIER_GATE"
    STATE_PIER_FOLLOW = "FOLLOW_PIER_WALL"
    STATE_FINISHED = "COURSE_FINISHED"

    def __init__(self, driver: BlueBoatDriver):
        self.driver = driver
        self.state = self.STATE_GATES

        # Phase 1: Channel Gates Navigation
        self.current_gate_idx = 1
        self.total_channel_gates = 3
        self.base_thrust = 32.0  # Cruising surge thrust in Newtons (max 50.0 N)

        # Phase 3 & 4: Pier Wall Following (PD Controller at 5.0 m setpoint)
        self.target_pier_distance = 5.0  # Setpoint wall distance in meters
        self.prev_distance_error = 0.0
        self.kp_pier = 18.0              # Proportional gain
        self.kd_pier = 8.5               # Derivative gain

        print("[StudentController] Initialized successfully. Ready for departure!")

    def step(self, obs: Observation):
        """
        Executed periodically at 10 Hz by BlueBoatDriver.
        """
        if self.state == self.STATE_GATES:
            self._step_channel_gates(obs)
        elif self.state == self.STATE_CARDINAL:
            self._step_cardinal(obs)
        elif self.state == self.STATE_PIER_ENTRY:
            self._step_pier_entry(obs)
        elif self.state == self.STATE_PIER_FOLLOW:
            self._step_pier_follow(obs)
        elif self.state == self.STATE_FINISHED:
            self.driver.set_thrust(0.0, 0.0)

    # =========================================================================
    # PHASE 1: Channel Gates (Center Capsule Precision Validation)
    # =========================================================================
    def _step_channel_gates(self, obs: Observation):
        port_name = f"gate_port_{self.current_gate_idx}"
        stbd_name = f"gate_starboard_{self.current_gate_idx}"

        target_angle = 0.0
        surge = self.base_thrust

        if port_name in obs.buoys and stbd_name in obs.buoys:
            p_buoy = obs.buoys[port_name]
            s_buoy = obs.buoys[stbd_name]

            # Aim for the exact midpoint between the two buoys (capsule center)
            mid_x = (p_buoy.x + s_buoy.x) * 0.5
            mid_y = (p_buoy.y + s_buoy.y) * 0.5
            target_angle = math.atan2(mid_y, mid_x)

            if math.hypot(mid_x, mid_y) < 2.2:
                print(f"[StudentController] Channel gate {self.current_gate_idx} cleared!")
                self.current_gate_idx += 1
                if self.current_gate_idx > self.total_channel_gates:
                    print("[StudentController] Channel completed -> Navigating towards CARDINAL buoy")
                    self.state = self.STATE_CARDINAL

        yaw_rate = obs.imu.yaw_rate
        steering = 15.0 * target_angle - 4.0 * yaw_rate
        self.driver.set_thrust(surge - steering, surge + steering)

    # =========================================================================
    # PHASE 2: Regulatory Cardinal Buoy Rounding (IALA Maritime Convention)
    # =========================================================================
    def _step_cardinal(self, obs: Observation):
        cardinal_buoy = None
        for name, b_data in obs.buoys.items():
            if "cardinal" in name:
                cardinal_buoy = b_data
                break

        if cardinal_buoy is not None:
            # Round on the legally mandated quadrant with safety margin
            offset_y = 4.0
            if "south" in cardinal_buoy.name:
                offset_y = -4.0

            target_angle = math.atan2(cardinal_buoy.y + offset_y, cardinal_buoy.x)
            surge = self.base_thrust * 0.9
            yaw_rate = obs.imu.yaw_rate
            steering = 12.0 * target_angle - 3.5 * yaw_rate
            self.driver.set_thrust(surge - steering, surge + steering)

            # Once the buoy is well cleared behind the vessel, transition to pier entry gate
            if cardinal_buoy.x < -1.0 or cardinal_buoy.distance < 3.5:
                print("[StudentController] Cardinal buoy rounded -> Heading for PIER ENTRY GATE")
                self.state = self.STATE_PIER_ENTRY
        else:
            self.driver.set_thrust(self.base_thrust, self.base_thrust)

    # =========================================================================
    # PHASE 3: Pier Harbor Entry Gate (Alignment & Sonar Lock)
    # =========================================================================
    def _step_pier_entry(self, obs: Observation):
        surge = self.base_thrust * 0.85
        target_angle = 0.0

        p_name = "gate_pier_entry_port"
        s_name = "gate_pier_entry_starboard"

        if p_name in obs.buoys and s_name in obs.buoys:
            p_buoy = obs.buoys[p_name]
            s_buoy = obs.buoys[s_name]

            mid_x = (p_buoy.x + s_buoy.x) * 0.5
            mid_y = (p_buoy.y + s_buoy.y) * 0.5
            target_angle = math.atan2(mid_y, mid_x)

            # Transition when gate is traversed or when Ping2 sonar confirms wall lock (~5m)
            if math.hypot(mid_x, mid_y) < 2.0 or (obs.ping2.is_valid and abs(obs.ping2.distance - 5.0) < 1.5):
                print("[StudentController] Pier entry gate cleared -> Activating PIER WALL TRACKING (5.0m)")
                self.state = self.STATE_PIER_FOLLOW
        else:
            # If buoys not yet acquired, move forward until sonar locks
            if obs.ping2.is_valid and obs.ping2.distance < 8.0:
                self.state = self.STATE_PIER_FOLLOW

        yaw_rate = obs.imu.yaw_rate
        steering = 14.0 * target_angle - 3.8 * yaw_rate
        self.driver.set_thrust(surge - steering, surge + steering)

    # =========================================================================
    # PHASE 4: Sonar Wall Following & Pier Exit Gate
    # =========================================================================
    def _step_pier_follow(self, obs: Observation):
        surge = self.base_thrust * 0.85

        # Detect the exit gate marking finish line
        xp_name = "gate_pier_exit_port"
        xs_name = "gate_pier_exit_starboard"

        if xp_name in obs.buoys and xs_name in obs.buoys:
            xp = obs.buoys[xp_name]
            xs = obs.buoys[xs_name]
            mid_x = (xp.x + xs.x) * 0.5
            if mid_x < 1.5:
                print("[StudentController] Pier exit gate cleared! Course completed!")
                self.state = self.STATE_FINISHED
                self.driver.stop()
                return

        # PD wall tracking controller (setpoint = 5.0 m)
        if obs.ping2.is_valid:
            current_dist = obs.ping2.distance
            dist_error = current_dist - self.target_pier_distance
            d_error = (dist_error - self.prev_distance_error) / 0.1
            self.prev_distance_error = dist_error

            steering = (self.kp_pier * dist_error) + (self.kd_pier * d_error)
            steering = max(-20.0, min(20.0, steering))

            left_cmd = surge - steering
            right_cmd = surge + steering
            self.driver.set_thrust(left_cmd, right_cmd)
        else:
            # Slight port turn to re-acquire wall if signal is temporarily lost
            self.driver.set_thrust(surge * 0.9, surge * 1.1)


def main():
    driver = BlueBoatDriver()
    controller = StudentController(driver)
    driver.run(controller.step, rate_hz=10.0)


if __name__ == "__main__":
    main()
