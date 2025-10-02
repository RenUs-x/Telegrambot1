Mega Telethon Bot — Final Deployment Package
============================================

This package is ready for deployment to Render (or similar services).

Files included:
 - mega_telethon_bot.py  (main bot code)
 - requirements.txt      (dependencies for pip)
 - Procfile              (start command for Render/Heroku)
 - runtime.txt           (Python version hint)
 - .env.example          (example secrets, do NOT commit real values)
 - README.md             (this file)

How to deploy on Render:
------------------------
1. Push this project to your GitHub repo.
2. On Render, create a new "Web Service" (or "Worker Service").
3. Connect your repo.
4. Render will detect requirements.txt and install dependencies.
5. Build Command: (default) `pip install -r requirements.txt`
6. Start Command: will be taken from Procfile → `python mega_telethon_bot.py`
7. In Render dashboard → Environment Variables:
   - API_ID
   - API_HASH
   - BOT_TOKEN
   - ADMIN_IDS (optional, comma-separated user IDs)
8. Deploy → the bot will run 24/7.

Run locally for testing:
------------------------
$ python3 -m venv venv && source venv/bin/activate
$ pip install -r requirements.txt
$ export API_ID=...
$ export API_HASH=...
$ export BOT_TOKEN=...
$ python mega_telethon_bot.py

Security:
---------
- Never commit real secrets to GitHub.
- Use environment variables on hosting platform.
- Rate limiting, admin-only broadcast, health check endpoint are included.
