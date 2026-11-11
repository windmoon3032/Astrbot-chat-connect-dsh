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

const VERSION = '0.5.1';
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

  async function listSessionSummaries() {
    const query = ctx.get('sessionQuery');
    if (query && typeof query.listSessions === 'function') {
      const records = await query.listSessions();
      return records.map((record) => {
        const header = record.header || {};
        return {
          sessionId: header.id || record.sessionId,
          title: header.title || record.title || '',
          cwd: header.cwd || record.cwd || '',
          updatedAt: record.updatedAt || header.updatedAt || header.createdAt || 0,
        };
      });
    }
    const sessions = typeof ctx.sessions?.list === 'function' ? ctx.sessions.list() : [];
    return sessions.map((session) => ({
      sessionId: session.id,
      title: session.header?.title || '',
      cwd: session.header?.cwd || '',
      updatedAt: Date.now(),
    }));
  }

  function currentModelOf(sessionId) {
    const agent = agents.get(sessionId);
    if (!agent) return null;
    try {
      return agent.session.requestHeader()?.config || null;
    } catch {
      return null;
    }
  }

  async function createSession(cwd) {
    const defaults = (() => {
      try {
        return ctx.agentDefaultModel?.currentSelection?.() || {};
      } catch {
        return {};
      }
    })();
    const sessionId = `session-${randomUUID()}`;
    await ctx.agents.create({
      sessionId,
      ...(cwd ? { meta: { cwd } } : {}),
      ...(defaults.provider && defaults.model
        ? { agentOptions: { provider: defaults.provider, model: defaults.model } }
        : {}),
    });
    return sessionId;
  }

  async function applyModel(sessionId, body) {
    const agent = agents.get(sessionId);
    if (!agent) {
      const error = new Error('session-not-live');
      error.status = 409;
      throw error;
    }
    if (!body.model && !body.provider && !body.reasoningEffort) {
      const error = new Error('model or reasoningEffort is required');
      error.status = 400;
      throw error;
    }
    const base =
      currentModelOf(sessionId) ||
      (() => {
        try {
          return ctx.agentDefaultModel?.currentSelection?.() || {};
        } catch {
          return {};
        }
      })();
    const llm = ctx.get('llm');
    const resolved = await llm.resolveCallConfig({
      provider: body.provider || base.provider,
      model: body.model || base.model,
      ...(body.reasoningEffort ? { reasoningEffort: String(body.reasoningEffort) } : {}),
    });
    const selection = {
      provider: resolved.provider,
      model: resolved.model,
      ...(resolved.reasoningEffort === undefined ? {} : { reasoningEffort: resolved.reasoningEffort }),
    };
    const remote = ctx.get('remote');
    if (remote?.session?.selectModel) {
      await remote.session.selectModel({ sessionId, ...selection });
    } else {
      agent.session.append('model/selection', selection);
    }
    return selection;
  }

  const busy = new Set();
  const questionPending = new Map();
  const activeRuns = new Map();
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
    // agent.phase.lastTurn 是宿主内部字段，拿来当注入前的 turn 基线；
    // 取不到就退化为 0，后面靠 sawOwnMessage + 宽限兜底。
    let lastTurn = Number(agent?.phase?.lastTurn ?? 0) || 0;
    let injectTurn = 0;
    let sawOwnMessage = false;
    let injected = false;
    let graceTimer = null;
    let completedTurn = 0;
    let earlyQuestion = null;
    let settled = false;
    let resolveDone;
    let rejectDone;
    const done = new Promise((resolve, reject) => {
      resolveDone = resolve;
      rejectDone = reject;
    });

    const userMessage = createUserMessage(prompt);
    const ownId = userMessage.id;
    const runState = {
      onQuestion: (request) => {
        earlyQuestion = questionPayload(request);
        finish();
      },
    };
    activeRuns.set(sessionId, runState);
    let dispose;
    let timer;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      if (graceTimer) clearTimeout(graceTimer);
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
        const turn = Number(data.turn || 0) || 0;
        if (turn > lastTurn) lastTurn = turn;

        // followup 在 agent 忙时会走 next-turn，宿主发的是 inbox/spliced 而不是 user/message
        if (event.type === 'agent/inbox/spliced') {
          const inserted = Array.isArray(data.inserted) ? data.inserted : [];
          if (inserted.some((m) => String(m?.id || '') === ownId)) sawOwnMessage = true;
          return;
        }
        if (event.type === 'user/message') {
          if (String(data.message?.id || '') === ownId) sawOwnMessage = true;
          return;
        }
        if (!injected) return;

        if (event.type === 'assistant/message') {
          // 只收注入 turn 之后的 assistant 文本，避免把当前轮剩余内容混进来
          if (turn > injectTurn) {
            const text = messageText(data.message);
            if (text) texts.push(text);
          }
          return;
        }
        if (event.type === 'turn/end') {
          if (turn > injectTurn) {
            const reasonKind = String(data.reason?.kind || '');
            if (reasonKind && reasonKind !== 'completed') {
              const error = new Error('cancelled');
              error.status = 409;
              error.detail = `turn ended: ${reasonKind}`;
              completedTurn = turn;
              finish(error);
              return;
            }
            if (sawOwnMessage || texts.length) {
              completedTurn = turn;
              finish();
            } else if (!graceTimer) {
              // 理论上不会走到这里；万一没看到自己的消息，给 3 秒宽限再收尾
              graceTimer = setTimeout(() => {
                if (!settled) finish();
              }, 3000);
            }
          }
        }
      } catch {
        /* 忽略畸形事件 */
      }
    };

    injectTurn = lastTurn;
    runState.injectTurn = injectTurn;
    dispose = ctx.on('session/event', onEvent);
    timer = setTimeout(() => {
      const error = new Error('timeout');
      error.status = 504;
      error.partial = texts.join('\n\n').trim();
      finish(error);
    }, Math.max(1000, timeoutMs || defaultTimeoutMs));

    try {
      injected = true;
      agent.followup(userMessage);
    } catch (error) {
      cleanup();
      busy.delete(sessionId);
      throw error;
    }

    try {
      await done;
      if (earlyQuestion) return { question: earlyQuestion, sessionId };
      return texts.join('\n\n').trim();
    } catch (error) {
      busy.delete(sessionId);
      throw error;
    } finally {
      if (!earlyQuestion) activeRuns.delete(sessionId);
    }
  }

  function questionPayload(request) {
    const questions = (request.questions || []).map((q) => ({
      id: String(q.id || ''),
      header: String(q.header || ''),
      question: String(q.question || ''),
      options: (q.options || []).map((o) => (typeof o === 'string' ? o : String(o.label || o.value || ''))),
    }));
    return { callId: String(request.callId || request.wait?.callId || ''), questions };
  }

  function answerQuestion(sessionId, inputAnswers, timeoutMs) {
    const pending = questionPending.get(sessionId);
    if (!pending) {
      const error = new Error('no-pending-question');
      error.status = 409;
      return Promise.reject(error);
    }
    const questions = pending.request.questions || [];
    let answers = inputAnswers;
    if (!Array.isArray(answers)) {
      const text = String(answers || '').trim();
      answers = questions.map((q) => ({ id: q.id, selected: [], custom: text }));
    }
    questionPending.delete(sessionId);
    pending.resolve({ answers });

    const agent = agents.get(sessionId);
    const run = activeRuns.get(sessionId);
    const baseTurn = Number(run?.injectTurn ?? agent?.phase?.lastTurn ?? 0) || 0;
    return new Promise((resolve, reject) => {
      const texts = [];
      let settled = false;
      let dispose;
      let timer;
      const done = (error, turn) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try {
          dispose?.();
        } catch {
          /* ignore */
        }
        busy.delete(sessionId);
        activeRuns.delete(sessionId);
        if (error) reject(error);
        else resolve({ text: texts.join('\n\n').trim(), turn });
      };
      dispose = ctx.on('session/event', (session, event) => {
        if (!session || session.id !== sessionId || !event) return;
        const data = event.data || {};
        const turn = Number(data.turn || 0) || 0;
        if (event.type === 'assistant/message' && turn > baseTurn) {
          const text = messageText(data.message);
          if (text) texts.push(text);
        }
        if (event.type === 'turn/end' && turn > baseTurn) {
          const kind = String(data.reason?.kind || '');
          if (kind && kind !== 'completed') {
            const error = new Error('cancelled');
            error.status = 409;
            error.detail = `turn ended: ${kind}`;
            done(error);
            return;
          }
          done(null, turn);
        }
      });
      timer = setTimeout(() => {
        const error = new Error('timeout');
        error.status = 504;
        error.partial = texts.join('\n\n').trim();
        done(error);
      }, Math.max(1000, timeoutMs || defaultTimeoutMs));
    });
  }

  ctx.on('user-questions/request', async (request, next) => {
    const sid = request.agent?.session?.id;
    const run = sid ? activeRuns.get(sid) : undefined;
    if (!sid || !run || questionPending.has(sid)) return next();
    return await new Promise((resolve) => {
      questionPending.set(sid, { request, resolve });
      try {
        run.onQuestion(request);
      } catch {
        /* ignore */
      }
    });
  }, { global: true });

  async function handle(req, res) {
    const url = new URL(req.url || '/', 'http://dsh.local');

    if (req.method === 'GET' && url.pathname === '/sessions') {
      try {
        const items = await listSessionSummaries();
        return sendJson(res, 200, { ok: true, count: items.length, items });
      } catch (error) {
        return sendJson(res, 500, { ok: false, error: 'sessions-failed', detail: String(error) });
      }
    }

    if (req.method === 'POST' && url.pathname === '/session/new') {
      try {
        const body = JSON.parse((await readBody(req, MAX_BODY_BYTES)) || '{}');
        const sessionId = await createSession(String(body.cwd || '').trim() || undefined);
        return sendJson(res, 200, { ok: true, sessionId });
      } catch (error) {
        return sendJson(res, 500, { ok: false, error: 'create-failed', detail: String(error) });
      }
    }

    if (req.method === 'POST' && url.pathname === '/model') {
      try {
        const body = JSON.parse((await readBody(req, MAX_BODY_BYTES)) || '{}');
        const sessionId = String(body.sessionId || '').trim();
        if (!sessionId) return sendJson(res, 400, { ok: false, error: 'sessionId is required' });
        const selection = await applyModel(sessionId, body);
        return sendJson(res, 200, { ok: true, selection });
      } catch (error) {
        return sendJson(res, error.status || 500, {
          ok: false,
          error: error.status === 409 ? 'session-not-live' : 'model-change-failed',
          detail: String(error),
        });
      }
    }

    if (req.method === 'POST' && url.pathname === '/answer') {
      try {
        const body = JSON.parse((await readBody(req, MAX_BODY_BYTES)) || '{}');
        const sessionId = String(body.sessionId || '').trim();
        if (!sessionId) return sendJson(res, 400, { ok: false, error: 'sessionId is required' });
        const result = await answerQuestion(sessionId, body.answers, Number(body.timeoutMs || defaultTimeoutMs));
        return sendJson(res, 200, { ok: true, sessionId, ...result });
      } catch (error) {
        return sendJson(res, error.status || 500, {
          ok: false,
          error: String(error?.message || error),
          detail: error?.detail || undefined,
          partial: error?.partial || undefined,
        });
      }
    }

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
      const result = await runPrompt(sessionId, prompt, timeoutMs);
      if (result && typeof result === 'object' && result.question) {
        sendJson(res, 200, {
          ok: true,
          sessionId,
          question: result.question,
          elapsedMs: Date.now() - started,
        });
        return;
      }
      sendJson(res, 200, {
        ok: true,
        sessionId,
        turn: completedTurn,
        text: result,
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
