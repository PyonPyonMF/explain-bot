# explain-bot

A Discord bot that makes short narrated explainer videos in a 3Blue1Brown-like style.

- `/explain query:<text>`: explains a topic or a question.
- Right-click a message → **Apps → Explain (video)**: explains that post. Discord sends the message text with the
  command, so the bot does not need the privileged Message Content intent.

## How it works

```
Discord ──► Worker (src/index.ts)
              check Ed25519 signature → daily limit (KV) → reply "thinking…" (deferred, < 3 s)
              ctx.waitUntil: POST job to the container ──► returns 202 at once
                                     │
              Container (container/, Python, Cloudflare Containers)
                1. Sonnet (claude-sonnet-5-5): forced tool call → JSON scene plan
                2. plan.py: strict validation; every chart number is computed by code, not by the model
                3. ElevenLabs: one MP3 per scene (previous_text/next_text keep the voice continuous)
                4. scenes.py: matplotlib frames → ffmpeg, each scene as long as its voice clip
                5. PATCH the original Discord response with the MP4 (interaction token, valid 15 min)
```

Scene types: `title, statement, bullets, flow, compare, formula, plot, gradient_descent, neuron, vectors, bars, summary`.
Sonnet only chooses scenes and writes text. It cannot run code, so a bad plan cannot break the container.

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

Container size: `standard-2` (1 vCPU, 6 GiB). One vCPU renders about 1 second of video per second
(tested: 48 s of video in 48 s). Change `instance_type` to `standard-3` for 2× speed.

## Cost per video (approximate)

- ElevenLabs: about 1 credit per character; narration is capped at 1600 characters.
- Sonnet: one call, about 10 k input + 2 k output tokens.
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
- Fonts: DejaVu (Latin, Cyrillic, Greek). Chinese/Japanese/Korean text will show as boxes.
- Sonnet can still be wrong about facts. The renderer only guarantees that chart numbers match the formulas.
- If all container instances are busy, the bot replies "busy" (queue of 4 jobs per instance).
