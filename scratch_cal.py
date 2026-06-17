import config
from icalevents.icalevents import events
from datetime import datetime, timedelta

config.ICS_CALENDAR_URL = 'https://p107-caldav.icloud.com/published/2/NDExNDY5NDU0NDExNDY5NAHCKMcPHjlr1pBzoYgHOCxVAWLucBFo0VXcPl70KjZCFh0sncNaniKzvZlGAVn4rWHm24csBsC7O9ImpXTuDdY'

start = datetime.now()
end = start + timedelta(days=7)
evs = events(url=config.ICS_CALENDAR_URL, start=start, end=end)
evs.sort(key=lambda x: x.start)

print(f"Total events found in next 7 days: {len(evs)}")

for e in evs:
    print(f"{'ALL DAY ' if e.all_day else ''}{e.start.astimezone().strftime('%Y-%m-%d %H:%M')} - {e.end.astimezone().strftime('%H:%M')}: {e.summary.encode('ascii', 'ignore').decode('ascii')}")
