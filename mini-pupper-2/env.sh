# Usage: source env.sh   (or: . env.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate the venv if it isn't already
[ -z "$VIRTUAL_ENV" ] && source "$SCRIPT_DIR/.venv/bin/activate"

export MIC_GAIN=20
export FULL_DUPLEX=0
export SPK_VOLUME=80
export BARGE_RATIO=4.0
