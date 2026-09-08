#!/bin/sh
# Cut the release for whatever version the code already says it is.
#
#   sh tools/release.sh              check everything, tag, push
#   sh tools/release.sh --dry-run    check everything, change nothing
#
# The version is never an argument. It is read out of version.py, so the tag
# cannot name something other than what it ships -- which is the whole failure
# this script exists to prevent. Pushing the tag is the only step: the release
# workflow re-checks the version, runs the suite against the tagged tree, and
# publishes the notes from that version's CHANGELOG section.
#
# A release is installed onto live inverter hardware, so every precondition is
# checked before the tag exists rather than after.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$root/src/deye_virtual_battery/version.py")
tag="v$version"

refuse() {
    echo "refusing to release: $1" >&2
    exit 1
}

# The tag must point at something the world can already fetch, so the release
# and main never describe different trees.
branch=$(git -C "$root" rev-parse --abbrev-ref HEAD)
[ "$branch" = "main" ] || refuse "on branch $branch, not main"
[ -z "$(git -C "$root" status --porcelain)" ] || refuse "the working tree is dirty"

git -C "$root" fetch --quiet --tags origin
[ -n "$(git -C "$root" rev-parse -q --verify origin/main)" ] ||
    refuse "origin/main is unknown"
[ "$(git -C "$root" rev-parse HEAD)" = "$(git -C "$root" rev-parse origin/main)" ] ||
    refuse "HEAD is not origin/main -- push the commits first"

if git -C "$root" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    refuse "$tag already exists; bump VERSION and the CHANGELOG for the next one"
fi

# The same section the workflow will publish. An empty one produces a release
# with no notes, which is worse than a failed release.
grep -q "^## $version\$" "$root/CHANGELOG.md" ||
    refuse "CHANGELOG.md has no '## $version' section"

sh "$root/tools/preflight.sh" "${PREFLIGHT_MODE:-all}"

echo
echo "release $tag at $(git -C "$root" rev-parse --short HEAD)"
if [ "$dry_run" -eq 1 ]; then
    echo "dry run: nothing was tagged or pushed"
    exit 0
fi

git -C "$root" tag -a "$tag" -m "$tag"
git -C "$root" push origin "$tag"

echo
echo "tag pushed. The release workflow now re-checks the version, runs the"
echo "suite against $tag and publishes the notes. Watch it with:"
echo "  gh run watch"
echo "  gh release view $tag"
