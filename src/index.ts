/**
 * Discord interactions endpoint for the /explain bot.
 *
 *  - /explain query:<text>          (slash command)
 *  - right-click a message > Apps > "Explain (video)"   (message command)
 *
 * The Worker checks the Discord signature, applies a per-user daily limit, answers "thinking…"
 * (deferred response) within 3 seconds, and hands the job to the render container.
 * The container makes the video and edits the original response itself (token valid 15 minutes).
 */
import { Container, getContainer } from "@cloudflare/containers";

export interface Env {
  RENDERER: DurableObjectNamespace<Renderer>;
  CF_VERSION_METADATA: WorkerVersionMetadata;
  LIMITS: KVNamespace;
  // secrets
  DISCORD_PUBLIC_KEY: string;
  ANTHROPIC_API_KEY: string;
  ANTHROPIC_WORKSPACE_ID?: string;
  ELEVENLABS_API_KEY: string;
  ELEVENLABS_VOICE_ID: string;
  DISCORD_BOT_TOKEN?: string; // optional: lets the container read conversation context
  // vars
  ANTHROPIC_MODEL: string;
  ELEVENLABS_MODEL: string;
  DAILY_LIMIT: string;
  MAX_UPLOAD_MB: string;
  BOT_LANG: string;
  RENDER_INSTANCES: string;
  DEBUG_ERRORS: string;
  FULL_GEN: string;
  REVIEW_ROUNDS: string;
}

export class Renderer extends Container<Env> {
  defaultPort = 8080;
  // A job runs in the background after the Worker returns, so keep the instance alive long enough
  // for the slowest job. Discord's interaction token expires after 15 minutes anyway.
  sleepAfter = "15m";

  constructor(ctx: ConstructorParameters<typeof Container<Env>>[0], env: Env) {
    super(ctx, env);
    this.envVars = {
      ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY ?? "",
      ANTHROPIC_WORKSPACE_ID: env.ANTHROPIC_WORKSPACE_ID ?? "",
      ANTHROPIC_MODEL: env.ANTHROPIC_MODEL ?? "",
      ELEVENLABS_API_KEY: env.ELEVENLABS_API_KEY ?? "",
      ELEVENLABS_VOICE_ID: env.ELEVENLABS_VOICE_ID ?? "",
      ELEVENLABS_MODEL: env.ELEVENLABS_MODEL ?? "",
      MAX_UPLOAD_MB: env.MAX_UPLOAD_MB ?? "",
      BOT_LANG: env.BOT_LANG ?? "",
      DEBUG_ERRORS: env.DEBUG_ERRORS ?? "",
      FULL_GEN: env.FULL_GEN ?? "1",
      REVIEW_ROUNDS: env.REVIEW_ROUNDS ?? "2",
      CODE_VERSION: env.CF_VERSION_METADATA?.id ?? "",
      DISCORD_BOT_TOKEN: env.DISCORD_BOT_TOKEN ?? "",
    };
  }
}

const STRINGS = {
  ru: {
    empty: "В этом сообщении нечего объяснять.",
    expired: "Форма устарела. Вызови команду ещё раз.",
    modalTitle: "Что объяснить в этом сообщении?",
    modalLabel: "Вопрос (можно пусто)",
    modalPlaceholder: "например: кто тут прав и почему?",
    limit: (n: number) => `Лимит: ${n} видео в день. Попробуй завтра.`,
    busy: "⚠️ Сервер видео сейчас занят. Попробуй через пару минут.",
    failed: "⚠️ Не получилось запустить создание видео. Попробуй ещё раз.",
  },
  en: {
    empty: "There is nothing to explain in this message.",
    expired: "This form expired. Run the command again.",
    modalTitle: "What should I explain here?",
    modalLabel: "Question (optional)",
    modalPlaceholder: "e.g. who is right here and why?",
    limit: (n: number) => `Limit: ${n} videos per day. Try again tomorrow.`,
    busy: "⚠️ The video server is busy. Try again in a few minutes.",
    failed: "⚠️ Could not start the video job. Try again.",
  },
};

const MAX_TEXT = 4000;

// Discord constants
const PING = 1;
const APPLICATION_COMMAND = 2;
const MODAL_SUBMIT = 5;
const MODAL = 9;
const ASK_COMMAND = "Ask about this (video)"; // message command that opens the question form
const CHAT_INPUT = 1;
const MESSAGE_COMMAND = 3;
const PONG = 1;
const CHANNEL_MESSAGE = 4;
const DEFERRED_CHANNEL_MESSAGE = 5;
const EPHEMERAL = 64;

