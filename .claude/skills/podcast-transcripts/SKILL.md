---
name: podcast-transcripts
description: 'Podcast episode page URLs -> direct audio via Apple/RSS resolution -> Deepgram nova-3 transcripts as speaker-labeled .md -> public Google Drive folder.'
---

# Podcast transcripts

## SOP

Run these commands in order from the repository root:

1. Resolve only (optional preflight):

   `python3 .claude/skills/podcast-transcripts/pipeline.py --urls-file urls.txt --out-dir podcast-output --only-resolve`

2. Resolve, transcribe, and write markdown:

   `python3 .claude/skills/podcast-transcripts/pipeline.py --urls-file urls.txt --out-dir podcast-output`

3. Make the markdown folder public in Drive and upload/update every `.md` file:

   `python3 .claude/skills/podcast-transcripts/drive_upload.py --dir podcast-output --folder-name 'Automotive Podcasts'`

Resolution uses the first successful strategy in this order: direct audio URL in the page HTML; an Apple episode link and its collection feed; an Apple show link matched by episode title; then known embeds (Podbean player, Buzzsprout feed, or an RSS link for Libsyn, Megaphone, Simplecast, Transistor, Omny, Spreaker, Art19, or Captivate). RSS episode titles are matched with a 0.6 fuzzy-title threshold and then page-slug words.

`resolved.json` and existing episode `.md` files are checkpoints. Reruns reuse completed resolution checkpoints and skip a URL whose output markdown already exists, logging `cached`.

`DEEPGRAM_API_KEY` is read from the repository-root `.env`; it does not need to be exported. The key needs no billing scope.

| Problem | Troubleshooting |
| --- | --- |
| Page blocked | Try a browser UA / TinyFish. |
| No Apple link | Check the known-host embed strategies. |
| Low title-match ratio | Pass the correct title manually in `resolved.json` and rerun. |
| Site's own RSS link is broken (e.g. a WordPress `/feed/` that 500s) | Add the domain to `DOMAIN_FEED_OVERRIDES` in `pipeline.py`; find the real feed with `https://itunes.apple.com/search?term=<show name>&entity=podcast`. Overrides win over the page's declared RSS link. |
| Deepgram 400 on a URL that looks fine | Some hosts (Buzzsprout especially) intermittently refuse Deepgram's URL fetcher. `transcribe_audio` already falls back to downloading the file and POSTing the bytes — a rerun picks these up via checkpoints. |
| Page embeds only a YouTube player | The show usually still has an audio feed — search iTunes for the show name and use a domain override rather than yt-dlp (YouTube blocks this VPS with a bot check). |
