#!/bin/sh
# Run what CI runs, before pushing.
#
#   sh tools/preflight.sh            version + shell syntax + the test suite
#   sh tools/preflight.sh --shell    shell syntax only (what CI's job calls)
#   sh tools/preflight.sh --version  version consistency only (ditto)
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

# One version number lives in four places and a release is only coherent when
# all four agree: the packaged VERSION, the pyproject metadata, the changelog
# heading people read, and the git tag the release workflow builds from. The
# workflow already refuses a tag that disagrees with the code -- but that fires
# only once the tag is pushed, and 0.7.7 shipped in the code, the changelog and
# pyproject while the newest release on GitHub still said 0.7.6. Nothing was
# wrong; the tag had simply never been cut. This is the check that says so.
check_version() {
    echo "== version"
    packaged=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' \
        "$root/src/deye_virtual_battery/version.py")
    project=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml")
    logged=$(sed -n 's/^## \([0-9][0-9.]*\)[[:space:]]*$/\1/p' \
        "$root/CHANGELOG.md" | head -n 1)

    [ -n "$packaged" ] || { echo "  version.py has no VERSION" >&2; exit 1; }
    echo "  version.py    $packaged"
    echo "  pyproject     $project"
    echo "  CHANGELOG     $logged"

    if [ "$project" != "$packaged" ]; then
        echo "  pyproject.toml says $project, the package says $packaged" >&2
        exit 1
    fi
    if [ "$logged" != "$packaged" ]; then
        echo "  CHANGELOG.md's newest section is $logged, the package says $packaged" >&2
        echo "  the release workflow builds its notes from that section" >&2
        exit 1
    fi

    # Advisory, not fatal: the tag is pushed after the release commit lands, so
    # for one commit its absence is correct rather than broken.
    if [ -d "$root/.git" ] && command -v git >/dev/null 2>&1; then
        if git -C "$root" rev-parse -q --verify "refs/tags/v$packaged" >/dev/null; then
            echo "  tag           v$packaged"
        else
            echo "  tag           v$packaged is NOT cut -- no GitHub release ships this"
            echo "                cut it with: sh tools/release.sh"
        fi
    fi
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
    --version) check_version ;;
    --tests) check_tests ;;
    --matrix) check_version; check_shell; check_tests; check_matrix ;;
    all) check_version; check_shell; check_tests ;;
    *) echo "usage: preflight.sh [--shell|--version|--tests|--matrix]" >&2; exit 2 ;;
esac

echo
echo "preflight passed"
