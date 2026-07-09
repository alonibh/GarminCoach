import urllib.request
import json
import time

url = 'https://mcp.athletedata.health/mcp?apiKey=sk_soma_3f9eae9454c65764012c510767c8e8f415a2dc922985add6'

calls = [
    ("MCP-RD-01", "get_readiness_today", {}),
    ("MCP-RD-02", "get_training_trends", {"days": 14}),
    ("MCP-RD-03", "get_daily_metrics", {"days": 7}),
    ("MCP-RD-04", "get_injury_risk", {}),
    ("MCP-RD-05", "get_pmc_status", {}),
    ("MCP-RD-06", "get_daily_metrics", {"days": 3}),
    ("MCP-RD-07", "get_performance_estimates", {}),
    ("MCP-RD-08", "reality_check_goal", {
        "goal_metric": "10k_time", 
        "current_value": "50:00", 
        "target_value": "45:00", 
        "timeframe_weeks": 10, 
        "proposed_weekly_hours": 5
    }),
    ("MCP-RD-09", "get_anomalies", {"days": 7}),
    ("MCP-RD-10", "get_analytics_summary", {"days": 14})
]

outfile = 'C:/Projects/garmincoach/docs/athletedata-comparison-2026-07-08.md'

with open(outfile, 'a', encoding='utf-8') as f:
    f.write('\n# AD-MCP Data Collection\n\n')

for mcp_id, tool_name, params in calls:
    print(f"Calling {mcp_id}: {tool_name}")
    payload = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {
            'name': tool_name,
            'arguments': params
        }
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
            result_data = None
            for line in content.split('\n'):
                if line.startswith('data: '):
                    event_data = json.loads(line[6:])
                    if 'result' in event_data:
                        result_data = event_data['result']
            
            with open(outfile, 'a', encoding='utf-8') as f:
                f.write(f"## Scenario ID: {mcp_id} / Turn 1\n\n")
                f.write(f"Source lanes:\n- AD-TG-B: N/A\n- GC-TG-A: N/A\n- AD-MCP: ✅ Done\n\n")
                f.write(f"Prompt sent:\n> Invoke `{tool_name}` with {json.dumps(params)}\n\n")
                f.write(f"AthleteData MCP response:\n```json\n{json.dumps(result_data, indent=2)}\n```\n\n")
                f.write("---\n")
                
    except Exception as e:
        print("Error on", tool_name, e)
        if hasattr(e, 'read'):
            print(e.read().decode())
            
    time.sleep(1) # rate limit safety
