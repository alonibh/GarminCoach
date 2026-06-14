import datetime
from sync.garmin_client import client
client.login()
print(client.api.get_stats((datetime.date.today() - datetime.timedelta(days=1)).isoformat()))
