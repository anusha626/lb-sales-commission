# Deploying the LB Commission Calculator

End result: a URL like `https://lb-sales-commission.streamlit.app` your colleagues open in any browser. Free. ~10 minutes the first time, ~1 minute for every change after that.

The app is gated by a shared password. Anyone without it sees only a sign-in box.

## Prerequisites (one-time)

1. **GitHub account** — sign up free at https://github.com/signup if you don't have one.
2. **Git on your Mac** — open Terminal and run `git --version`. If it prompts to install Xcode Command Line Tools, click Install. Wait for it to finish.
3. **Streamlit Cloud account** — sign up free at https://share.streamlit.io using your GitHub account (one click, no password to remember).

## Step 1 — Create a private GitHub repo

1. Go to https://github.com/new
2. **Repository name:** `lb-sales-commission`
3. **Visibility:** **Private** (this is important — keeps your code private)
4. **Don't** tick any "Add a README", "Add .gitignore", or "Add license" boxes — we already have these locally
5. Click **Create repository**
6. The next page shows commands. Note the URL — it looks like `https://github.com/<your-username>/lb-sales-commission.git`

## Step 2 — Push the project to GitHub (Terminal)

Open Terminal and paste these commands one by one. Replace `<your-username>` with your GitHub username.

```bash
cd "/Users/lbinternationalsdnbhd/Documents/Sales Commssion ai"

# Initialise git in the project folder
git init -b main

# Tell git who you are (only needed once per Mac)
git config --global user.email "you@example.com"
git config --global user.name "Your Name"

# Stage everything that .gitignore allows
git add .

# Take a snapshot
git commit -m "Initial commit: LB sales commission calculator"

# Connect to GitHub
git remote add origin https://github.com/<your-username>/lb-sales-commission.git

# Push it up
git push -u origin main
```

GitHub may ask for a username and password. Use your GitHub username and a **Personal Access Token** (not your GitHub password) — generate one at https://github.com/settings/tokens with the "repo" scope ticked.

After this, refresh your GitHub repo page in the browser and you'll see all the project files — except `.streamlit/secrets.toml`, `sample_data.csv`, and the `attachments_test/` folder, which are correctly excluded.

## Step 3 — Deploy on Streamlit Cloud

1. Go to https://share.streamlit.io
2. Click **Create app** (or **New app**)
3. **Repository:** select `<your-username>/lb-sales-commission`
4. **Branch:** `main`
5. **Main file path:** `app.py`
6. **App URL** (optional): customise the subdomain, e.g. `lb-sales-commission` → `https://lb-sales-commission.streamlit.app`
7. Click **Advanced settings…** before deploying
8. **Python version:** 3.11 or newer
9. **Secrets:** paste this exact line:
   ```
   app_password = "Lbitesa88"
   ```
10. Click **Save**, then **Deploy**

The first build takes 2–4 minutes. When it's done you'll see your app — already gated by the password.

## Step 4 — Test it

1. Open the URL Streamlit gives you in a private/incognito browser window
2. You should see "💼 LB Commission Calculator — Enter the shared password to continue"
3. Enter `Lbitesa88` → the full app appears
4. Share the URL + password with your colleagues over a private channel (NOT email/Slack with screenshots committed in chat history)

## Updating the app later

Anything you change in the project on your Mac, push to GitHub and Streamlit Cloud auto-redeploys in ~1 minute:

```bash
cd "/Users/lbinternationalsdnbhd/Documents/Sales Commssion ai"
git add .
git commit -m "Describe what changed"
git push
```

Common updates:

- **New month's CSV processing** — no push needed, your colleagues just upload the CSV in the app
- **Maybank rate change** — edit `data/rates.json` on your Mac, then push (the live app picks up new rates after redeploy)
- **New Sales Advisor** — edit `data/sa_list.json` on your Mac, then push
- **Change the shared password** — change it in Streamlit Cloud's Secrets manager (Settings → Secrets in the app dashboard); no push needed

## Things that DON'T sync to GitHub

By design (so they stay private):

- `.streamlit/secrets.toml` — the password file on your Mac
- `sample_data.csv` and any other `.csv` at the project root — customer data
- `attachments_test/` and `*.pdf`, `*.jpg`, `*.png` — proof-of-payment files
- The `.venv/` virtual environment

Your colleagues never need any of these. They only need the URL + password.

## Important: things to know

1. **The free Streamlit Cloud disk is ephemeral.** If you or a colleague edits rate %, tier %, or SAs from the **Settings page in the live app**, those edits will be **lost on the next redeploy** (which happens whenever you push, or when the app sleeps and wakes). For settings that need to stick, edit the JSON file on your Mac and `git push`. (For your monthly use this is fine — you'll edit rates ~yearly.)

2. **Apps sleep after ~7 days of no traffic.** First visitor after sleep waits ~30 seconds for it to wake. After that it's instant.

3. **The CSV your colleagues upload only lives in their browser session.** Closing the tab clears it. No sales data is stored anywhere on the server.

4. **Always test in incognito** after a deploy to confirm the password gate is active. If you ever see the app without being asked for a password, the secret didn't get set — go back to the Streamlit Cloud Secrets manager and check the value.

## If something goes wrong

- **"Authentication failed" when pushing to GitHub** → you need a Personal Access Token, not your GitHub password. Generate one at https://github.com/settings/tokens, "Generate new token (classic)", tick `repo`, copy the token, paste it as the password.
- **Streamlit build fails** → click "Manage app" in the Streamlit Cloud dashboard and read the build log. 99% of the time it's a typo in `requirements.txt`.
- **App loads but skips the password** → secrets aren't set. Open the app's Settings on Streamlit Cloud, paste `app_password = "Lbitesa88"` into the Secrets box, save, and reboot the app from the dashboard.
- **Anything else** → ask Claude. Paste the error message.
