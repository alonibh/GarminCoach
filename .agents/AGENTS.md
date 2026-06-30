# Rules for Agents

- Commit and push to GitHub after every chat/piece of work.
- Discard (revert) changes that are not relevant or were abandoned during testing.
- ALWAYS verify the deployment after pushing code and restarting the remote server. You must check the service logs (`sudo journalctl -u garmincoach -n 50`) and perform a quick smoke test (e.g. `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/`) to ensure the server didn't crash on startup.
- After each change you make, you must make sure you didn't break anything, that the new change actually works, and all tests pass. If new tests are needed, add them.
