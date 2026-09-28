#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== [1/3] Building local Gazebo Jetty Docker image ==="
docker build \
    -f "${SCRIPT_DIR}/Dockerfile.local" \
    -t ocean-regatta-student-local:2026 \
    "${SCRIPT_DIR}"

echo "=== [2/3] Preparing local replay directory ==="
mkdir -p "${REPO_ROOT}/local_output/replay"
chmod -R 777 "${REPO_ROOT}/local_output"

echo "=== [3/3] Executing local simulation sandbox ==="
docker run --rm -it \
    -v "${REPO_ROOT}:/workspace" \
    -w /workspace \
    ocean-regatta-student-local:2026 \
    bash -c '
        export DISPLAY=:99
        Xvfb :99 -screen 0 1024x768x24 &
        
        gz sim -s -r -v 2 \
            --headless-rendering \
            worlds/practice_world.sdf \
            --log-record \
            --log-record-path /workspace/local_output/replay &
        GZ_PID=$!
        
        sleep 4
        python3 starter_kit/student_controller.py &
        STUDENT_PID=$!
        
        echo "Simulation running (Press Ctrl+C to stop)..."
        wait $STUDENT_PID || true
        kill $GZ_PID 2>/dev/null || true
    '

echo "Simulation complete. Replay available: gz sim --playback ./local_output/replay"
