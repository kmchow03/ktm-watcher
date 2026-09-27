# KTM Shuttle Tebrau seat watcher (runs on GitHub, no laptop needed)

GitHub's servers check the KTMB Shuttle Tebrau page about every 5–15 minutes for the trips in
`config.toml`. When a train in your time window gets free seats, you get a Telegram message.
You then book in the KITS app. The bot never logs in and never buys anything.

You can do all of this on your phone. Tip: in your phone browser, switch to "Desktop site"
when you're on GitHub. The menus are easier to find that way.

## 1. Telegram (5 min)
1. In Telegram, message **@BotFather** and send `/newbot`. Pick any name.
   It replies with a **token** like `123456789:AAE...`. Copy it somewhere.
2. Open your new bot (BotFather gives you the link) and press **Start**.
   Your bot can't message you until you do this.
3. Message **@userinfobot**. It replies with your **Id** (a number). Copy it.

## 2. GitHub (10 min)
1. Sign up at github.com (free).
2. Tap **+ → New repository**. Name it e.g. `ktm-watcher`, choose **Public**, and tap **Create**.
   (It needs to be Public for unlimited free runs. Only your travel dates show; your Telegram details stay secret.)
3. Tap **Add file → Upload files** and upload `ktm_watch.py`, `config.toml`, `requirements.txt`
   and `README.md`. Then tap **Commit changes**.
4. Tap **Add file → Create new file**. In the name box, type exactly:
   `.github/workflows/check-seats.yml`
   Paste in the contents of `check-seats.yml`, then tap **Commit changes**.
5. Go to **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:
   - `TELEGRAM_BOT_TOKEN`: your token from step 1.1
   - `TELEGRAM_CHAT_ID`: your Id from step 1.3

## 3. Choose trips and start
1. Open `config.toml` on GitHub and tap the ✏️ pencil. Set the direction, date and time window,
   then tap **Commit changes**. Add another `[[watch]]` block for each extra trip.
2. Go to the **Actions** tab. If asked, tap **"I understand my workflows, go ahead and enable them"**.
3. Tap **Check KTM seats → Run workflow** to do a first check right away.
   A green tick means it worked. Tap into the run and open **Check seats** to see the seat counts.
   After that it runs on its own.

## Changing or stopping
- **New dates:** edit `config.toml` again. Trips with past dates are skipped automatically.
- **Pause:** Actions → Check KTM seats → ••• → **Disable workflow**.

## If it can't read the page
KTMB may change its site, or it may block overseas servers. GitHub's servers are in the US.
After 3 failed checks in a row you'll get a Telegram warning. Open the failed run on the
Actions tab and download **debug** at the bottom, which has a screenshot. Send it to Claude to fix.
