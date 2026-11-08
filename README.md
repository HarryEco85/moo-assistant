# Moo Lite

Your own voice assistant for Windows. Talk to Moo (or type), and she answers in a British female voice.

**She can:** summarise your day, check and search your Outlook email, check your calendar, find and read documents on your PC, open files, set reminders (one-off or repeating), remember things you tell her, give you a spoken briefing every weekday morning (with weather and drive times), search the web, rewrite or summarise anything you copy, and draft emails in your style (on your clipboard and in Outlook Drafts).

**She can't** (by design): send emails (drafts only, you press Send), delete or move emails, edit, move or delete files, or click around your screen.

## What it costs

| Part | Cost |
| --- | --- |
| Voice: Microsoft natural voices (Sonia or Libby) | Free, needs internet |
| Backup voice (Piper), hearing (Whisper), wake word (openWakeWord) | Free, run on your PC |
| Outlook connection (Microsoft Graph) | Free with your Microsoft 365 |
| Weather (Open-Meteo), postcodes (postcodes.io), drive times (OpenStreetMap) | Free |
| Web search (Claude) | About 1p per search, switch off in Settings → Accounts |
| Brain: Claude Haiku 5.5 | Pay as you go, roughly £1–5 a month for one person |

There's a monthly spending cap (default $10) in Settings. Moo stops thinking if she hits it, so there are no surprise bills. The bottom of her window shows this month's spend.

## Install (about 15 minutes)

1. **Get a Claude API key.** Go to console.anthropic.com, sign up, add £5–10 of credit under Billing, then create an API key (it starts `sk-ant-`). This is separate from your normal Claude subscription.
2. **Unzip this folder** somewhere permanent, e.g. `C:\Moo`.
3. **Double-click `install.bat`.** It installs Python if needed, sets everything up, plays three British voices so you can pick one, asks for your API key (stored in Windows Credential Manager, not in a file), and puts **Moo** on your desktop.
4. **Double-click Moo.** The first start takes a minute or two while speech recognition downloads (about 500 MB). After that she starts in seconds.

## Connect Outlook (one email to your IT company)

Moo works without Outlook straight away (files, reminders, chat). For email and calendar, your IT company needs to register her in Microsoft Entra. Send them this:

> **Subject: App registration for my desktop assistant (read mail and calendar, save drafts)**
>
> Hi,
>
> Could you create an app registration in our Entra tenant for a personal assistant app I run on my PC? It needs to read my own mailbox and calendar, and save drafts in my Drafts folder. It has no permission to send email.
>
> - Name: Moo (personal assistant)
> - Supported account types: this organisational directory only (single tenant)
> - Platform: Mobile and desktop applications, redirect URI `http://localhost`
> - Authentication: Allow public client flows = Yes (no client secret needed)
> - API permissions (Microsoft Graph, delegated): `User.Read`, `Mail.ReadWrite`, `Calendars.Read`, then grant admin consent (no `Mail.Send`)
> - Optional: under Enterprise applications set "Assignment required" and assign it to me only
>
> Please send me the **Application (client) ID** and **Directory (tenant) ID**.
>
> Thanks,
> [Your name]

When they reply, open Moo → ⚙ Settings → paste both IDs → Save → **Connect Outlook now**. A normal Microsoft sign-in opens in your browser. You'll rarely need to sign in again.

## Connect Gmail and Google Calendar (5 minutes, no IT needed)

Moo reads your Gmail and can save drafts there. She never sends, deletes, or marks anything as read.

**Gmail**
1. Make sure **2-Step Verification** is on: myaccount.google.com/security.
2. Go to **myaccount.google.com/apppasswords**, type `Moo` as the name, click **Create**, and copy the 16-letter password.
3. In Moo: ⚙ → **Accounts** → enter your **Gmail address** and paste the **app password** → click **Test Gmail** → **Save**.

If the test fails, open Gmail → ⚙ → **See all settings** → **Forwarding and POP/IMAP**, and if there's an IMAP option, make sure it's **enabled**.

**Google Calendar (optional)**
1. Open **calendar.google.com** → ⚙ → **Settings**.
2. On the left, under **Settings for my calendars**, click your calendar, then scroll to **Integrate calendar**.
3. Copy **Secret address in iCal format** and paste it into Moo: ⚙ → **Accounts** → **Google Calendar secret address** → **Save**.

Moo then checks Outlook and Gmail together ("any important emails?"), or one of them if you say so ("anything in my Gmail?"). Replies are drafted in whichever account the email came from.

## Talking to Moo

Three ways: say the wake word, press **Ctrl+Alt+M** anywhere in Windows, or click the 🎙 button. Talk naturally; she stops listening when you pause. Speak over her (wake word or hotkey) to interrupt, or say "stop".

Things to try:

