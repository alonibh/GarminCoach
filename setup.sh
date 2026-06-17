#!/bin/bash
set -e

echo "Updating system and installing dependencies..."
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip iptables-persistent

echo "Extracting app..."
mkdir -p ~/garmincoach
tar -xzf ~/garmincoach.tar.gz -C ~/garmincoach/

echo "Setting up virtual environment..."
cd ~/garmincoach
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

echo "Running database migrations..."
python3 migrate.py

echo "Configuring systemd service..."
sudo tee /etc/systemd/system/garmincoach.service > /dev/null << 'EOF'
[Unit]
Description=GarminCoach Uvicorn Server
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/garmincoach
Environment="PATH=/home/ubuntu/garmincoach/.venv/bin"
ExecStart=/home/ubuntu/garmincoach/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 80
Restart=always

[Install]
WantedBy=multi-user.target
EOF

echo "Allowing Uvicorn to bind to Port 80..."
# Allow python to bind to port 80 without root
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f ~/.venv/bin/python3 || echo ~/.venv/bin/python) || true
# Alternatively, use iptables to route 80 to 8000
sudo iptables -A PREROUTING -t nat -i enp0s3 -p tcp --dport 80 -j REDIRECT --to-port 8000 || true
sudo iptables -A PREROUTING -t nat -i eth0 -p tcp --dport 80 -j REDIRECT --to-port 8000 || true
sudo sh -c "iptables-save > /etc/iptables/rules.v4" || true

# Actually, the easiest way on Ubuntu to run uvicorn on port 80 as a normal user 
# via systemd is to let systemd bind the port or run it on 8000 and use iptables.
# We will use iptables to forward 80 to 8000, and run uvicorn on 8000.

# Update the service file to use port 8000
sudo sed -i 's/--port 80/--port 8000/g' /etc/systemd/system/garmincoach.service

echo "Starting service..."
sudo systemctl daemon-reload
sudo systemctl enable garmincoach.service
sudo systemctl restart garmincoach.service

echo "Deployment complete! App is running on port 8000 (and mapped to port 80)."
