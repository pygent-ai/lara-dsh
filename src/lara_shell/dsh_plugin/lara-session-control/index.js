/**
 * lara-session-control — a dsh (DeepSeek Harness) Cordis plugin that gives
 * Lara a session control plane inside the dsh runtime it owns.
 *
 * The SDK JSON-RPC server owns stdio and exposes no hook for extra methods,
 * so this plugin serves its own newline-delimited JSON-RPC on a loopback TCP
 * port that the launching Lara process selects through LARA_DSH_CONTROL_PORT.
 * Live session events, approvals and user questions already reach Lara
 * through the SDK wire (`session.event` / `session.status` notifications,
 * `approval/request` and `user-question/ask` host requests); this channel
 * adds only what that wire lacks: a session inventory with live status,
 * sessions rooted at their own directory, cancellation, and dsh's model
 * catalog and credential store.
 *
 * Client → plugin requests (all results are JSON objects):
 *   lara/ping                                    -> { ok, name, version }
 *   lara/session/list                            -> { sessions: SessionEntry[] }
 *   lara/session/stat     { sessionId }          -> { session: SessionEntry | null }
 *   lara/session/create   { sessionId, cwd }     -> { sessionId, cwd, createdAt }
 *   lara/session/cancel   { sessionId }          -> { cancelled: boolean }
 *   lara/models/list                             -> { providers: [{ id, name, models: [{ id, name, description? }] }] }
 *   lara/credentials/describe { ref }            -> { configured, source?, writable?, masked? }
 *   lara/credentials/resolve  { ref }            -> { value, source } | { value: null }
 *   lara/credentials/set      { ref, value }     -> { ok }   (value "" unsets)
 *
 * No imports from dsh packages: everything comes through `ctx`, so the file
 * loads from any absolute path via a `--patch` overlay or as an installed bundle.
 */

import net from 'node:net'

export const name = 'lara-session-control'
export const inject = ['sessionPersistence', 'agents', 'sessions']

const VERSION = '0.3.0'
const JSONRPC_INVALID_REQUEST = -32600
const JSONRPC_METHOD_NOT_FOUND = -32601
const JSONRPC_INTERNAL_ERROR = -32603

export function apply(ctx) {
  const port = Number(process.env.LARA_DSH_CONTROL_PORT)
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    process.stderr.write(`[${name}] LARA_DSH_CONTROL_PORT is not set; control channel disabled\n`)
    return
  }

  const server = net.createServer((socket) => {
    socket.setEncoding('utf8')
    let buffer = ''
    socket.on('data', (chunk) => {
      buffer += chunk
      let newline = buffer.indexOf('\n')
      while (newline !== -1) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (line) void handleLine(ctx, socket, line)
        newline = buffer.indexOf('\n')
      }
    })
    socket.on('error', () => { /* a dropped client is not a runtime fault */ })
  })

  ctx.effect(() => {
    server.on('error', (error) => {
      process.stderr.write(`[${name}] control channel failed on 127.0.0.1:${port}: ${error.message}\n`)
    })
    server.listen(port, '127.0.0.1')
    return () => new Promise((resolve) => server.close(() => resolve()))
  }, 'lara.session-control.serve')

}

async function handleLine(ctx, socket, line) {
  let message
  try {
    message = JSON.parse(line)
  } catch {
    write(socket, { jsonrpc: '2.0', id: null, error: { code: JSONRPC_INVALID_REQUEST, message: 'invalid JSON' } })
    return
  }
  const id = message?.id ?? null
  if (message?.jsonrpc !== '2.0' || typeof message.method !== 'string') {
    write(socket, { jsonrpc: '2.0', id, error: { code: JSONRPC_INVALID_REQUEST, message: 'invalid JSON-RPC request' } })
    return
  }
  try {
    const result = await dispatch(ctx, message.method, message.params ?? {})
    write(socket, { jsonrpc: '2.0', id, result })
  } catch (error) {
    const code = error instanceof MethodNotFound ? JSONRPC_METHOD_NOT_FOUND : JSONRPC_INTERNAL_ERROR
    write(socket, { jsonrpc: '2.0', id, error: { code, message: error instanceof Error ? error.message : String(error) } })
  }
}

function write(socket, message) {
  if (!socket.destroyed) socket.write(`${JSON.stringify(message)}\n`)
}

class MethodNotFound extends Error {}

function requireString(params, key) {
  const value = params[key]
  if (typeof value !== 'string' || value === '') throw new Error(`${key} must be a non-empty string`)
  return value
}