- "Good morning, what's my day looking like?"
- "Any important emails since yesterday?"
- "Find the latest email from that customer about the drainage job."
- "What have I got on Thursday?"
- "Find the CCTV survey report for St Chads Road and open it."
- "What does the quote in that file come to?"
- "Remind me at 3 to call Steve." / "Remind me every weekday at half 4 to do timesheets."
- "Remember that I like briefings under a minute." / "What do you remember about me?"
- *(copy some text first)* "Summarise this." / "Make this sound more professional." / "Turn this into quote wording."
- "Reply to Steve's email saying Thursday works." It goes on your clipboard and into your Outlook Drafts.
- "What's the weather doing tomorrow?" / "How long to drive to (a postcode)?"
- "Search the web for the price of 150mm twinwall pipe." / "Any closures on the M42 today?"

Set your **home postcode** and **email sign-off** in ⚙ → Personality so weather, drive times and drafts are right.

## The wake word: "Hey Jarvis" until you make "Hey Moo"

openWakeWord ships with "Hey Jarvis" ready-made, so that's the wake word to begin with (apt, really). To make it **"Hey Moo"**:

1. Open openWakeWord's free training notebook in Google Colab (linked from github.com/dscripka/openWakeWord, "Training New Models").
2. Enter `hey moo` as the phrase and run it. It takes around an hour and produces a file ending `.onnx`.
3. Rename it `hey_moo.onnx` and put it in `%APPDATA%\MooLite\models`, then restart Moo.

If "Hey Jarvis" triggers too easily or not enough, change `wake_word_threshold` in `%APPDATA%\MooLite\settings.json` (higher = stricter; 0.5 is the default).

## Customising Moo

Click **⚙** in her window. Everything applies when you click Save, with no reinstalling:

| Tab | What you can change |
| --- | --- |
| Voice | Pick from every English female voice (British first) with ▶ Hear it, plus pace, pitch, volume, clearer speech, and text-only mode |
| Look | Colour theme (Ocean, Aurora, Violet, Ember, Arctic, Graphite), text size, see-through window, always on top, her name |
| Personality | What she calls you, reply length, and her personality in plain English (e.g. "dry humour, call me boss"), plus morning briefing time |
| Listening | Wake word on or off, how sensitive it is, how long a pause means you've finished, the talk shortcut, speech recognition accuracy |
| Accounts | Claude key, brain (Haiku or Sonnet), spending cap, Outlook, Azure voices |
| System | Start with Windows, extra folders to search, open her folder, edit memories, clear the conversation |

### More British voices (free Azure account, optional)

The free Microsoft voices include two British women (Sonia and Libby) plus Irish, Australian, American and other accents. A free Azure Speech account adds roughly six more British voices, such as Hollie, Olivia, Abbi and Bella, in higher-quality audio. The free tier covers half a million characters a month, far more than Moo speaks.

1. Go to portal.azure.com and sign in or create a free account (it asks for a card for verification; the free tier isn't charged).
2. Search for **Speech service**, then **Create**. Set **Region** to **UK South** and **Pricing tier** to **Free F0**, then **Review + create**.
3. Open the new resource, then **Keys and Endpoint**, and copy **Key 1**.
4. In Moo: ⚙ → **Accounts** → paste it into **Azure Speech key**, leave region as `uksouth`, then **Save**. Reopen ⚙ → **Voice** and the extra voices appear, marked "Azure".

## Updates

Moo checks for a new version each time she starts. If there is one, she tells you and shows an **Install** button, or you can say "install the update". She downloads it, installs it and restarts herself. You can also say "check for updates" any time. Your settings, keys, memories and reminders are never touched by an update.

## Settings and your data

Everything Moo keeps is in `%APPDATA%\MooLite` (paste that into File Explorer's address bar):

| File | What it is |
| --- | --- |
| `settings.json` | All settings, including some not shown in the Settings window |
| `memory.json` | Things you've asked her to remember; edit or delete freely |
| `reminders.json` | Upcoming reminders |
| `history.json` | The last dozen exchanges, so follow-up questions work |
| `usage.json` | Spend per month |
| `moo.log` | Diagnostic log |

**Privacy:** your voice never leaves the PC; only the transcribed text goes to Claude, and Moo's replies go to Microsoft to be spoken. Emails and file contents Moo reads to answer a question are sent to Claude for that answer. Anthropic doesn't train on API data by default. Moo refuses to store anything that looks like a password or account number.

**Smarter brain:** in Settings, change the model to `claude-sonnet-5-5`. It's noticeably sharper with long email threads but costs about 20 times more per question (still only a few pounds a month at light use).

## If something's not right

- **Use "Start Moo (with log window).bat"** to see errors as they happen; the full log is `%APPDATA%\MooLite\moo.log`.
- **No voice:** run `install.bat` again; it re-downloads missing voice files.
- **She mishears you:** set `whisper_model` to `medium.en` in `settings.json` (needs the NVIDIA card; slower on CPU).
- **"Outlook refused access":** the IT company hasn't granted admin consent yet.
- **Drafts fail but reading works:** the IT company granted `Mail.Read` instead of `Mail.ReadWrite`. Either ask them to add it, or untick "save email drafts" in ⚙ → Accounts.
- **Updating to a new version:** close Moo, unzip the new version over this folder (choose Replace), then double-click `update.bat`.
