#!/bin/sh
# Fix ownership of bind-mounted directories so the txporter user can write to them.
# Runs as root, then drops to txporter via runuser.
chown txporter:txporter /home/txporter/config /home/txporter/output 2>/dev/null || true
exec runuser -u txporter -- "$@"
