#!/bin/sh
set -eu
CONTENTS=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$CONTENTS/Resources/ca/ca" desktop "$@"
