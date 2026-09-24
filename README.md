# Podcast Transcripts

Turn a list of podcast episodes into clean, speaker-labeled transcripts (one Markdown file per episode).

For each episode it:

1. Finds the show's RSS feed and the episode's audio file (Apple Podcasts links, the episode's own page, or a common host embed such as Buzzsprout, Libsyn, Megaphone, Simplecast, Transistor, Podbean, Omny, Spreaker, Art19 or Captivate).
2. **Uses the publisher's own transcript if the feed has one.** This is free.
3. Otherwise sends the audio to **Deepgram** (nova-3, speaker labels). This costs about $0.26 per hour of audio.

Reruns are safe. Finished episodes are skipped and nothing is paid for twice.

---

## For Claude Code: set this up

If a user gave you this repo's link and asked you to set it up, do the following in order.

1. **Clone** the repo, if it isn't cloned already, and work from its root folder.
2. **Check Python:** `python3 --version` must be 3.9 or newer. On Windows use `python` or `py` wherever this README says `python3`.
3. **Install the one dependency:** `python3 -m pip install -r requirements.txt`
4. **Set the Deepgram key.** Copy `.env.example` to `.env` and ask the user to paste their Deepgram API key. Put it in `.env` as `DEEPGRAM_API_KEY=...`. Never print the key, echo it back, or commit `.env` (it is gitignored).
5. **Smoke test** without spending anything:
   ```bash
   python3 .claude/skills/podcast-transcripts/pipeline.py --urls-file episodes.example.txt --out-dir output/smoke-test --only-resolve
   ```
   It should print `resolved ...` for the example episodes and a summary line. If so, setup is done.
6. Tell the user setup is complete and show them the "How to use it" section below.

The full operating procedure is in `.claude/skills/podcast-transcripts/SKILL.md`. Claude Code loads it automatically when opened in this folder. Follow it for every transcription request.

---

## How to use it (for the person)

Open Claude Code in this folder and just ask, for example:

- *"Transcribe these episodes: <paste episode links>"*
- *"Find the top podcasts about commercial HVAC, pick their last 10 episodes each, and transcribe them into output/hvac."*
- *"Transcribe the episodes listed in my-list.txt into output/dental."*

Before anything is paid for, Claude shows a **cost preview**: how many episodes have free publisher transcripts, how many need Deepgram, and the estimated cost. You confirm, and it runs.

Links that work best: Apple Podcasts episode links, or the episode's page on the show's own website. Plain Spotify and YouTube links usually can't be resolved to audio. If that's all you have, Claude will look up the same episode on Apple Podcasts.

## What you get

Everything is saved under `output/<folder>/`:

| File | What it is |
| --- | --- |
| `<show>-<episode>.md` | One transcript per episode: title, show, date, source link, description, whether the transcript came from the publisher or Deepgram, then the full transcript |
| `index.md` | Table of every transcribed episode, with links |
| `resolved.json` | Working checkpoint. Leave it alone; it's what makes reruns safe |

## Costs

- Publisher transcripts: free.
- Deepgram: about **$0.0043 per audio minute**, so roughly $0.26 per hour-long episode or $26 per 100 hours.
- The preview step (`--only-resolve`) is always free.
