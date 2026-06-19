$ErrorActionPreference = "Stop"
$KeyPath = "C:\Users\aloni\Downloads\ssh-key-2026-06-14.key"
$Ip = "82.70.222.42"

Write-Host "1. Creating application archive..."
tar.exe -czf garmincoach.tar.gz --exclude=.venv --exclude=__pycache__ --exclude=.git --exclude=*.pyc --exclude=garmincoach.db .

Write-Host "2. Uploading archive to server..."
scp -i $KeyPath -o StrictHostKeyChecking=no garmincoach.tar.gz "ubuntu@${Ip}:~/"

Write-Host "3. Uploading setup script..."
scp -i $KeyPath -o StrictHostKeyChecking=no setup.sh "ubuntu@${Ip}:~/"

Write-Host "4. Running setup script on server..."
ssh -i $KeyPath -o StrictHostKeyChecking=no "ubuntu@${Ip}" "bash setup.sh"

Write-Host "Deployment complete!"
