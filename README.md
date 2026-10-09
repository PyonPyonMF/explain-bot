# explain-bot

A Discord bot that makes short narrated explainer videos in a 3Blue1Brown-like style.

- `/explain query:<text> [messages:<0-50>] [private:true]`: explains a topic or a question. With `messages`, it also
  reads that many recent messages of the channel (for example: `/explain messages:20 query:кто тут прав?`).
  With `private`, only you see the video.
- Right-click a message → **Apps → Explain (video)**: explains that post.
- Right-click a message → **Apps → Ask about this (video)**: opens a form for your question, then explains the post.
- In Gateway mode, `@BigBro объясни градиентный спуск` makes a video directly in the channel.
  Reply to a post with `@BigBro кто тут прав?` to explain that post in context, including its images, links,
  up to four parent replies, and recent discussion. A plain mention explains the recent conversation.
  Pasted quotes and forwarded posts are also supported. The bot replies with progress, then attaches the video
  to that same reply; it ignores other bots and does not ping participants.

For a post, the bot reads: the text, images (up to 4), link previews, small text files (`.txt`, `.py`, …),
the pages behind links (up to 3), the message it replies to, and the 10 messages before it.

### What needs the bot token (optional)

Reading the replied-to message (if Discord does not include it) and earlier channel messages needs:
1. the bot is a member of the server (Installation → Guild Install → scopes `applications.commands` **and** `bot`,
   permissions *View Channels* and *Read Message History*);
2. Developer Portal → *Bot* → **Message Content Intent** on (no review needed below 100 servers);
3. secret `DISCORD_BOT_TOKEN` in the Worker.

Without these, everything else works; the bot explains the post without the conversation around it.
It can never read DMs between two people.

## How it works

```
Discord ──► Worker (src/index.ts)
              check Ed25519 signature → daily limit (KV) → reply "thinking…" (deferred, < 3 s)
              ctx.waitUntil: POST job to the container ──► returns 202 at once
                                     │
              Container (container/, Python, Cloudflare Containers)
                inputs.py   post text, images, linked pages, files, reply + earlier messages
                fullgen.py  (FULL_GEN=1, default)
                  1. Sonnet writes the narration plan AND a Python module that draws every frame (kit.py API)
                  2. ElevenLabs voices each scene (in parallel with 3–4)
                  3. gen_harness.py renders 2 test frames per scene as user `sandbox` (no API keys) and checks
                     them: crashes, text outside the frame / in the subtitle band, overlapping text, slow frames
                  4. Sonnet looks at the frames and returns fixed code (max REVIEW_ROUNDS rounds, 7 min budget)
                  5. full render, one scene per vCPU; a scene that crashes becomes a text card
                pipeline.py templates (fallback when the generated code never runs, or FULL_GEN=0)
                  12 fixed scene types filled from a JSON plan
                → PATCH the original Discord response with the MP4 (progress messages while it works)
```

Generated code runs as an unprivileged user without the API keys in its environment; it cannot read the
server's environment or write to /app. It still has network access.

## Standalone bot (no public HTTPS address)

The bot can connect directly to Discord over the Gateway. It needs only outbound internet access;
no domain, reverse proxy, public key, or open inbound ports. Slash commands and both message commands
work in servers where the bot is installed, and in DMs with the bot.

```bash
git clone https://github.com/PyonPyonMF/explain-bot && cd explain-bot
cp .env.example .env
# Fill DISCORD_BOT_TOKEN, ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID.
docker compose -f compose.gateway.yml up -d --build
```

