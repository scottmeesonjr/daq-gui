#!/bin/bash
set -e

ROLE="$1"
DIR="$HOME/daq"
case "$ROLE" in
  rx) SCRIPT="daq_rx_dashboard.py" ;;
  tx) SCRIPT="daq_tx.py" ;;
  *)  echo "Usage: bash setup_autostart.sh rx   (or tx)"; exit 1 ;;
esac

[ -f "$DIR/$SCRIPT" ]         || { echo "Missing $DIR/$SCRIPT. Copy it over first."; exit 1; }
[ -x "$DIR/venv/bin/python" ] || { echo "Missing $DIR/venv. Create the virtual environment first."; exit 1; }

if [ "$ROLE" = "tx" ]; then
  read -r -p "This starts transmitting on every boot. Is the antenna attached? (yes/no) " ok
  [ "$ok" = "yes" ] || { echo "Stopped. Attach the antenna first."; exit 1; }
fi

echo "== Installing auto-start service =="
sudo tee /etc/systemd/system/aurelian.service > /dev/null << SERVICE
[Unit]
Description=Project Aurelian DAQ ($ROLE)
After=network.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -u $DIR/$SCRIPT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE
sudo systemctl daemon-reload
sudo systemctl enable --now aurelian.service
echo "Service running. Live log: journalctl -u aurelian -f"

if [ "$ROLE" = "rx" ]; then
  echo
  echo "== Setting up the Aurelian-DAQ WiFi network =="
  while true; do
    read -r -s -p "Choose a WiFi password (8+ characters): " PASS; echo
    [ ${#PASS} -ge 8 ] && break
    echo "Too short."
  done
  sudo nmcli connection delete aurelian-hotspot > /dev/null 2>&1 || true
  sudo nmcli connection add type wifi ifname wlan0 con-name aurelian-hotspot \
    ssid "Aurelian-DAQ" 802-11-wireless.mode ap 802-11-wireless.band bg \
    ipv4.method shared wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" \
    connection.autoconnect yes connection.autoconnect-priority 100 > /dev/null
  echo
  echo "Done. The hotspot turns on at the next reboot (not now, so this SSH session survives)."
  echo "After: sudo reboot"
  echo "  1. Join WiFi 'Aurelian-DAQ' on your laptop/phone"
  echo "  2. Dashboard: http://10.42.0.1:5000   (or http://aurelian-rx.local:5000)"
  echo "  3. SSH:       ssh $USER@10.42.0.1"
fi
