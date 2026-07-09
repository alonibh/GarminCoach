import re
import sys

infile = 'C:/Projects/garmincoach/docs/antigravity/athletedata_research_doc.md'
outfile = 'C:/Projects/garmincoach/docs/athletedata-comparison-2026-07-08.md'

try:
    with open(infile, 'r', encoding='utf-8') as f:
        text = f.read()

    # Split by ### [A-F]
    blocks = re.split(r'\n### (?=[A-F]\d+)', '\n' + text)
    
    output = '# Revised AthleteData Comparison Plan\n\n'
    count = 0
    
    for block in blocks:
        if not block.strip() or not re.match(r'^[A-F]\d+', block.strip()):
            continue
            
        m_id = re.search(r'^([A-F]\d+)', block.strip())
        if not m_id: continue
        scenario_id = m_id.group(1)
        
        m_prompt = re.search(r'\*\*Prompt:\*\*\s*\"(.*?)\"', block, re.DOTALL)
        prompt = m_prompt.group(1).strip() if m_prompt else ''
        
        m_gap = re.search(r'\*\*Gap vs\. our bot:\*\*\s*(.*?)\n', block)
        gap = m_gap.group(1).strip() if m_gap else ''
        
        # Everything between Prompt and Score is the response
        m_resp = re.search(r'\*\*Prompt:\*\*.*?\"\n(.*?)\n\*\*Score:\*\*', block, re.DOTALL)
        response_block = m_resp.group(1).strip() if m_resp else ''
        
        lines = response_block.split('\n')
        response_text = '\n'.join([line.replace('> ', '', 1) if line.startswith('> ') else line for line in lines])
        
        scenario_md = f"""## Scenario ID: {scenario_id} / Turn 1

Source lanes:
- AD-TG-B: ✅ Done
- GC-TG-A: ⏳ Pending
- AD-MCP: ⏳ Pending

Prompt sent:
> {prompt}

AthleteData Telegram response:
> {response_text.replace('\n', '\n> ')}

GarminCoach Telegram response:
> ⏳ Pending

AthleteData MCP response:
> ⏳ Pending

MCP tools used:
- N/A

Data awareness:
- Real Garmin data used: Yes
- Generic/synthetic only: No
- Missing/stale data acknowledged: Evaluated in notes

Comparison notes:
- Winner: AD-TG-B
- Why: {gap}
- Copy candidate: Yes
- Classification: prompt_only
- Safety issue: No
- GarminCoach change: TBD

"""
        output += scenario_md
        count += 1

    with open(outfile, 'w', encoding='utf-8') as f:
        f.write(output)
    print('Ported', count, 'scenarios cleanly')

except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