interface Job {
  application_id: string;
  token: string;
  kind: "message" | "query";
  text: string; // the /explain query, or "" for a message
  question?: string; // optional question from the form
  message?: any; // trimmed Discord message (content, author, attachments, embeds, reply reference)
  channel_id?: string;
  context_messages?: number; // how many earlier channel messages to read (needs DISCORD_BOT_TOKEN)
  locale?: string;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "GET") return new Response("explain-bot is running");
    if (request.method !== "POST") return new Response("method not allowed", { status: 405 });

    const body = await request.text();
    if (!(await verifyDiscord(request, body, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }
    const i = JSON.parse(body);
    if (i.type === PING) return json({ type: PONG });
    const S = env.BOT_LANG === "en" ? STRINGS.en : STRINGS.ru;
    const d = i.data ?? {};

    // "Ask about this (video)": store the message for 15 min and open the question form.
    if (i.type === APPLICATION_COMMAND && d.type === MESSAGE_COMMAND && d.name === ASK_COMMAND) {
      const m = d.resolved?.messages?.[d.target_id];
      if (!m || !hasContent(m)) return ephemeral(S.empty);
      await env.LIMITS.put(`ask:${i.id}`, JSON.stringify({ message: trimMessage(m), channel_id: channelId(i) }), {
        expirationTtl: 900,
      });
      return json({
        type: MODAL,
        data: {
          // channel and message ids let us re-read the message with the bot token if KV is not consistent yet
          custom_id: `ask:${i.id}:${channelId(i) ?? ""}:${d.target_id}`,
          title: S.modalTitle.slice(0, 45),
          components: [
            {
              type: 1,
              components: [
                { type: 4, custom_id: "q", style: 2, label: S.modalLabel.slice(0, 45), placeholder: S.modalPlaceholder,
                  required: false, max_length: 500 },
              ],
            },
          ],
        },
      });
    }

    let job: Job | null;
    let ephemeralReply = false;
    if (i.type === MODAL_SUBMIT && String(d.custom_id ?? "").startsWith("ask:")) {
      const [, iid, cid, mid] = String(d.custom_id).split(":");
      let saved = (await env.LIMITS.get(`ask:${iid}`, "json")) as { message: any; channel_id?: string } | null;
      if (!saved && env.DISCORD_BOT_TOKEN && cid && mid) {
        // KV is eventually consistent across locations; fall back to reading the message again.
        const r = await fetch(`https://discord.com/api/v10/channels/${cid}/messages/${mid}`, {
          headers: { authorization: `Bot ${env.DISCORD_BOT_TOKEN}` },
        });
        if (r.ok) saved = { message: trimMessage(await r.json()), channel_id: cid };
      }
      if (!saved) return ephemeral(S.expired);
      const { message, channel_id } = saved;
      job = { ...base(i), kind: "message", text: "", message, channel_id, question: findValue(d.components, "q") ?? "" };
    } else if (i.type === APPLICATION_COMMAND) {
      job = buildJob(i);
      ephemeralReply = option(i, "private") === true;
      if (!job) return ephemeral(S.empty);
    } else {
      return new Response("unsupported interaction", { status: 400 });
    }

    const userId: string = i.member?.user?.id ?? i.user?.id ?? "unknown";
    const limit = parseInt(env.DAILY_LIMIT || "0", 10);
    if (limit > 0 && !(await takeQuota(env, userId, limit))) return ephemeral(S.limit(limit));

    ctx.waitUntil(startJob(env, job, S));
    return json({ type: DEFERRED_CHANNEL_MESSAGE, data: ephemeralReply ? { flags: EPHEMERAL } : {} });
  },
} satisfies ExportedHandler<Env>;

function base(i: any) {
  return { application_id: String(i.application_id), token: String(i.token), locale: i.locale };
}

function channelId(i: any): string | undefined {
  return i.channel_id ?? i.channel?.id;
}

function option(i: any, name: string): any {
  return (i.data?.options ?? []).find((o: any) => o.name === name)?.value;
}

function buildJob(i: any): Job | null {
  const d = i.data ?? {};
  if (d.type === CHAT_INPUT) {
    const q = option(i, "query");
    if (typeof q !== "string" || !q.trim()) return null;
    const n = Math.max(0, Math.min(50, Number(option(i, "messages") ?? 0) || 0));
    return { ...base(i), kind: "query", text: q.slice(0, MAX_TEXT), channel_id: channelId(i), context_messages: n };
  }
  if (d.type === MESSAGE_COMMAND) {
    const m = d.resolved?.messages?.[d.target_id];
    if (!m || !hasContent(m)) return null;
    return { ...base(i), kind: "message", text: "", message: trimMessage(m), channel_id: channelId(i) };
  }
  return null;
}

