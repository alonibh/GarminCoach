import json
import os

json_file = 'C:/Projects/garmincoach/docs/athletedata-telegram-raw-2026-07-08.json'
md_file = 'C:/Projects/garmincoach/docs/athletedata-comparison-2026-07-08.md'

with open(json_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

output = '\n# AD-TG-B Chained Context Flows & Discovery\n\n'

for item in data['scenarios']:
    scenario_id = item.get('id', 'UNKNOWN')
    prompt = item.get('prompt', '')
    response = item.get('response', '')
    
    if not scenario_id.startswith('FLOW-') and not scenario_id.startswith('AD-TG-B-PREFLIGHT'):
        continue
        
    output += f"""## Scenario ID: {scenario_id}

Source lanes:
- AD-TG-B: ✅ Done
- GC-TG-A: ⏳ Pending
- AD-MCP: N/A

Prompt sent:
> {prompt}

AthleteData Telegram response:
> {response.replace('\n', '\n> ')}

GarminCoach Telegram response:
> ⏳ Pending

AthleteData MCP response:
> N/A

MCP tools used:
- N/A

Data awareness:
- Real Garmin data used: {item.get('realData', 'Unknown')}
- Generic/synthetic only: {item.get('syntheticOnly', 'Unknown')}
- Missing/stale data acknowledged: {item.get('missingData', 'Unknown')}

Comparison notes:
- Winner: TBD
- Why: TBD
- Copy candidate: TBD
- Classification: TBD
- Safety issue: TBD
- GarminCoach change: TBD

"""

with open(md_file, 'a', encoding='utf-8') as f:
    f.write(output)

print("Flows appended successfully.")
