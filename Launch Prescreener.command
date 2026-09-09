#!/usr/bin/env bash
# Double-click this in Finder to launch the Prescreener.
# It just delegates to launch.sh so there's one source of truth.
cd "$(dirname "${BASH_SOURCE[0]}")"
./launch.sh