function hasContent(m: any): boolean {
  return Boolean((m.content ?? "").trim() || m.attachments?.length || m.embeds?.length);
}

/** Keep only what the container needs: text, author, files, link previews, reply reference. */
function trimMessage(m: any, depth = 0): any {
  if (!m) return undefined;
  return {
    id: m.id,
    content: String(m.content ?? "").slice(0, MAX_TEXT),
    timestamp: m.timestamp,
    author: { username: m.author?.username, global_name: m.author?.global_name },
    attachments: (m.attachments ?? []).slice(0, 10).map((a: any) => ({
      url: a.url, proxy_url: a.proxy_url, filename: a.filename, content_type: a.content_type, size: a.size,
    })),
    embeds: (m.embeds ?? []).slice(0, 5).map((e: any) => ({
      title: e.title, description: e.description?.slice(0, 1000), url: e.url,
      image: e.image?.url ? { url: e.image.url } : undefined,
      thumbnail: e.thumbnail?.url ? { url: e.thumbnail.url } : undefined,
    })),
    message_reference: m.message_reference ? { message_id: m.message_reference.message_id } : undefined,
    referenced_message: depth === 0 ? trimMessage(m.referenced_message, 1) : undefined,
  };
}

/** Find a text input value in modal-submit components (works for action rows and label components). */
function findValue(components: any, id: string): string | undefined {
  for (const c of components ?? []) {
    if (c?.custom_id === id && typeof c.value === "string") return c.value.slice(0, 500);
    const inner = findValue(c?.components, id) ?? (c?.component ? findValue([c.component], id) : undefined);
    if (inner !== undefined) return inner;
  }
  return undefined;
}

async function startJob(env: Env, job: Job, S: (typeof STRINGS)["ru"]): Promise<void> {
  try {
    // Instance names include the Worker version, so every deploy starts fresh containers with the new image.
    // (An old instance stays alive as long as it gets jobs within sleepAfter, and would keep the old image.)
    const n = Math.max(1, parseInt(env.RENDER_INSTANCES || "2", 10));
    const version = (env.CF_VERSION_METADATA?.id ?? "dev").slice(0, 8);
    const stub = getContainer(env.RENDERER, `render-${version}-${Math.floor(Math.random() * n)}`);
    const res = await stub.fetch(
      new Request("http://renderer/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(job),
      }),
    );
    if (res.status === 202) return;
    console.error("container refused job", res.status, await res.text());
    await editOriginal(job, res.status === 429 ? S.busy : S.failed);
  } catch (err) {
    console.error("startJob failed", err);
    await editOriginal(job, S.failed);
  }
}

async function editOriginal(job: Job, content: string): Promise<void> {
  const url = `https://discord.com/api/v10/webhooks/${job.application_id}/${job.token}/messages/@original`;
  const r = await fetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
  });
  if (!r.ok) console.error("edit original failed", r.status, await r.text());
}

/** KV counter per user per UTC day. KV is eventually consistent, so the limit is approximate. */
async function takeQuota(env: Env, userId: string, limit: number): Promise<boolean> {
  const key = `u:${userId}:${new Date().toISOString().slice(0, 10)}`;
  const used = parseInt((await env.LIMITS.get(key)) ?? "0", 10);
  if (used >= limit) return false;
  await env.LIMITS.put(key, String(used + 1), { expirationTtl: 60 * 60 * 48 });
  return true;
}

async function verifyDiscord(request: Request, body: string, publicKeyHex: string): Promise<boolean> {
  const sig = request.headers.get("x-signature-ed25519");
  const ts = request.headers.get("x-signature-timestamp");
  if (!sig || !ts || !publicKeyHex) return false;
  try {
    const key = await crypto.subtle.importKey("raw", hexToBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "Ed25519" }, key, hexToBytes(sig), new TextEncoder().encode(ts + body));
  } catch {
    return false;
  }
}

function hexToBytes(hex: string): Uint8Array {
  const clean = hex.trim();
  if (clean.length % 2 || /[^0-9a-f]/i.test(clean)) throw new Error("bad hex");
  const out = new Uint8Array(clean.length / 2);
  for (let k = 0; k < out.length; k++) out[k] = parseInt(clean.slice(2 * k, 2 * k + 2), 16);
  return out;
}

function json(obj: unknown): Response {
  return new Response(JSON.stringify(obj), { headers: { "content-type": "application/json" } });
}

function ephemeral(content: string): Response {
  return json({ type: CHANNEL_MESSAGE, data: { content, flags: EPHEMERAL } });
}
