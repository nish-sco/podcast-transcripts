---
name: podcast-transcripts
description: 'Podcast episode links -> transcripts as Markdown. Uses the publisher''s free transcript from the RSS feed when one exists, otherwise Deepgram nova-3 with speaker labels. Use for "transcribe these podcasts/episodes", "get transcripts for <show>", or industry podcast research.'
---

# Podcast transcripts

All commands run from the repository root. Use `python` or `py` instead of `python3` on Windows.

## SOP

1. **Collect episode links** into `output/<topic>/urls.txt`, one URL per line (`#` comments are allowed).
   - If the user gives shows or a topic rather than episode links, research them and pick the episodes. Prefer Apple Podcasts *episode* links (`podcasts.apple.com/.../id<show>?i=<episode>`) or the episode's page on the show's own site.
   - Spotify and YouTube links usually can't be resolved to audio. Find the same episode on Apple Podcasts.
   - Say how many episodes you picked and why before moving on.

2. **Preview (free).** This resolves every link and prints what it will cost:

   `python3 .claude/skills/podcast-transcripts/pipeline.py --urls-file output/<topic>/urls.txt --out-dir output/<topic> --only-resolve`

   Report the summary line to the user: resolved or failed counts, how many publisher transcripts are free, and how many Deepgram hours at what estimated cost. Fix failed links before continuing (see Troubleshooting).

3. **Confirm the cost with the user**, then transcribe:

   `python3 .claude/skills/podcast-transcripts/pipeline.py --urls-file output/<topic>/urls.txt --out-dir output/<topic>`

   Publisher transcripts are fetched first. If one is missing or unusable, the episode falls back to Deepgram automatically.

4. **Report:** episodes transcribed, split between publisher and Deepgram, any failures with their reasons, and the path to `output/<topic>/index.md`.

## Behaviour worth knowing

- **Checkpoints:** `resolved.json` and existing episode `.md` files are checkpoints. A rerun skips finished episodes (logged as `cached`) and never pays for them twice. To retry failures, rerun the same command.
- **Resolution order:** direct audio URL in the page HTML, then the Apple episode link and its show feed, then the Apple show link matched by episode title, then known host embeds (Podbean, Buzzsprout, Libsyn, Megaphone, Simplecast, Transistor, Omny, Spreaker, Art19, Captivate). Feed titles are matched fuzzily at a 0.6 threshold, then by words in the page slug.
- **Deepgram key:** `DEEPGRAM_API_KEY` is read from the environment or from the repo-root `.env`. It is only needed for episodes with no usable publisher transcript.
- **Parallelism:** 4 transcriptions run at once. An hour-long episode takes about 1–2 minutes on Deepgram.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `DEEPGRAM_API_KEY not set` | Add `DEEPGRAM_API_KEY=...` to `.env` in the repo root. Ask the user for the key and never print it. |
| Page blocked (403 or timeout) | Use the Apple Podcasts link for the same episode instead. |
| "no direct audio URL, Apple link, or known host embed" | Find the episode on Apple Podcasts (`https://itunes.apple.com/search?term=<show name>&entity=podcast`) and use that link. |
| Wrong episode matched (low title-match ratio) | Use the exact Apple episode link (`?i=` form), or fix the title in `resolved.json` and rerun. |
| The site's own RSS link is broken (e.g. a WordPress `/feed/` that returns 500) | Add the domain to `DOMAIN_FEED_OVERRIDES` in `pipeline.py` with the real feed URL from the iTunes search above. Overrides win over the page's declared RSS link. |
| Deepgram 400 on a URL that looks fine | Some hosts (Buzzsprout especially) refuse Deepgram's fetcher. The script already falls back to downloading the file and uploading the bytes, and a rerun picks these up. |
| Page embeds only a YouTube player | The show almost always has an audio feed too. Search iTunes for the show name and use its Apple link or a domain override. |
