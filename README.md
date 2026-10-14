# explain-bot

A Discord bot that makes short narrated explainer videos in a 3Blue1Brown-like style.

- `/help` (Russian UI: `/помощь`) shows a private feature overview with a `section` / «раздел» selector:
  text/transcripts/summaries, video/audio, files/links, commands/context, 3D/library and limits.
  In channel chat, mention the bot with `помощь`, `help`, `что ты умеешь` or a topic such as `помощь видео`.
  Help is immediate, calls no models, consumes no generation quota and works even when the render queue is full.
- Vague requests such as `разбери это`, `сделай что-нибудь`, a bare mention, or conflicting choices such as
  `текстом или видео` open an interactive choice card. `выбор` explicitly requests the card. Pick a video or text
  explanation, 3D, and (when a media source is plausible) a transcript or summary. **Edit request** opens a modal;
  submitting it updates the card before you choose an action. **Cancel** discards the pending request.
  Explicit commands, concrete questions/topics and bare source links/attachments retain their existing defaults.
  Cards start no transcription/rendering and consume no quota. Only the original requester can choose, in the
  original channel, once. The source snapshot is retained across restarts for 15 minutes, without interaction
  tokens. Busy queues keep the choice available; expiry asks the user to resend. `CLARIFY_AMBIGUOUS=0` disables
  automatic cards. Detection is a conservative set of request patterns, not a guarantee that every ambiguous
  natural-language request will be recognized; use `выбор` to request the controls explicitly.
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
  Reaction GIFs (Discord's Klipy picker, Tenor, Giphy, direct or Discord-proxied GIF/mp4 links, uploads), animated
  stickers and videos are shown to the model as one storyboard of six numbered frames (with timestamps for video), together with the
  GIF's title and tags, so "@BigBro что он имел ввиду?" explains what the GIF shows and what it answers in the
  discussion. Pictures and GIFs from the two latest media posts of the recent discussion are included as well.
- Reply to a bot video with `@BigBro` and a follow-up question to receive another narrated video with a short
  text excerpt of the answer. A reply
  without the explicit mention is ignored. Each explanation keeps its own source snapshot, narration or text, and up to
  eight turns, scoped to that Discord channel and reply branch. Public mention replies are retained in the
  existing database for up to 30 days (the latest 100 messages, including continuations, at most 2 MB per record); interaction tokens,
  API credentials and generated animation code are not stored. For older videos without saved narration,
  the bot tries to recover the original request and clearly marks that narration unavailable to the model.
- Add `объясни текстом` (also `обьясни текстом` or `ответь текстом`) to any request to receive a text answer
  without video generation, TTS or generated illustrations. This works for new questions and follow-ups.
  Ordinary commands such as `дай рецепт текстом`, `опиши картинку текстом` and `составь план текстом` work too.
  Optional provenance or a more exact variant does not block a useful answer to the actual request: for example,
  a suggested recipe from a food image does not require an episode number unless the user asks for the exact original.
  Bare topics and `A vs B` normally request an explanation or comparison of meanings and differences. Unrelated
  recent chat does not turn ordinary expressions into unidentified game factions or require a server name.
  An unsearched `ambiguous`/`not_found` assessment gets one bounded reconsideration before asking for context;
  unfamiliar public terms can be looked up, while genuinely missing source details still require clarification.
  The explicit bot mention is still required. Long text is delivered in multiple messages without cutting
  off the answer. Replies to any part retain the same context. Each new request selects its own format:
  without an explicit text request, the bot makes a video again. Text requests have a separate worker so
  they do not wait for an active video render, while sharing the total queue and daily request limit.
- Reply to a video/audio message with the bot mention and `Сделай транскрипцию видео текстом` (also
  `расшифруй видео`, `transcribe video`) to receive the recognized speech with timestamps and anonymous
  speaker labels. The bot returns the transcript directly without an LLM rewriting or shortening it.
- `summarize видео` (also `сделай резюме видео`, `суммаризируй видео`) transcribes the speech internally and
  returns a short text summary: the main point and key takeaways. The transcript is not included in this reply.
  Both commands always select text output and skip video generation and web research. They work with a new
  media attachment or a follow-up using the retained source transcript. Missing speech is reported instead of
  substituting a summary of the thumbnail or surrounding chat. The existing speech size/duration limits apply.

Videos can use actual image references instead of drawing every object with geometric primitives:
- When explaining an existing screenshot, diagram, chart or photo, the bot uses a dedicated 720×1280 (9:16)
  walkthrough: a brief overview, smooth zoom/pan to the relevant region, an outline and one or two short
  arrow-linked callouts. The source keeps its proportions. Labels are placed away from the highlighted
  details and the large two-line subtitles; the image is the main visual rather than a small card beside
  a sidebar. A blurred source backdrop fills the portrait frame during wide overviews. Scene focus and
  callouts are reviewed against the original image before encoding.
  Native source pixels are used for close-ups when available; the smaller vision-API thumbnails do not
  determine render quality. Follow-up memory retains bounded native copies within its existing 2 MB limit.
  New simulations/diagrams can still use the animation renderer; explicit 3D and text requests keep their own
  modes. `IMAGE_WALKTHROUGH=0` disables
  the automatic image-walkthrough route.
- An explicitly attached PDF is the primary subject: the bot reads its text and up to four labeled page previews.
  Limits are 10 MiB, 60 pages and 120,000 extracted characters. Unrelated chat text and images are excluded for
  document requests. A bare mention with an attachment explains the attachment. If the file cannot be read
  (including a scan without a text layer), the bot replies with the reason and does not generate a video.
- Discord attachments are made available to the renderer as image assets.
- Shared links are read as content, not as URLs: X/Twitter posts and full X Articles (long-reads with their
  headings, lists, code and images; also via fixvx/fxtwitter/vxtwitter/fixupx/
  nitter links, through the FxTwitter API with vxTwitter as a fallback) including quoted posts, photos, video
  preview frames, polls and community notes; Telegram posts; Reddit posts with top comments; YouTube speech
  (visual samples only on explicit request); and for other pages the article body plus preview image. JavaScript-only pages
  are retried the way Discord's link-preview crawler sees them.
- X thread links use FxTwitter's v2 thread reader, including links to the middle of a thread. The author's
  posts are deduplicated and ordered, with quotes attributed separately. Up to 80 posts, 80,000 characters
  and 12 visual inputs are read; downloadable videos become numbered storyboards. Direct X links exclude unrelated
  Discord history. Missing posts, read limits and unavailable media are disclosed under the resulting video;
  a single-post fallback is never presented as a complete thread.
- Reference pictures are searched on the web: Wikimedia Commons and Openverse (openly licensed, no key needed),
  plus optional whole-web search through Brave Search (`BRAVE_SEARCH_API_KEY`) or your own SearXNG
  (`SEARXNG_URL`). Sonnet looks at the candidates and keeps only images that actually show the subject,
  describing each one for the scene writer.
- Before the script is written, Sonnet checks the facts the explanation relies on with Anthropic's server-side
  web search (`web_search` / `web_fetch` tools, same API key), before visual preparation. Up to six
  findings reach the script with their sources; a finding is dropped unless its URL actually came back from
  the search tools. The sources are listed under the video. `WEB_RESEARCH=0` turns this off;
  `RESEARCH_MAX_SEARCHES` caps the billed searches per video. Web search must be allowed for the organization
  in the Anthropic Console. Research resolves the current question before visual planning. Public forum/gaming
  handles explicitly named in the request can be researched, while incidental chat participants, private contact
  details and private URLs stay out of search queries.
- For public forums such as `forum.ss13.ru`, the research agent can use the forum's own post/member search,
  open topic pages and follow pagination. The IP.Board reader preserves each post's author, date, quotations,
  current signature and member title separately; forum opinions and signatures are not treated as verified
  historical facts. Ordinary research reads at most ten forum pages, with per-page text and byte bounds.
- **Deep forum research:** `/research` (Russian `/исследование`) takes a question plus public source URL and
  returns a narrated **video** by default. Mention commands `рисерч: ...`, `research: ...`, and a request containing
  `изучи` plus a forum URL also select it. Example: `рисерч: почему закрылся сервер и как на это повлияла группа?
  Изучи https://forum.ss13.ru/`. It searches related topics and spelling variants, follows discussion pages,
  compares contemporary accounts and recollections, and tests the question's premises. Reports cover the direct
  answer, public actors, chronology, causes/competing accounts, requested relationships and unresolved gaps.
  This detailed dossier stays internal and feeds the video, without a second shallow search. The investigation
  video uses up to 10 scenes and 3200 narration characters (typically 2–4 minutes), with at most three source
  links in the caption. Explicit `рисерч текстом: ...`, `explain in text`, or `/research format:text` selects
  the full written report instead. Mention, slash-command and button entry points share these defaults.
  Factual paragraphs cite IDs assigned to **opened** source posts/pages; search snippets alone are not evidence.
  Allegations and inferences remain labeled in the video. No unrelated chat-media downloads or ASR occur.
  Progress updates replace the pending message. Research bounds: 600 seconds including dossier writing, 48 forum
  page requests, 12 web searches, 8 web fetches, 22 tool rounds and 180,000 source characters. Configure
  `DEEP_RESEARCH_TIMEOUT` (120–720), `DEEP_RESEARCH_MAX_PAGES` (8–80), `DEEP_RESEARCH_MAX_SEARCHES` (2–20).
  The run stops when enough evidence has been examined, repeated attempts yield no new evidence, or a bound is
  reached. Voice generation and rendering follow research. Partial evidence is narrated with scope/gaps; the bot
  never claims to have exhausted an entire archive. No usable evidence or a failed synthesis produces only a
  brief notice, not a long report or fabricated video.
  `/research private:true` keeps the result private. One accepted run uses one normal daily quota unit, but its
  multi-round model/search calls can cost more than an ordinary explanation. `WEB_RESEARCH=0` disables it too.
  Only source domains named by the user or actually observed in public search results are eligible; access
  restrictions and rate limits are respected. Other forum engines can use site-scoped web search; native search
  requires a supported public form or link. An empty search never proves that information does not exist.
  In ordinary (non-deep) research, if the requested entity/cause remains unresolved, the bot gives the supported part briefly and asks for the
  missing context with an edit/cancel card instead of rendering a video about the question itself. Clarifying
  the same request does not charge its quota twice. Explicit questions are not replaced by unrelated recent GIFs.
- Questions about a chat reaction (including a reaction to another reply) use an explicit message/reply map,
  keeping the target, ancestors and neighboring context separate. Available server nicknames and account aliases
  are retained. These explanations interpret the supplied exchange without searching private participants or
  their quotations. They keep the requested text/video format and use conversational animation rather than an
  unrelated image walkthrough. Wording and interpretation are distinguished: unverified feelings, motives or
  diagnostic labels do not become facts, and an unprovable side allegation does not block the supported answer.
  Missing-source clarification previews end at sentence/word boundaries.
- A host-side Codex CLI worker generates one detailed illustration when useful and can research source pages.
- Sonnet chooses the representation for the explanation: recognizable imagery for appearance/structure,
  sequences or animated diagrams for processes/phenomena, and computed plots for quantitative relationships.
  Frames are reviewed for explanatory accuracy and clarity; irrelevant decoration is not required.
Source links/credits accompany the finished video, and generated illustrations are labeled as such.

Speech in supplied Discord videos, voice messages/audio files, direct media URLs and downloadable X clips
is transcribed by ElevenLabs Scribe v2 using the existing `ELEVENLABS_API_KEY`. Timestamped turns and anonymous
speaker labels accompany the video frames, and are reused by follow-up memory. No transcription is triggered
for unrelated media picked up from recent chat. Audio is decoded locally to a single 16 kHz track before upload;
temporary source/audio files are removed after processing. Supported media downloads are capped at 50 MiB,
with at most two clips transcribed per request and up to 120 minutes of speech in five-minute chunks. Duplicate media
within a request is transcribed once. Missing audio, failures and truncated speech are distinguished in the
model input; failures and limits are also disclosed to the user. Speech recognition is separately billed by ElevenLabs. `SPEECH_RECOGNITION=0` disables it,
`SPEECH_MAX_SECONDS` changes the per-clip cap (up to 7200), and `ELEVENLABS_STT_MODEL` selects the Scribe model.

`STT_PROVIDER` and `TTS_PROVIDER` accept `auto` (default), `elevenlabs`, or `local`. With a configured private
speech worker, `auto` falls back to local Whisper/Piper on provider quota/authentication/network failure.
Quota/authentication failures open a shared 24-hour circuit for both services, persisted across bot restarts;
delete `/data/speech-provider.json` and restart the bot after restoring the provider quota to retry immediately.
`local` always skips ElevenLabs. A successful fallback continues the original job and caches completed speech
parts. Local Whisper provides timestamps but does **not** diarize speakers. Piper uses separate Russian and
English voices (selected by Cyrillic text), and sounds different from the configured ElevenLabs voice.

The optional worker uses `scripts/requirements-speech.txt` in a separate Python 3.11+ environment; it loads
downloaded model files only and listens on `127.0.0.1:8766`. `scripts/speech_worker.py --config CONFIG` expects
`token`, `whisper_model` (local faster-whisper model directory), and `voices` (`ru`/`en` ONNX file paths).
`scripts/speech_supervisor.py` restarts the worker and a private SSH reverse Unix-socket tunnel, requiring
`ssh_host`, `ssh_key`, `ssh_port`, `known_hosts`, and `remote_socket` in the same private config. On Windows,
run the supervisor with pythonw in a logon task. Restrict the config/key directory to the owning user and SYSTEM.
The deployed Windows service uses CPU Whisper small/int8 and Piper Irina/ Lessac medium; models and inference
stay on the home computer, which must be on and connected. The bot accesses only a bearer-protected Unix socket
mounted at `/speech-socket`: set `LOCAL_SPEECH_SOCKET=/speech-socket/speech.sock` and the matching
`LOCAL_SPEECH_TOKEN`. No speech HTTP port is publicly exposed. Model cards stay alongside downloaded voices.

X/Twitter video posts also use audio first: prefer the separate audio rendition from a completed HLS playlist,
fetch segments from `video.twimg.com` with bounded downloads, then decode local files only. If no audio rendition
exists, a bounded MP4 download supplies the speech track. Post text is retained; video previews are not analyzed.
Explicit `покадрово` requests add up to 24 sampled frames with timestamps, as for YouTube. The linked post gets
priority over other videos in its thread. Completed ASR parts can resume on retry; partial speech is identified
and never presented as a complete summary. The shared cap is 120 minutes and two recordings per request.

YouTube URLs use **audio first**: `yt-dlp` downloads only the audio track, local FFmpeg decodes five-minute
16 kHz FLAC chunks, and Scribe returns timestamped speech for the full permitted recording. `summarize URL`,
`сделай транскрипцию URL`, and `разбери URL` use this path. No video track, thumbnail or ambient chat image is
analyzed by default. Failed downloads never fall back to guessing the contents from metadata.
`разбери покадрово 00:10–00:20 URL` or `analyze frame-by-frame ...` separately opts into video downloading and
up to 24 timestamped frames from that range (roughly two per second for short ranges). Without a range, the
bounded samples span the recording. This is a sampled visual analysis, not a guarantee that every frame was
seen. Negative instructions and quoted command text do not enable it. The default summary remains speech-only;
an explicit visual request adds only the supplied frames to it. A follow-up can request frames of the original
YouTube or X link without resending it. Ordinary explanation/research output remains video unless text is requested.

`compose.gateway.yml` self-hosts `bgutil-ytdlp-pot-provider:2.0.2` as the `pot` service, reachable only on the
Compose network at `http://pot:4416`; it publishes **no host port**. The bot image includes pinned yt-dlp,
its EJS scripts and Deno. Browser cookies/account credentials are not imported, and the downloader subprocess
does not receive Discord, Anthropic or ElevenLabs credentials. Direct/live/private/unavailable media limits
are reported; PO tokens do not guarantee that YouTube accepts every request.

YouTube defaults: up to 120 minutes per recording (`YOUTUBE_MAX_SECONDS`, bounded 60–7200 seconds), 192 MiB audio,
256 MiB video, and the shared two-recording request budget. Live streams are rejected. Source files and completed
ASR parts are cached under protected `/data/youtube`; repeated requests reuse transcripts, and interrupted ASR
can resume completed parts on retry. Speaker labels are local to each audio chunk, not persistent identities.
The cache defaults to 512 MiB / 24 hours (`YOUTUBE_CACHE_MB`, `YOUTUBE_CACHE_HOURS`), evicts inactive recordings
and protects disk headroom. Source text is bounded at 120,000 characters; partial coverage is explicit.
`YOUTUBE_MEDIA=0` disables this ingestion. This cache preserves processing work, not the in-memory job queue.
For non-Compose installations, install `container/requirements-youtube.txt`, a supported Deno runtime and the
self-hosted provider, then set `YOUTUBE_POT_URL` to its private URL.

Claude requests cache their stable system instructions (including the scene schema and animation API)
with a one-hour TTL, shared across jobs within the API workspace. Dynamic questions, source material and
retry feedback stay outside that explicit cache prefix. Web research also benefits from the API's automatic
caching of server-tool results once caching is enabled. `ANTHROPIC_PROMPT_CACHE=0` disables the markers;
`ANTHROPIC_CACHE_TTL=5m` selects the shorter lifetime. Short prefixes below the model's minimum are left
uncached by the API; prompts are never padded. Cache writes cost extra, so savings depend on actual reuse.
The `claude_usage` log records fresh input, cache writes by TTL, cache reads, output tokens and the percentage
of input read from cache for each stage, without prompts or credentials. These counters measure reuse,
not an account-wide savings percentage.

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

`VISUAL_ASSETS=0` disables visual preparation, `WEB_VISUALS=0` disables web image lookup
(`IMAGE_SEARCH_PROVIDERS` picks the sources, `IMAGE_PICKER=0` skips the visual relevance check), and
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
| `DAILY_LIMIT` | `5` | explanations per user per UTC day; `0` = no limit |
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
