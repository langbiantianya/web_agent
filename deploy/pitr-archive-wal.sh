#!/bin/sh
# PostgreSQL archive_command helper.
#
# archive_command = 'PG_ARCHIVE_DIR=/var/lib/postgresql/archive \
#   /opt/webagent/pitr-archive-wal.sh "%p" "%f"'
#
# Exit non-zero on any ambiguity so PostgreSQL retains the WAL segment and retries
# instead of silently losing the only copy.
set -eu

source_path=${1:?missing source WAL path}
file_name=${2:?missing WAL file name}
archive_dir=${PG_ARCHIVE_DIR:?missing PG_ARCHIVE_DIR}

case "$file_name" in
  */*|.*|'')
    echo "invalid WAL archive name: $file_name" >&2
    exit 2
    ;;
esac

mkdir -p "$archive_dir"
target="$archive_dir/$file_name"

if [ -e "$target" ]; then
  if cmp -s "$source_path" "$target"; then
    exit 0
  fi
  echo "archive target already exists with different content: $target" >&2
  exit 3
fi

temporary="$archive_dir/.$file_name.tmp.$$"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
cp "$source_path" "$temporary"
chmod 0600 "$temporary"
mv "$temporary" "$target"
trap - EXIT HUP INT TERM
