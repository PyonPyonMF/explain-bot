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
import { Container, getRandom } from "@cloudflare/containers";

export interface Env {
  RENDERER: DurableObjectNamespace<Renderer>;
  LIMITS: KVNamespace;
  // secrets
  DISCORD_PUBLIC_KEY: string;
  ANTHROPIC_API_KEY: string;
  ANTHROPIC_WORKSPACE_ID?: string;
  ELEVENLABS_API_KEY: string;
  ELEVENLABS_VOICE_ID: string;
  // vars
  ANTHROPIC_MODEL: string;
  ELEVENLABS_MODEL: string;
  DAILY_LIMIT: string;
  MAX_UPLOAD_MB: string;
  BOT_LANG: string;
  RENDER_INSTANCES: string;
  DEBUG_ERRORS: string;
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
    };
  }
}

const STRINGS = {
  ru: {
    empty: "В этом сообщении нет текста, который можно объяснить.",
    limit: (n: number) => `Лимит: ${n} видео в день. Попробуй завтра.`,
    busy: "⚠️ Сервер видео сейчас занят. Попробуй через пару минут.",
    failed: "⚠️ Не получилось запустить создание видео. Попробуй ещё раз.",
  },
  en: {
    empty: "This message has no text to explain.",
    limit: (n: number) => `Limit: ${n} videos per day. Try again tomorrow.`,
    busy: "⚠️ The video server is busy. Try again in a few minutes.",
    failed: "⚠️ Could not start the video job. Try again.",
  },
};

const MAX_TEXT = 4000;

// Discord constants
const PING = 1;
const APPLICATION_COMMAND = 2;
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
  text: string;
  author?: string;
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
    if (i.type !== APPLICATION_COMMAND) return new Response("unsupported interaction", { status: 400 });

    const S = env.BOT_LANG === "en" ? STRINGS.en : STRINGS.ru;
    const job = buildJob(i);
    if (!job || !job.text.trim()) return ephemeral(S.empty);

    const userId: string = i.member?.user?.id ?? i.user?.id ?? "unknown";
    const limit = parseInt(env.DAILY_LIMIT || "0", 10);
    if (limit > 0 && !(await takeQuota(env, userId, limit))) return ephemeral(S.limit(limit));

    ctx.waitUntil(startJob(env, job, S));
    return json({ type: DEFERRED_CHANNEL_MESSAGE });
  },
} satisfies ExportedHandler<Env>;

function buildJob(i: any): Job | null {
  const d = i.data ?? {};
  const base = { application_id: String(i.application_id), token: String(i.token), locale: i.locale };
  if (d.type === CHAT_INPUT) {
    const q = (d.options ?? []).find((o: any) => o.name === "query")?.value;
    return typeof q === "string" ? { ...base, kind: "query", text: q.slice(0, MAX_TEXT) } : null;
  }
  if (d.type === MESSAGE_COMMAND) {
    const m = d.resolved?.messages?.[d.target_id];
    if (!m) return null;
    const parts: string[] = [];
    if (m.content) parts.push(m.content);
    for (const e of m.embeds ?? []) {
      if (e.title) parts.push(e.title);
      if (e.description) parts.push(e.description);
    }
    const author = m.author?.global_name ?? m.author?.username;
    return { ...base, kind: "message", text: parts.join("\n\n").slice(0, MAX_TEXT), author };
  }
  return null;
}

async function startJob(env: Env, job: Job, S: (typeof STRINGS)["ru"]): Promise<void> {
  try {
    const n = Math.max(1, parseInt(env.RENDER_INSTANCES || "2", 10));
    const stub = await getRandom(env.RENDERER, n);
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
