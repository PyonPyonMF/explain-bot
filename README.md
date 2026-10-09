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

Videos can use actual image references instead of drawing every object with geometric primitives:
- Discord attachments are made available to the renderer as image assets.
- Wikimedia Commons supplies relevant photos/diagrams with source and license information.
- A host-side Codex CLI worker generates one detailed illustration when useful and can research source pages.
- Sonnet chooses the representation for the explanation: recognizable imagery for appearance/structure,
  sequences or animated diagrams for processes/phenomena, and computed plots for quantitative relationships.
  Frames are reviewed for explanatory accuracy and clarity; irrelevant decoration is not required.
Source links/credits accompany the finished video, and generated illustrations are labeled as such.

### Optional 3D classroom mode

Use `@BigBro 3d объясни, почему маятник качается` or `/explain query:... mode:3d`.
Replies/quoted posts keep their conversation context in 3D mode too. Normal requests stay in 2D.

Blender builds a Japanese school classroom with timber flooring, windows, a chalkboard, and wooden desks.
Lessons use perspective cameras: an eye-level presenter close-up, a close-up of the object, and readable
board inserts, with narrated scene changes and animated 3D demonstration objects.
The board can show text and the image references prepared for the explanation. The scene planner chooses
meaningful models/diagrams for the topic rather than treating every object as the same primitive.
For detailed real-world objects, add a GLB/GLTF model to the local catalog so the renderer can use its actual
geometry and materials instead of an approximation.

Place the teacher model at `models3d/teacher.vrm` on the server (or set `VRM_AVATAR_PATH` under `/models3d`).
The directory is mounted read-only and excluded from Git. VRM 1.0 and 0.x import through the VRM add-on;
the presenter is normalized to classroom scale, gets arm gestures, idle motion and basic mouth/blink
animation where the model's rig/expressions support them. Without a VRM file the scene renders without a
presenter. Creator credits from VRM metadata accompany the video. Respect the model's usage license.

Optional `models3d/catalog.json`:

```json
[
  {"id":"microscope", "file":"microscope.glb", "description":"Detailed optical microscope",
   "credit":"Model creator", "source_url":"https://example.com/model"}
]
```

### Reusable 3D object library

`/objects` (Russian UI: `/объекты`) lists stored objects. Use `query` / `поиск` to search by name, topic,
or Russian/English tags. An exact single match includes its preview. Browsing is private and does not
consume the video quota.

The bot searches this catalog before building a 3D lesson. New reusable objects declared by the scene
generator are captured only after the video renders successfully. Each entry stores a standalone GLB,
an object-only thumbnail, a description, tags, named components, attribution, and reuse count. The room,
avatar, board, lesson-specific text and executable lesson code are not included. Future lessons reuse the
geometry and animate its named parts with their own timing and narration.

The persistent `library3d/` directory is mounted separately from the read-only `models3d/` directory and
is excluded from Git. Only the trusted bot process updates the SQLite catalog and publishes assets;
the renderer can read published models but cannot modify them. Models are normalized to stand on Z=0,
centered on X/Y, with their largest dimension equal to one. Parent/pivot relationships are retained.

Identical files or equivalent geometry/material fingerprints are deduplicated. Changed objects receive
a new ID rather than overwriting a version already referenced by other lessons. Archiving hides an object
from future selection while preserving its files. Default limits: 500 objects and 512 MiB of model data;
reaching a library limit does not discard the video.

Operator commands:

```bash
docker compose -f compose.gateway.yml exec bot python -m object_library list маятник
docker compose -f compose.gateway.yml exec bot python -m object_library import /models3d/microscope.glb --id microscope --title "Microscope" --description "Optical microscope" --tags "microscope,микроскоп" --credit "Creator" --license "License" --source-url "https://example.com/model"
docker compose -f compose.gateway.yml exec bot python -m object_library archive microscope
docker compose -f compose.gateway.yml exec bot python -m object_library restore microscope
```

Imported library GLBs must embed their buffers/textures. The manual `models3d/catalog.json` catalog remains
supported. `OBJECT_LIBRARY_AUTO_SAVE=0` disables automatic additions; `OBJECT_LIBRARY_MAX_ASSETS` and
`OBJECT_LIBRARY_MAX_MB` set storage limits. Include `library3d/` when backing up the deployment.

The CPU profile uses Blender Workbench with textures, studio shading and FXAA, 960×540 at 8 rendered FPS
(encoded as 24 FPS). `THREED_ENGINE=BLENDER_EEVEE_NEXT` enables more advanced lighting at a higher render
cost. `THREED_FPS`, `THREED_WIDTH`, `THREED_HEIGHT`, `THREED_SAMPLES`, `THREED_MAX_SECONDS` and
`THREED_RENDER_TIMEOUT` are configurable. Lessons are intentionally short (two or three scenes). A 3D
failure is reported as a failure; it is not silently replaced by a 2D clip. Generated Blender code runs as
the unprivileged sandbox user with API credentials removed, with auto-execution from model files disabled.

