#!/bin/bash
# webctl.sh for openshelf (CMS圖書館 / Libgen 鏡像檢索全文檢索與線上閱讀器)

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

ACTION="${1:-status}"

case "$ACTION" in
  start)
    echo "Starting openshelf container..."
    docker compose up -d
    ;;
  stop)
    echo "Stopping openshelf container..."
    docker compose stop
    ;;
  restart)
    echo "Restarting openshelf container..."
    docker compose restart
    ;;
  status)
    if docker compose ps | grep -q "Up"; then
      echo "openshelf is RUNNING"
      exit 0
    else
      echo "openshelf is STOPPED"
      exit 1
    fi
    ;;
  health)
    if curl -s -f http://127.0.0.1:8088/api/health >/dev/null 2>&1; then
      echo "openshelf is HEALTHY"
      exit 0
    else
      echo "openshelf is UNHEALTHY"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|health}"
    exit 1
    ;;
esac
