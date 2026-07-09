import urllib.request
import json
import sys

url = 'https://mcp.athletedata.health/mcp?apiKey=sk_soma_3f9eae9454c65764012c510767c8e8f415a2dc922985add6'
payload = {
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'tools/list',
    'params': {}
}
req = urllib.request.Request(
    url, 
    data=json.dumps(payload).encode('utf-8'), 
    headers={
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json, text/event-stream'
    }
)
try:
    with urllib.request.urlopen(req) as response:
        content = response.read().decode()
        print("Status:", response.getcode())
        print("Response:", content[:1000])
except Exception as e:
    print("Error:", e)
    if hasattr(e, 'read'):
        print(e.read().decode())
