#!/bin/sh
# Prüft, ob ein Release-Tag (v0.3.3 oder v0.3.3-rc1) zur Version im Code passt.
# Aufruf: packaging/check-version.sh v0.3.3
set -eu
cd "$(dirname "$0")/.."

TAG=${1:?Tag fehlt, z. B. v0.3.3}
if ! printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'; then
    echo "Ungültiger Tag '$TAG': erwartet vMAJOR.MINOR.PATCH, optional mit Zusatz wie -rc1" >&2
    exit 1
fi

CODE=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/pi_ble_proxy/__init__.py)
PYPROJECT=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)
BASE=${TAG#v}
BASE=${BASE%%-*}

if [ "$CODE" != "$PYPROJECT" ]; then
    echo "Versionen widersprechen sich: __init__.py=$CODE, pyproject.toml=$PYPROJECT" >&2
    exit 1
fi
if [ "$BASE" != "$CODE" ]; then
    echo "Tag $TAG passt nicht zur Version im Code ($CODE)" >&2
    exit 1
fi
echo "OK: Tag $TAG entspricht Version $CODE"
