#!/usr/bin/env bash
# Run on VM: bash setup_vm.sh
# Sets up venv, installs deps, configures systemd timer.
set -e

PROJ=/home/ubuntu/tg-photos-sync

echo "=== Creating virtualenv ==="
python3 -m venv "$PROJ/venv"
"$PROJ/venv/bin/pip" install --upgrade pip
"$PROJ/venv/bin/pip" install -r "$PROJ/requirements.txt"

echo "=== Installing systemd units ==="
sudo cp "$PROJ/systemd/tg-photos-sync.service" /etc/systemd/system/
sudo cp "$PROJ/systemd/tg-photos-sync.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-photos-sync.timer

echo "=== Timer status ==="
systemctl status tg-photos-sync.timer --no-pager

echo ""
echo "Done! Next steps:"
echo "  1. Copy .env and credentials.json to $PROJ/"
echo "  2. Run auth: $PROJ/venv/bin/python $PROJ/sync.py --auth-only"
echo "  3. First sync: $PROJ/venv/bin/python $PROJ/sync.py"
echo "  4. Manual trigger: sudo systemctl start tg-photos-sync.service"
