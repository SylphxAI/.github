#!/bin/sh
# Portable Git credential protocol; mounted unchanged into BuildKit as a secret.
# Store/erase never persist. No host-only, other host, or unknown-path answer.
[ "$#" = 2 ] && [ "$2" = get ] && [ -r "$1" ] || exit 0
protocol= host= path=
while IFS= read -r line && [ -n "$line" ]; do
  case "$line" in
    protocol=*) protocol=${line#protocol=} ;;
    host=*) host=${line#host=} ;;
    path=*) path=${line#path=} ;;
  esac
done
[ "$protocol" = https ] || exit 0
host=$(printf '%s' "$host" | LC_ALL=C tr '[:upper:]' '[:lower:]')
[ "$host" = github.com ] || exit 0
case "$path" in *.git) path=${path%.git} ;; esac
case "$path" in */*) owner=${path%%/*}; name=${path#*/} ;; *) exit 0 ;; esac
case "$owner" in ''|.|..|*[!a-zA-Z0-9_.-]*) exit 0 ;; esac
case "$name" in ''|.|..|*[!a-zA-Z0-9_.-]*) exit 0 ;; esac
path=$(printf '%s' "$owner/$name" | LC_ALL=C tr '[:upper:]' '[:lower:]')
tab=$(printf '\t')
while IFS="$tab" read -r repository token; do
  if [ "$repository" = "$path" ] && [ -n "$token" ]; then
    printf 'username=x-access-token\npassword=%s\n' "$token"
    exit 0
  fi
done < "$1"
