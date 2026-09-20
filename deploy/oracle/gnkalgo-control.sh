#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMMAND="${1:-}"

usage() {
  cat <<'EOF'
Usage: ./deploy/oracle/gnkalgo-control.sh COMMAND [OPTIONS]

Commands:
  start                 Validate, build, and start the complete stack
  stop                  Gracefully stop the stack (data volumes are preserved)
  restart               Restart every running service without rebuilding
  reload                Rebuild and recreate application and edge services
  status                Show stack and Docker disk-usage status
  logs [SERVICE ...]    Follow logs for all or selected services
  engine-start          Start and enable the host Docker service
  engine-stop           Stop the host Docker service after the stack is stopped

Examples:
  ./deploy/oracle/gnkalgo-control.sh start
  ./deploy/oracle/gnkalgo-control.sh reload
  ./deploy/oracle/gnkalgo-control.sh logs api caddy

The stop/restart/reload commands never delete named volumes. This script does
not use `docker compose down -v`.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

run_privileged() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "This action needs root privileges and sudo is unavailable."
  fi
}

start_docker_engine() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required to manage Docker."
  if ! systemctl is-active --quiet docker; then
    echo "Starting Docker service..."
    run_privileged systemctl start docker
  fi
  run_privileged systemctl enable docker >/dev/null
}

select_docker_command() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed."

  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif command -v sudo >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    "${DOCKER[@]}" info >/dev/null 2>&1 || die "Cannot connect to the Docker engine."
  else
    die "Cannot connect to Docker. Run this script as root or grant Docker access."
  fi

  "${DOCKER[@]}" compose version >/dev/null 2>&1 || die "Docker Compose v2 is not installed."
  COMPOSE=("${DOCKER[@]}" compose --project-directory "$PROJECT_ROOT")
}

require_project() {
  [[ -f "$PROJECT_ROOT/docker-compose.yml" ]] || die "docker-compose.yml is missing."
  [[ -f "$PROJECT_ROOT/.env" ]] || die ".env is missing. Run deploy/oracle/prepare-env.sh first."
  cd "$PROJECT_ROOT"
}

validate_config() {
  local field_encryption_key
  field_encryption_key="$(sed -n 's/^FIELD_ENCRYPTION_KEY=//p' "$PROJECT_ROOT/.env" | head -n 1 | tr -d '\r')"
  if [[ ! "$field_encryption_key" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
    die "FIELD_ENCRYPTION_KEY is invalid. Run deploy/oracle/repair-encryption-key.sh before starting."
  fi
  echo "Validating production configuration..."
  "${COMPOSE[@]}" config --quiet
}

show_status() {
  echo
  "${COMPOSE[@]}" ps
}

case "$COMMAND" in
  start)
    require_project
    start_docker_engine
    select_docker_command
    validate_config
    echo "Building and starting the GnKAlgo stack..."
    "${COMPOSE[@]}" up --build --detach --remove-orphans
    show_status
    ;;

  stop)
    require_project
    select_docker_command
    echo "Gracefully stopping the GnKAlgo stack..."
    "${COMPOSE[@]}" stop --timeout 30
    show_status
    ;;

  restart)
    require_project
    start_docker_engine
    select_docker_command
    validate_config
    echo "Restarting every GnKAlgo service..."
    "${COMPOSE[@]}" restart --timeout 30
    show_status
    ;;

  reload)
    require_project
    start_docker_engine
    select_docker_command
    validate_config
    echo "Rebuilding and recreating application and edge services..."
    "${COMPOSE[@]}" up --detach postgres redis
    "${COMPOSE[@]}" up --build --detach --force-recreate --remove-orphans api market-worker web nginx caddy
    show_status
    ;;

  status)
    require_project
    select_docker_command
    show_status
    echo
    "${DOCKER[@]}" system df
    ;;

  logs)
    require_project
    select_docker_command
    shift
    "${COMPOSE[@]}" logs --follow --tail "${TAIL_LINES:-200}" "$@"
    ;;

  engine-start)
    start_docker_engine
    systemctl --no-pager --full status docker || true
    ;;

  engine-stop)
    require_project
    select_docker_command
    if "${COMPOSE[@]}" ps --status running --quiet | grep -q .; then
      die "GnKAlgo containers are still running. Run the stop command first."
    fi
    echo "Stopping the host Docker service..."
    run_privileged systemctl stop docker
    ;;

  -h|--help|help)
    usage
    ;;

  "")
    usage
    exit 2
    ;;

  *)
    usage >&2
    die "Unknown command: $COMMAND"
    ;;
esac
