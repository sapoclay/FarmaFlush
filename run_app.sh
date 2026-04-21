#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$PROJECT_DIR/app.py"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
PORT="5000"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "No se encontró el intérprete del entorno virtual en: $VENV_PYTHON" >&2
    echo "Crea el entorno e instala dependencias antes de arrancar." >&2
    exit 1
fi

mapfile -t project_pids < <(pgrep -f "(^|/)(python|python3|\.venv/bin/python)( .*)? $APP_FILE$|(^|/)(python|python3|\.venv/bin/python)( .*)? app\.py$" || true)

if (( ${#project_pids[@]} > 0 )); then
    echo "Cerrando instancia previa del proyecto: ${project_pids[*]}"
    kill "${project_pids[@]}" || true

    for _ in {1..20}; do
        mapfile -t remaining_pids < <(pgrep -f "(^|/)(python|python3|\.venv/bin/python)( .*)? $APP_FILE$|(^|/)(python|python3|\.venv/bin/python)( .*)? app\.py$" || true)
        if (( ${#remaining_pids[@]} == 0 )); then
            break
        fi
        sleep 0.2
    done

    if (( ${#remaining_pids[@]} > 0 )); then
        echo "Forzando cierre de procesos restantes: ${remaining_pids[*]}"
        kill -9 "${remaining_pids[@]}" || true
    fi
fi

port_pids="$(ss -ltnp 2>/dev/null | awk -v port=":$PORT" '$4 ~ port {print $NF}')"
if [[ -n "$port_pids" ]]; then
    if ss -ltnp 2>/dev/null | grep -q ":$PORT "; then
        echo "El puerto $PORT sigue ocupado por otro proceso ajeno al proyecto." >&2
        echo "Libéralo manualmente o cambia el puerto antes de continuar." >&2
        ss -ltnp | grep ":$PORT " >&2 || true
        exit 1
    fi
fi

cd "$PROJECT_DIR"
exec "$VENV_PYTHON" "$APP_FILE"