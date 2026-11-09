/**
 * dsh-qq-bridge — 让外部程序把 prompt 送进 DSH 当前会话
 *
 * 在 DSH 宿主进程里监听 127.0.0.1:PORT，提供：
 *   GET  /health   → { ok, name, version, port, timeoutMs, busy }
 *   POST /prompt   → { prompt, sessionId, timeoutMs? }
 *                     header: x-bridge-secret
 *                    成功 { ok:true, text, sessionId, elapsedMs }
 *                    失败 { ok:false, error, detail?, partial? }
 *
 * 实现要点：
 *   - 用 ctx.get('agents') 拿到活着的 Agent，agent.followup() 注入用户消息；
 *   - 用消息 id 精确匹配 user/message 事件，避免把别人的消息当成自己的；
 *   - 监听 session/event 收集 assistant/message 文本，等对应 turn 的 turn/end；
 *   - 同一会话并发请求返回 409 session-busy，超时返回 504 timeout（带 partial）。
 *
 * 本文件不 import 任何 @deepseek-ai/* 运行时值（profile loader 不提供），
 * 用户消息按 DSH 的 createUserMessage 结构本地构造。
 */
import { createServer } from 'node:http';
import { randomUUID, timingSafeEqual } from 'node:crypto';

export const name = 'dsh-qq-bridge';
export const inject = ['agents'];

const VERSION = '0.2.2';
const MAX_BODY_BYTES = 256 * 1024;

function safeEqual(a, b) {
  const left = Buffer.from(String(a ?? ''));
  const right = Buffer.from(String(b ?? ''));
  if (left.length !== right.length) return false;
  try {
    return timingSafeEqual(left, right);
  } catch {
    return false;
  }
}

function createUserMessage(text) {
  return Object.freeze({
    role: 'user',
    id: randomUUID(),
    content: [{ type: 'text', text }],
    source: { kind: 'user', via: 'qq-bridge' },
  });
}

