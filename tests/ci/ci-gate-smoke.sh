#!/usr/bin/env bash
# Issue #5 Phase 1 CI failure/recovery smoke test.
# This file is deliberately syntax-invalid for a temporary test PR only.
# It must never be merged into main.
if true; then
  echo 'CI smoke test'
# Intentionally missing 'fi' so that bash -n fails.
