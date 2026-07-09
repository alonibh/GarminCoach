import re
import sys
import time
from db import SessionLocal
from coach import coach

md_file = 'C:/Projects/garmincoach/docs/athletedata-comparison-2026-07-08.md'

with open(md_file, 'r', encoding='utf-8') as f:
    text = f.read()

# We need to find blocks that have GC-TG-A: ⏳ Pending
# But it's easier to just split by scenario, process, and rebuild.

blocks = re.split(r'\n## Scenario ID: ', '\n' + text)
output = blocks[0].strip() + '\n\n'

with SessionLocal() as session:
    for block in blocks[1:]:
        if 'GarminCoach Telegram response:\n> ⏳ Pending' in block:
            # extract prompt
            m_prompt = re.search(r'Prompt sent:\n> (.*?)\n\n', block, re.DOTALL)
            if m_prompt:
                prompt = m_prompt.group(1).replace('\n> ', '\n').strip()
                print(f"Running prompt: {prompt[:30]}...")
                try:
                    gc_response = coach.handle_chat(session, prompt)[0]
                    
                    # replace the pending state
                    gc_response_md = gc_response.replace('\n', '\n> ')
                    block = block.replace(
                        'GarminCoach Telegram response:\n> ⏳ Pending',
                        f'GarminCoach Telegram response:\n> {gc_response_md}'
                    )
                    block = block.replace(
                        '- GC-TG-A: ⏳ Pending',
                        '- GC-TG-A: ✅ Done'
                    )
                except Exception as e:
                    print(f"Error running handle_chat: {e}")
        
        output += '## Scenario ID: ' + block + '\n'

with open(md_file, 'w', encoding='utf-8') as f:
    f.write(output)

print("GC-TG-A baseline tests completed.")