function messageText(message) {
  if (!message || !Array.isArray(message.content)) return '';
  const parts = [];
  for (const part of message.content) {
    if (part && part.type === 'text' && typeof part.text === 'string') parts.push(part.text);
  }
  return parts.join('\n').trim();
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  });
  res.end(body);
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        // 不在这里 destroy：先让 handle() 把 413 响应写回去
        reject(Object.assign(new Error('body too large'), { code: 413 }));
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export function apply(ctx, config = {}) {
  const port = Number(config.port || 5795);
  const secret = String(config.secret || '');
  const defaultTimeoutMs = Math.max(1000, Number(config.timeoutMs || 1500000));
  const maxBody = Math.max(1024, Number(config.maxBodyBytes || MAX_BODY_BYTES));

  const agents = typeof ctx.get === 'function' ? ctx.get('agents') : ctx.agents;
  if (!agents) {
    ctx.logger?.warn?.('dsh-qq-bridge: agents 服务不可用，桥未启动');
    return;
  }
  if (!secret) {
    ctx.logger?.warn?.('dsh-qq-bridge: secret 为空，请在 cordis.patch.yml 里设置并与 bot 端一致');
  }

  const busy = new Set();
  const server = createServer((req, res) => {
    void handle(req, res).catch((error) => {
      sendJson(res, 500, { ok: false, error: String(error?.message || error) });
    });
  });
  // 端口被占用时只告警，不要让 DSH 宿主崩溃
  server.on('error', (error) => {
    ctx.logger?.warn?.(`dsh-qq-bridge: server error: ${error?.message || error}`);
  });

  async function runPrompt(sessionId, prompt, timeoutMs) {
    const agent = agents.get(sessionId);
    if (!agent) {
      const error = new Error('session-not-live');
      error.status = 409;
      error.detail = `session ${sessionId} 不在当前 DSH 进程里`;
      throw error;
    }
    if (busy.has(sessionId)) {
      const error = new Error('session-busy');
      error.status = 409;
      error.detail = '该会话正在执行上一条 qq-bridge 请求';
      throw error;
    }
    busy.add(sessionId);

    const texts = [];
    let sawOwnMessage = false;
    let targetTurn = 0;
    let settled = false;
    let resolveDone;
    let rejectDone;
    const done = new Promise((resolve, reject) => {
      resolveDone = resolve;
      rejectDone = reject;
    });

    const userMessage = createUserMessage(prompt);
    const ownId = userMessage.id;
    let dispose;
    let timer;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      if (typeof dispose === 'function') {
        try {
          dispose();
        } catch {
          /* ignore */
        }
      }
    };
    const finish = (error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) rejectDone(error);
      else resolveDone();
    };

    const onEvent = (session, event) => {
      try {
        if (!session || session.id !== sessionId || !event) return;
        const data = event.data || {};
        if (event.type === 'user/message') {
          if (sawOwnMessage) return;
          if (String(data.message?.id || '') === ownId) {
            sawOwnMessage = true;
            targetTurn = Number(data.turn || 0) || 0;
          }
          return;
        }
        if (event.type === 'assistant/message' && sawOwnMessage) {
          const text = messageText(data.message);
          if (text) texts.push(text);
          return;
        }
        if (event.type === 'turn/end' && sawOwnMessage) {
          const turn = Number(data.turn || 0);
          if (!targetTurn || !turn || turn >= targetTurn) finish();
        }
      } catch {
        /* 忽略畸形事件 */
      }
    };

    dispose = ctx.on('session/event', onEvent);
    timer = setTimeout(() => {
      const error = new Error('timeout');
      error.status = 504;
      error.partial = texts.join('\n\n').trim();
      finish(error);
    }, Math.max(1000, timeoutMs || defaultTimeoutMs));

    try {
      agent.followup(userMessage);
    } catch (error) {
      cleanup();
      busy.delete(sessionId);
      throw error;
    }

    try {
      await done;
      return texts.join('\n\n').trim();
    } finally {
      busy.delete(sessionId);
    }
  }

  async function handle(req, res) {
    const url = new URL(req.url || '/', 'http://dsh.local');

    if (req.method === 'GET' && url.pathname === '/health') {
      sendJson(res, 200, {
        ok: true,
        name,
        version: VERSION,
        port,
        timeoutMs: defaultTimeoutMs,
        busy: [...busy],
      });
      return;
    }

    if (req.method !== 'POST' || url.pathname !== '/prompt') {
      sendJson(res, 404, { ok: false, error: 'not-found' });
      return;
    }

    if (!safeEqual(req.headers['x-bridge-secret'], secret)) {
      sendJson(res, 401, { ok: false, error: 'bad-secret' });
      return;
    }

    let raw;
    try {
      raw = await readBody(req, maxBody);
    } catch (error) {
      sendJson(res, error?.code === 413 ? 413 : 400, {
        ok: false,
        error: error?.code === 413 ? 'body-too-large' : 'bad-request',
      });
      if (error?.code === 413) req.destroy();
      return;
    }

    let payload;
    try {
      payload = JSON.parse(raw || '{}');
    } catch {
      sendJson(res, 400, { ok: false, error: 'invalid-json' });
      return;
    }

    const prompt = String(payload.prompt || '').trim();
    const sessionId = String(payload.sessionId || '').trim();
    const timeoutMs = Number(payload.timeoutMs || defaultTimeoutMs);
    if (!prompt || !sessionId) {
      sendJson(res, 400, { ok: false, error: 'prompt and sessionId are required' });
      return;
    }

    const started = Date.now();
    try {
      const text = await runPrompt(sessionId, prompt, timeoutMs);
      sendJson(res, 200, {
        ok: true,
        sessionId,
        text,
        elapsedMs: Date.now() - started,
      });
    } catch (error) {
      const status = Number(error?.status || 500);
      sendJson(res, status, {
        ok: false,
        error: String(error?.message || error),
        detail: error?.detail || undefined,
        partial: error?.partial || undefined,
        elapsedMs: Date.now() - started,
      });
    }
  }

  ctx.effect(() => {
    server.listen(port, '127.0.0.1', () => {
      ctx.logger?.info?.(`dsh-qq-bridge ${VERSION}: listening on 127.0.0.1:${port}`);
    });
    return () => {
      try {
        server.close();
      } catch {
        /* ignore */
      }
    };
  });
}
