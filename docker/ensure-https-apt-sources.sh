#!/bin/sh
set -eu

apt_etc="${1:-/etc/apt}"
sources_dir="$apt_etc/sources.list.d"

if [ "$apt_etc" = "/etc/apt" ] && [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
    echo "ca-certificates trust store is required before enforcing HTTPS APT sources" >&2
    exit 1
fi

set -- \
    "$apt_etc/sources.list" \
    "$sources_dir"/*.list \
    "$sources_dir"/*.sources

found=0

for source_file; do
    [ -f "$source_file" ] || continue
    found=1

    sed -E -i \
        -e 's#http://deb\.debian\.org/#https://deb.debian.org/#g' \
        -e 's#http://security\.debian\.org/#https://security.debian.org/#g' \
        "$source_file"
done

if [ "$found" -ne 1 ]; then
    echo "No APT source files were found under $apt_etc" >&2
    exit 1
fi

bad=0
for source_file; do
    [ -f "$source_file" ] || continue
    if grep -Eq '^[[:space:]]*[^#].*http://' "$source_file"; then
        echo "HTTP APT repository is forbidden by the Enterprise HTTPS-only build contract: $source_file" >&2
        bad=1
    fi
done

if [ "$bad" -ne 0 ]; then
    exit 1
fi
