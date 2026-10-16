#!/usr/bin/env bash
# ==============================================================================
# BlueCTFd Service Automation Script
# Starts, stops, restarts, and monitors BlueAgent and CTFd in the background.
# ==============================================================================

set -eo pipefail

BASE_DIR="/home/debian/bluectfd"
LOG_DIR="${BASE_DIR}/logs"
RUN_DIR="${BASE_DIR}/run"
BLUEAGENT_DIR="${BASE_DIR}/blueagent"
CTFD_DIR="${BASE_DIR}/CTFd"

BLUEAGENT_PID_FILE="${RUN_DIR}/blueagent.pid"
CTFD_PID_FILE="${RUN_DIR}/ctfd.pid"

SHARED_TOKEN="${BLUEAGENT_TOKEN:-blueagent_secret_token_123}"
BLUEAGENT_PORT="${BLUEAGENT_PORT:-8000}"
CTFD_PORT="${CTFD_PORT:-4000}"

mkdir -p "${LOG_DIR}" "${RUN_DIR}"

is_running() {
    local pid_file="$1"
    if [[ -f "${pid_file}" ]]; then
        local pid
        pid=$(cat "${pid_file}")
        if [[ -n "${pid}" ]] && ps -p "${pid}" > /dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

start_blueagent() {
    echo -n "Starting BlueAgent daemon (port ${BLUEAGENT_PORT})... "
    if is_running "${BLUEAGENT_PID_FILE}"; then
        echo "ALREADY RUNNING (PID: $(cat "${BLUEAGENT_PID_FILE}"))"
        return 0
    fi

    # BlueAgent requires root privileges to manage Linux bridges, WireGuard, and nftables
    sudo env BLUEAGENT_CTFD_API_TOKEN="${SHARED_TOKEN}" \
         BLUEAGENT_HOST="0.0.0.0" \
         BLUEAGENT_PORT="${BLUEAGENT_PORT}" \
         "${BLUEAGENT_DIR}/.venv/bin/uvicorn" blueagent.main:app \
         --host 0.0.0.0 --port "${BLUEAGENT_PORT}" \
         >> "${LOG_DIR}/blueagent.log" 2>&1 &

    local pid=$!
    echo "${pid}" | sudo tee "${BLUEAGENT_PID_FILE}" > /dev/null

    # Wait for health endpoint
    local retries=15
    while [[ ${retries} -gt 0 ]]; do
        if curl -s "http://127.0.0.1:${BLUEAGENT_PORT}/health" | grep -Eq '"status":"(ok|healthy)"' 2>/dev/null; then
            echo "STARTED (PID: ${pid}, Health: OK)"
            return 0
        fi
        sleep 1
        retries=$((retries - 1))
    done

    echo "WARNING: BlueAgent spawned with PID ${pid}, but healthcheck timed out. Check ${LOG_DIR}/blueagent.log"
}

start_ctfd() {
    echo -n "Starting CTFd application (port ${CTFD_PORT})... "
    if is_running "${CTFD_PID_FILE}"; then
        echo "ALREADY RUNNING (PID: $(cat "${CTFD_PID_FILE}"))"
        return 0
    fi

    cd "${CTFD_DIR}"
    env BLUEAGENT_URL="http://127.0.0.1:${BLUEAGENT_PORT}" \
        BLUEAGENT_TOKEN="${SHARED_TOKEN}" \
        PYTHONPATH="${CTFD_DIR}" \
        "${CTFD_DIR}/.venv/bin/python" serve.py \
        --host 0.0.0.0 --port "${CTFD_PORT}" \
        >> "${LOG_DIR}/ctfd.log" 2>&1 &

    local pid=$!
    echo "${pid}" > "${CTFD_PID_FILE}"

    # Wait for CTFd HTTP endpoint
    local retries=25
    while [[ ${retries} -gt 0 ]]; do
        if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${CTFD_PORT}/" | grep -Eq '200|302' 2>/dev/null; then
            echo "STARTED (PID: ${pid}, HTTP: Ready)"
            return 0
        fi
        sleep 1
        retries=$((retries - 1))
    done

    echo "WARNING: CTFd spawned with PID ${pid}, but HTTP check timed out. Check ${LOG_DIR}/ctfd.log"
}

stop_blueagent() {
    echo -n "Stopping BlueAgent... "
    if is_running "${BLUEAGENT_PID_FILE}"; then
        local pid
        pid=$(cat "${BLUEAGENT_PID_FILE}")
        sudo kill "${pid}" 2>/dev/null || true
        for _ in {1..10}; do
            if ! ps -p "${pid}" > /dev/null 2>&1; then
                break
            fi
            sleep 0.5
        done
        if ps -p "${pid}" > /dev/null 2>&1; then
            sudo kill -9 "${pid}" 2>/dev/null || true
        fi
        sudo rm -f "${BLUEAGENT_PID_FILE}"
        echo "STOPPED"
    else
        sudo pkill -f "uvicorn blueagent.main:app" 2>/dev/null || true
        sudo rm -f "${BLUEAGENT_PID_FILE}"
        echo "NOT RUNNING"
    fi
}

stop_ctfd() {
    echo -n "Stopping CTFd... "
    if is_running "${CTFD_PID_FILE}"; then
        local pid
        pid=$(cat "${CTFD_PID_FILE}")
        kill "${pid}" 2>/dev/null || true
        for _ in {1..10}; do
            if ! ps -p "${pid}" > /dev/null 2>&1; then
                break
            fi
            sleep 0.5
        done
        if ps -p "${pid}" > /dev/null 2>&1; then
            kill -9 "${pid}" 2>/dev/null || true
        fi
        rm -f "${CTFD_PID_FILE}"
        echo "STOPPED"
    else
        pkill -f "python serve.py --host 0.0.0.0 --port ${CTFD_PORT}" 2>/dev/null || true
        rm -f "${CTFD_PID_FILE}"
        echo "NOT RUNNING"
    fi
}

status_services() {
    echo "==================== BlueCTFd Services Status ===================="
    echo -n "BlueAgent (FastAPI :${BLUEAGENT_PORT}): "
    if is_running "${BLUEAGENT_PID_FILE}"; then
        local pid
        pid=$(cat "${BLUEAGENT_PID_FILE}")
        local health
        health=$(curl -s --max-time 2 "http://127.0.0.1:${BLUEAGENT_PORT}/health" 2>/dev/null || echo "UNREACHABLE")
        echo "RUNNING (PID: ${pid}) | Health: ${health}"
    else
        echo "STOPPED"
    fi

    echo -n "CTFd (Flask :${CTFD_PORT}):      "
    if is_running "${CTFD_PID_FILE}"; then
        local pid
        pid=$(cat "${CTFD_PID_FILE}")
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://127.0.0.1:${CTFD_PORT}/" 2>/dev/null || echo "ERR")
        echo "RUNNING (PID: ${pid}) | HTTP Code: ${code}"
    else
        echo "STOPPED"
    fi
    echo "=================================================================="
}

show_logs() {
    echo "=== Last 25 lines of BlueAgent log (${LOG_DIR}/blueagent.log) ==="
    tail -n 25 "${LOG_DIR}/blueagent.log" 2>/dev/null || echo "No blueagent log yet."
    echo ""
    echo "=== Last 25 lines of CTFd log (${LOG_DIR}/ctfd.log) ==="
    tail -n 25 "${LOG_DIR}/ctfd.log" 2>/dev/null || echo "No ctfd log yet."
}

case "$1" in
    start)
        start_blueagent
        start_ctfd
        status_services
        ;;
    stop)
        stop_ctfd
        stop_blueagent
        ;;
    restart)
        stop_ctfd
        stop_blueagent
        sleep 1
        start_blueagent
        start_ctfd
        status_services
        ;;
    status)
        status_services
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