async function dispatch(ctx, method, params) {
  switch (method) {
    case 'lara/ping':
      return { ok: true, name, version: VERSION }
    case 'lara/session/list':
      return { sessions: await listSessions(ctx) }
    case 'lara/session/stat': {
      const sessionId = requireString(params, 'sessionId')
      const sessions = await listSessions(ctx)
      return { session: sessions.find((entry) => entry.sessionId === sessionId) ?? null }
    }
    case 'lara/session/create':
      return createSession(ctx, params)
    case 'lara/session/cancel': {
      const agent = ctx.agents.get(requireString(params, 'sessionId'))
      if (agent === undefined) return { cancelled: false }
      await agent.cancel()
      return { cancelled: true }
    }
    case 'lara/models/list':
      return { providers: await listModels(ctx) }
    case 'lara/credentials/describe': {
      const credentials = requireService(ctx, 'credentials')
      const info = await credentials.describe(requireString(params, 'ref'))
      return { configured: Boolean(info.configured), source: info.source ?? null, writable: info.writable ?? null, masked: info.masked ?? null }
    }
    case 'lara/credentials/resolve': {
      const credentials = requireService(ctx, 'credentials')
      const resolved = await credentials.resolve(requireString(params, 'ref'))
      return resolved === undefined ? { value: null } : { value: resolved.value, source: resolved.source }
    }
    case 'lara/credentials/set': {
      const credentials = requireService(ctx, 'credentials')
      const ref = requireString(params, 'ref')
      const value = params.value
      if (typeof value !== 'string') throw new Error('value must be a string')
      if (value === '') await credentials.unset(ref)
      else await credentials.set(ref, value)
      return { ok: true }
    }
    default:
      throw new MethodNotFound(`unknown lara-session-control method: ${method}`)
  }
}

function requireService(ctx, key) {
  const service = ctx.get(key)
  if (service === undefined) throw new Error(`the ${key} service is not mounted in this profile`)
  return service
}

/**
 * Providers registered with the llm service and each one's model catalog.
 * A provider whose catalog cannot be listed (e.g. missing credential) is
 * reported with an empty model list rather than failing the whole call.
 */
async function listModels(ctx) {
  const llm = requireService(ctx, 'llm')
  const providers = []
  for (const provider of llm.listProviders()) {
    let models = []
    try {
      models = (await llm.listModels(provider.id)).map((model) => ({
        id: model.id,
        name: model.name ?? model.id,
        ...(model.description === undefined ? {} : { description: model.description }),
      }))
    } catch {
      models = []
    }
    providers.push({ id: provider.id, name: provider.name ?? provider.id, models })
  }
  return providers
}

/**
 * Persist a new, idle session whose header records `cwd`.
 *
 * The SDK server stamps every session it creates with the one `cwd` from
 * `initialize`, and the file sandbox's workspace-write boundary is exactly
 * `session.header.cwd`, fixed for the session's life. Creating the session
 * here with the caller's directory and releasing it right away leaves a
 * persisted header the SDK's `session/resume` then adopts unchanged, so the
 * session works — and may write — inside its own project directory.
 */
async function createSession(ctx, params) {
  const sessionId = requireString(params, 'sessionId')
  const cwd = requireString(params, 'cwd')
  if (ctx.agents.get(sessionId) !== undefined) throw new Error(`session "${sessionId}" is already live`)
  if ((await ctx.sessionPersistence.stat(sessionId)) !== undefined) throw new Error(`session "${sessionId}" already exists`)
  const handle = await ctx.agents.create({ sessionId, meta: { cwd } })
  const header = handle.agent.session.header
  await handle.dispose()
  return { sessionId: String(header.id), cwd: header.cwd ?? null, createdAt: header.createdAt }
}

/**
 * Persisted sessions joined with the live agent registry. A live agent whose
 * session has not been materialized on disk yet is still reported, so a
 * freshly created session shows up as soon as its first prompt is queued.
 */
async function listSessions(ctx) {
  const live = new Map(ctx.agents.list().map((agent) => [String(agent.session.id), agent]))
  const titles = ctx.get('sessionTitle')
  const entries = []
  const seen = new Set()
  for (const snapshot of await ctx.sessionPersistence.list()) {
    const id = String(snapshot.header.id)
    seen.add(id)
    entries.push(describe(ctx, snapshot.header, snapshot, live.get(id), titles))
  }
  for (const [id, agent] of live) {
    if (!seen.has(id)) entries.push(describe(ctx, agent.session.header, undefined, agent, titles))
  }
  await fillPersistedTitles(ctx, entries, titles)
  entries.sort((a, b) => b.createdAt - a.createdAt)
  return entries
}

function describe(ctx, header, snapshot, agent, titles) {
  const session = agent?.session ?? ctx.sessions.get(header.id)
  const title = session !== undefined && titles !== undefined ? titles.get(session)?.title ?? null : null
  return {
    sessionId: String(header.id),
    cwd: header.cwd ?? null,
    createdAt: header.createdAt,
    origin: header.origin ?? 'root',
    parentSessionId: header.parentSession === undefined ? null : String(header.parentSession),
    eventCount: snapshot?.eventCount ?? session?.seq ?? null,
    title,
    live: agent !== undefined,
    status: agent?.status ?? null,
  }
}

/**
 * The title service folds from a Session object, which only live sessions
 * have. `sessionQuery.readTitle` folds a persisted log detached — no agent,
 * nothing made live — so persisted-only entries get titles too. A session
 * whose log cannot be read keeps a null title rather than failing the whole
 * inventory.
 */
async function fillPersistedTitles(ctx, entries, titles) {
  const query = ctx.get('sessionQuery')
  if (query === undefined || titles === undefined) return
  await Promise.all(entries
    .filter((entry) => !entry.live && entry.title === null)
    .map(async (entry) => {
      try {
        entry.title = (await query.readTitle(entry.sessionId))?.title ?? null
      } catch {
        entry.title = null
      }
    }))
}
