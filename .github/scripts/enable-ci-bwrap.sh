#!/usr/bin/env bash
# A dedicated executable profile; retain host-wide AppArmor/userns restrictions.
set -euo pipefail
sudo install -d -m 0755 /opt/deep-agent-ci
sudo install -m 0755 /usr/bin/bwrap /opt/deep-agent-ci/bwrap
sudo tee /etc/apparmor.d/deep-agent-ci-bwrap >/dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile deep-agent-ci-bwrap /opt/deep-agent-ci/bwrap flags=(unconfined) {
  userns,
}
PROFILE
sudo apparmor_parser -r /etc/apparmor.d/deep-agent-ci-bwrap
printf '%s\n' /opt/deep-agent-ci >> "$GITHUB_PATH"