In Discord Developer Portal, clear **General Information → Interactions Endpoint URL** if one was set.
Discord sends commands either to that URL or to the Gateway, never to both
([Discord documentation](https://docs.discord.com/developers/interactions/receiving-and-responding#receiving-an-interaction)).
Add the bot to your server with scopes `bot` and `applications.commands`, and permissions
*View Channels*, *Read Message History*, *Send Messages*, *Attach Files*, and *Send Messages in Threads*
where needed. Enable **Bot → Message Content Intent** for mentions, quoted posts, and conversation context.
Set `ENABLE_MENTIONS=0` to use only slash/context commands without requesting Message Content intent.
`MENTION_CONTEXT_MESSAGES` defaults to 10: replies include that many messages before the quoted post and
before the mention, with duplicates removed. Messages sent after the mention are not included.

Commands are registered automatically at startup; set `REGISTER_COMMANDS=0` to skip registration.
Run only one Gateway instance for this bot token. The renderer processes one video at a time, with
`MAX_QUEUED_JOBS` waiting jobs. `RENDER_WORKERS` defaults to 2 in this Compose configuration.

Logs: `docker compose -f compose.gateway.yml logs -f bot`.
Update: `git pull --ff-only && docker compose -f compose.gateway.yml up -d --build`.

## Self-hosting over HTTP (your own server, no Cloudflare)

One Docker container receives the Discord interactions (`POST /interactions`) and renders the videos.
No Worker, KV or Durable Objects; the daily limit is stored in `./data/bot.sqlite`.

Requirements: Linux x86_64, Docker with Compose, 2+ CPUs, 4+ GB RAM, and a public HTTPS address.

```bash
git clone https://github.com/PyonPyonMF/explain-bot && cd explain-bot
cp .env.example .env && nano .env          # keys, DISCORD_PUBLIC_KEY, DOMAIN
```

HTTPS, choose one:
- **Caddy (included):** a DNS A record for `DOMAIN` points to the server, ports 80 and 443 are open.
  `docker compose --profile caddy up -d --build` (Caddy gets a Let's Encrypt certificate itself).
- **Your own nginx / Traefik:** `docker compose up -d --build`, then proxy `https://your.domain/interactions`
  to `http://127.0.0.1:8080/interactions`. Expose only that path.
- **Cloudflare Tunnel:** run `cloudflared` to `http://localhost:8080`, public hostname path `/interactions`.

Then: Discord Developer Portal → *General Information* → *Interactions Endpoint URL* =
`https://your.domain/interactions` → *Save* (Discord sends a signed PING; the bot must already run).
Commands are registered the same way as for Cloudflare (`scripts/register-commands.sh`).

Logs: `docker compose logs -f bot`. Update: `git pull && docker compose up -d --build`.
After switching, the Cloudflare Worker receives nothing; you can delete it and cancel Workers Paid.

## Deploy with Workers Builds (no Docker on your computer)

Cloudflare builds the container image from `container/Dockerfile` on each push to `main`.

1. **Connect the repository.** Cloudflare dashboard → *Workers & Pages* → *Create* → *Import a repository* →
   select this repository. Keep the defaults: root directory `/`, deploy command `npx wrangler deploy`.
   The first build takes several minutes (it builds the image).
2. **Add secrets.** Worker `explain-bot` → *Settings* → *Variables and Secrets* → add, type *Secret*:

   | Name | Value |
   |---|---|
   | `DISCORD_PUBLIC_KEY` | Discord Developer Portal → your app → *General Information* → *Public Key* |
   | `ANTHROPIC_API_KEY` | Claude Console API key |
   | `ANTHROPIC_WORKSPACE_ID` | `wrkspc_…` only if the key is not scoped to a workspace; else leave it out |
   | `ELEVENLABS_API_KEY` | ElevenLabs API key |
   | `ELEVENLABS_VOICE_ID` | e.g. `xcVbjGhhLIoKi3va6mAe` (Viktoriya Voloshina, Russian female) |
   | `DISCORD_BOT_TOKEN` | optional, for conversation context (see above) |

3. **Discord app.** Developer Portal → your app:
   - *General Information* → *Interactions Endpoint URL* = `https://explain-bot.<your-subdomain>.workers.dev`.
     Discord sends a test request when you save; it fails until `DISCORD_PUBLIC_KEY` is set.
   - *Installation* → enable *User Install* and *Guild Install*, scope `applications.commands`.
4. **Register the commands** (once): *Bot* → *Reset Token*, then from any shell with curl:
   ```bash
   DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... ./scripts/register-commands.sh
   ```
   Any machine with curl works; the script installs nothing.
5. Install the app (Installation → *Install Link*) and run `/explain query:что такое градиентный спуск`.

## Settings (`wrangler.jsonc` → `vars`)

| Var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | model for the plan |
| `ELEVENLABS_MODEL` | `eleven_multilingual_v2` | TTS model |
| `DAILY_LIMIT` | `5` | videos per user per UTC day; `0` = no limit |
| `MAX_UPLOAD_MB` | `9.5` | target file size; larger videos are re-encoded |
| `BOT_LANG` | `ru` | language of the bot's own status messages (`ru`/`en`) |
| `RENDER_INSTANCES` | `2` | how many container instances share the jobs |
| `FULL_GEN` | `1` | `1` = generated animation with review; `0` = templates only (faster, cheaper) |
| `REVIEW_ROUNDS` | `2` | max review rounds (each: test frames → Sonnet with images → fixed code) |

Container size: `standard-3` (2 vCPU, 8 GiB). A full-gen job takes about 4–8 minutes: generation 1–2 min,
each review round 1–2 min, render 1–2 min. Discord allows 15 minutes.

## Cost per video (approximate)

- ElevenLabs: about 1 credit per character; narration is capped at 1600 characters.
- Sonnet (full gen): 1 generation call (about 8–12 k output tokens of code) + up to 2 review calls with
  images (about 15 k input + 8 k output each). Templates only: one call, about 10 k input + 2 k output.
- Container: about 1–2 vCPU-minutes. Workers Paid includes 375 vCPU-minutes per month.
  The instance stays up to 15 min after a job (`sleepAfter`), billed for memory while it runs.

## Local test (no API keys)

```bash
cd container && pip install -r requirements.txt
python test_local.py sample_plan.json out.mp4   # fake audio, real renderer
```

## Limits and known problems

- Discord's interaction token expires after 15 minutes; a job that takes longer cannot post its video.
- The KV daily limit is approximate (KV is eventually consistent).
- Linked pages: plain HTML only (no JavaScript sites); private and internal addresses are refused.
- Privacy: the post, its images and the conversation go to Anthropic and ElevenLabs.
- Fonts: DejaVu (Latin, Cyrillic, Greek). Chinese/Japanese/Korean text will show as boxes.
- Sonnet can still be wrong about facts. The renderer only guarantees that chart numbers match the formulas.
- If all container instances are busy, the bot replies "busy" (queue of 4 jobs per instance).
