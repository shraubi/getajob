#!/bin/bash
set -e

CV_FILE=""
ENV_FILE=".env"

while [[ $# -gt 0 ]]; do
  case $1 in
    --cv)       CV_FILE="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: env file '$ENV_FILE' not found"
  exit 1
fi

DOCKER_ARGS=(--rm --env-file "$ENV_FILE")

if [ -n "$CV_FILE" ]; then
  if [ ! -f "$CV_FILE" ]; then
    echo "Error: CV file '$CV_FILE' not found"
    exit 1
  fi
  DOCKER_ARGS+=(-v "$(realpath "$CV_FILE"):/app/cv/base_cv.txt:ro")
fi

docker run "${DOCKER_ARGS[@]}" getajob-test
