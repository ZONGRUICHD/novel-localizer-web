#!/bin/sh
set -eu

TMP_ROOT=/var/lib/shiori/tmp
UPLOAD_ROOT=/var/lib/shiori/uploads

if [ -d "$TMP_ROOT" ]; then
  find "$TMP_ROOT" -xdev -mindepth 1 -type f -mtime +2 -delete
  find "$TMP_ROOT" -xdev -mindepth 1 -type d -empty -mtime +2 -delete
fi

if [ -d "$UPLOAD_ROOT" ]; then
  find "$UPLOAD_ROOT" -xdev -mindepth 2 -type f -name '*.part' -mtime +7 -delete
  find "$UPLOAD_ROOT" -xdev -mindepth 1 -type d -empty -mtime +7 -delete
fi