### Optional Windows GPU worker

With `THREED_REMOTE=1`, an available personal computer renders 1080p Eevee scenes with 32 samples, soft
lighting and depth of field. The default is 12 rendered FPS, encoded at 24 FPS; `THREED_REMOTE_FPS` can be
increased to 24 at roughly twice the render time. A disconnected, busy, restarted or failed worker falls
back to the server's Workbench profile. The remote deadline is `THREED_REMOTE_TIMEOUT` (600 seconds).
The bot announces which renderer is being used. Only one local GPU worker should use this queue.

The server builds and bakes self-contained `.blend` files, including textures, gestures, lip movement,
object animation and camera framing. Windows runs the trusted renderer with `--disable-autoexec`; it does
not execute generated lesson Python and needs no VRM add-on or Discord/AI API credentials. It returns
silent H.264 clips; the server adds narration/subtitles and updates the object library. Job files are
removed afterwards. Logs contain job IDs, status and errors rather than message text.

Requirements: Blender 4.3+ or 5.x, Python 3.10+, Windows OpenSSH client, and Tailscale on both machines.
Generate a dedicated Ed25519 key under `%LOCALAPPDATA%\ExplainBotRender\worker_key` and copy **only its
public key** to codervm. On the server run:

```bash
sudo python3 scripts/install_render_transport.py /path/to/worker_key.pub
```

This installs a dedicated `bot-render` account and an SSH broker on port 2222 bound **only** to the server's
Tailscale address. It accepts the render protocol, with shell access, TTY and forwarding disabled. This
separate listener is necessary when Tailscale SSH owns port 22 and bypasses OpenSSH authorized-key rules.
Set the printed `THREED_WORKER_UID`/`THREED_WORKER_GID` in `.env`, set `THREED_REMOTE=1`, and rebuild the bot.
The private queue is mounted at `/render-queue`, separately from bot secrets and library storage.

On Windows, obtain `/etc/ssh/ssh_host_ed25519_key.pub` through an already trusted connection and save
`[codervm]:2222 <host-public-key>` in `%LOCALAPPDATA%\ExplainBotRender\known_hosts`. In the same directory,
create `config.json` (adjust Blender's path to the installed version):

```json
{
  "host": "bot-render@codervm", "port": 2222,
  "key": "C:/Users/user/AppData/Local/ExplainBotRender/worker_key",
  "known_hosts": "C:/Users/user/AppData/Local/ExplainBotRender/known_hosts",
  "blender": "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe",
  "work": "C:/Users/user/AppData/Local/ExplainBotRender/work"
}
```

Run `scripts/install_windows_worker.ps1`. The `ExplainBot-3D-Worker` scheduled task starts immediately
and at Windows login, without opening console windows. The computer must be awake, signed in and connected
to Tailscale. Use `Stop-ScheduledTask` / `Start-ScheduledTask -TaskName ExplainBot-3D-Worker` to pause/resume;
rerun the installer after worker code updates. Diagnostics: `work/worker.log`, `work/last-blender.log`, and
`journalctl -u explain-bot-render-sshd` on codervm. Do not commit the private key or local worker config.

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

### Codex illustration worker (Linux)

Install Codex CLI and sign in with ChatGPT under the account that will run the worker. Image generation uses
that account's Codex limits; no OpenAI API key is required. The supplied systemd unit uses account `coder` and
`/home/coder/.local/bin/codex`; adjust those paths/user/group for another host.

```bash
sudo install -d -o coder -g coder -m 700 /var/lib/explain-bot-visuals/queue
sudo cp deploy/explain-bot-visuals.service /etc/systemd/system/
# Optional non-secret overrides: CODEX_VISUAL_MODEL, CODEX_BIN in /etc/explain-bot-visuals.env
sudo systemctl daemon-reload
sudo systemctl enable --now explain-bot-visuals
docker compose -f compose.gateway.yml up -d --build
```

Set `CODEX_VISUAL_UID`/`CODEX_VISUAL_GID` in `.env` to the worker account's numeric UID/GID (`id coder`).
Only a private queue directory is mounted into the bot; Codex authentication stays on the host. The CLI runs
with shell tools, plugins, hooks, computer use, and subagents disabled. It receives a constrained illustration
brief and up to two visual references. It has a 210-second deadline. Per-job files are deleted after delivery;
abandoned results are cleaned up after one hour.

`VISUAL_ASSETS=0` disables visual preparation, `WEB_VISUALS=0` disables Commons image lookup, and
`CODEX_IMAGES=0` disables generated illustrations. Missing/unavailable image services fall back to existing
references and the normal renderer. Use `journalctl -u explain-bot-visuals` for worker status.

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
