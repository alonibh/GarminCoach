#!/bin/bash
echo "Fixing Nginx timeouts..."
for file in /etc/nginx/sites-available/*; do
  if sudo grep -q "proxy_pass" "$file"; then
    sudo sed -i '/proxy_read_timeout/d' "$file"
    sudo sed -i '/proxy_connect_timeout/d' "$file"
    sudo sed -i '/proxy_send_timeout/d' "$file"
    sudo sed -i '/proxy_pass/a \    proxy_read_timeout 300s;\n    proxy_connect_timeout 300s;\n    proxy_send_timeout 300s;' "$file"
  fi
done
sudo systemctl reload nginx || true

echo "Fixing systemd timeouts..."
SVC="/etc/systemd/system/garmincoach.service"
if [ -f "$SVC" ]; then
  sudo sed -i 's/ --timeout 300//g' "$SVC"
  sudo sed -i 's/ --timeout-keep-alive 300//g' "$SVC"
  
  if sudo grep -q "gunicorn" "$SVC"; then
    sudo sed -i '/ExecStart=/ s/$/ --timeout 300/' "$SVC"
  elif sudo grep -q "uvicorn" "$SVC"; then
    sudo sed -i '/ExecStart=/ s/$/ --timeout-keep-alive 300/' "$SVC"
  fi
  
  sudo systemctl daemon-reload
fi
