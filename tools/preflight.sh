#!/bin/sh
# Run what CI runs, before pushing.
#
#   sh tools/preflight.sh            shell syntax + the test suite
#   sh tools/preflight.sh --shell    shell syntax only (what CI's job calls)
#   sh tools/preflight.sh --tests    the test suite only
#   sh tools/preflight.sh --matrix   also run the suite on the oldest and
#                                    newest supported Python, in Docker
#
# The plain run catches ordinary breakage. It cannot catch a difference between
# your machine and the runner -- both CI failures this project has had were
# exactly that: a stdlib module missing on the floor Python, and a kernel
# policy on user namespaces. --matrix covers the first of those.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mode=${1:-all}

# Every shell script in the tree, found by its shebang rather than a list, so a
# new one cannot be added without being checked.
check_shell() {
    echo "== shell syntax"
    found=0
    for file in $(find "$root/install" "$root/tools" "$root/tests" -type f \
        ! -path '*/__pycache__/*' | sort); do
        head -n 1 "$file" | grep -q '^#!/bin/sh' || continue
        sh -n "$file"
        echo "  ok: ${file#"$root"/}"
        found=$((found + 1))
    done
    [ "$found" -gt 0 ] || { echo "no shell scripts found -- wrong root?" >&2; exit 1; }
}

check_tests() {
    echo "== tests (local python: $(python3 -V 2>&1))"
    # Same variable CI sets: the installer tests must fail, not skip, if the
    # sandbox they need stops working.
    ( cd "$root" && GX_SANDBOX_REQUIRED=1 python3 -m pytest -q )
}

# bubblewrap needs a user namespace, which a default container will not give
# it, hence --privileged.  This runs local code in a throwaway container; it is
# not a sandbox boundary.
check_matrix() {
    command -v docker >/dev/null 2>&1 || {
        echo "== matrix skipped (docker not installed)"
        return 0
    }
    for image in python:3.10-slim python:3.13-slim; do
        echo "== tests on $image"
        docker run --rm --privileged -v "$root":/src:ro "$image" sh -c '
            set -e
            mkdir -p /work && cp -a /src/. /work/ && rm -rf /work/.git && cd /work
            apt-get -qq update >/dev/null
            apt-get -qq install -y bubblewrap dash busybox >/dev/null 2>&1
            pip install -q -e ".[dev]" 2>/dev/null
            GX_SANDBOX_REQUIRED=1 python -m pytest -q
        '
    done
}

case $mode in
    --shell) check_shell ;;
    --tests) check_tests ;;
    --matrix) check_shell; check_tests; check_matrix ;;
    all) check_shell; check_tests ;;
    *) echo "usage: preflight.sh [--shell|--tests|--matrix]" >&2; exit 2 ;;
esac

echo
echo "preflight passed"
