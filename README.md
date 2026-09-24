# Podcast Transcripts

A Claude Code skill that turns podcast episode links into clean, speaker-labeled transcripts, one Markdown file per episode. It's built for research: pick an industry, collect its podcast episodes, and get every conversation as text that's ready to read, search, or summarize.

## What it does

- **Takes episode links.** Apple Podcasts episode links work best. Episode pages on a show's own website also work.
- **Uses free transcripts first.** Many shows publish a transcript in their podcast feed. When the skill finds one, it uses that instead of paying for transcription.
- **Transcribes the rest with Deepgram.** Episodes without a published transcript are sent to Deepgram's nova-3 model, which labels speakers. It costs about $0.26 per hour of audio.
- **Shows the cost before spending anything.** A free preview reports how many episodes have a publisher transcript, how many need Deepgram, and the estimated cost. It's an estimate: a publisher transcript that turns out to be unusable falls back to Deepgram.
- **Doesn't redo finished work.** Episodes that already have a transcript file are skipped on reruns, so an interrupted batch can be restarted safely.

## How it works

```
episode links
     │
     ▼
1. Find the episode ── Apple Podcasts lookup → the show's RSS feed → match the episode
     │                 (or: audio link on the page, or a known host player such as
     │                  Buzzsprout, Libsyn, Megaphone, Simplecast, Transistor, Podbean,
     │                  Omny, Spreaker, Art19, Captivate)
     ▼
2. Preview ─────────── publisher transcript available?  length of each episode?
     │                 → "3 free, 7 need Deepgram (~6.5 h, est $1.68)"
     ▼
3. Transcribe ──────── publisher transcript (free) ──✗ missing or unusable──► Deepgram
     │
     ▼
output/<topic>/  one .md per episode + index.md
```

1. **Find the episode.** For Apple Podcasts links and most show websites, the skill finds the show's RSS feed and the matching episode in it. The feed provides the audio file, the episode length, and, when the publisher offers one, a transcript link using the Podcasting 2.0 `<podcast:transcript>` tag. Some pages only expose an audio file and no feed. Those go straight to Deepgram, and their length (and so their cost) is unknown in the preview. Use the Apple Podcasts link when you can.
2. **Preview.** Resolving costs nothing, so the skill always runs this step first and reports the split between free and paid episodes.
3. **Transcribe.** It downloads the publisher transcript (VTT, SRT, JSON, HTML, or plain text) and converts it to readable paragraphs. Speaker names are kept when the publisher's file includes them. If there isn't one, or it's too short to be real, the audio goes to Deepgram. Four episodes run at a time, and an hour-long episode takes about 1–2 minutes.

Each transcript file lists the episode title, show, publish date, source link, audio link, description, and where the transcript came from (`publisher` or `deepgram`), followed by the full text.

## What's in this repo

| Path | Purpose |
| --- | --- |
| `.claude/skills/podcast-transcripts/SKILL.md` | The skill: the procedure Claude Code follows for every transcription request |
| `.claude/skills/podcast-transcripts/pipeline.py` | The script that does the work (Python 3.9+, one dependency: `requests`) |
| `.env.example` | Template for your Deepgram API key |
| `episodes.example.txt` | Three sample episodes used for the setup check |

---

## For Claude Code: set this up

If a user gave you this repo's link and asked you to set it up, do the following in order.

1. **Clone** the repo, if it isn't cloned already, and work from its root folder.
2. **Check Python:** `python3 --version` must be 3.9 or newer. On Windows use `python` or `py` wherever this README says `python3`.
3. **Install the one dependency:** `python3 -m pip install -r requirements.txt`
4. **Set the Deepgram key.** Copy `.env.example` to `.env` and ask the user to paste their Deepgram API key (they get it from whoever manages the team's Deepgram account, or from https://console.deepgram.com). Put it in `.env` as `DEEPGRAM_API_KEY=...`. Never print the key, echo it back, or commit `.env` (it is gitignored).
5. **Smoke test** without spending anything:
   ```bash
   python3 .claude/skills/podcast-transcripts/pipeline.py --urls-file episodes.example.txt --out-dir output/smoke-test --only-resolve
   ```
   It should print `resolved ...` for the example episodes and a summary line.
6. **Check the Deepgram key** (free call, prints only the HTTP status and never the key):
   ```bash
   python3 -c "import os,requests;k=[l.split('=',1)[1].strip().strip('\"\'') for l in open('.env') if l.startswith('DEEPGRAM_API_KEY=')][0];print(requests.get('https://api.deepgram.com/v1/projects',headers={'Authorization':'Token '+k}).status_code)"
   ```
   `200` means the key works. `401` means the key is wrong, so ask the user to check it.
7. Tell the user setup is complete and show them the "How to use it" section below.

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
| `<episode-title>.md` | One transcript per episode: title, show, date, source and audio links, description, whether the transcript came from the publisher or Deepgram, then the full transcript |
| `index.md` | Table of every transcribed episode, with links |
| `resolved.json` | Working checkpoint. Leave it alone; it's what makes reruns safe |

## Costs

- Publisher transcripts: free.
- Deepgram: about **$0.0043 per audio minute**, so roughly $0.26 per hour-long episode or $26 per 100 hours.
- The preview step (`--only-resolve`) is always free.
