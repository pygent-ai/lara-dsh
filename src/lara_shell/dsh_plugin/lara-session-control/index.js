/**
 * lara-session-control — the dsh (DeepSeek Harness) Cordis plugin through
 * which Lara drives the dsh runtime the user already runs (desktop, `dsh web`,
 * or a runtime Lara spawned itself).
 *
 * The plugin serves newline-delimited JSON-RPC on a loopback TCP port. In
 * attach mode (no LARA_DSH_CONTROL_PORT) it picks a free port and publishes it
 * to `<DSH_HOME>/lara/dsh-plugin.json`, where Lara finds the running dsh. In
 * spawn mode the launching Lara process selects the port through
 * LARA_DSH_CONTROL_PORT and nothing is published, so a private runtime never
 * shadows the user's own dsh.
 *
 * Client → plugin requests (all results are JSON objects):
 *   lara/ping                                    -> { ok, name, version, pid, attach }   (attach: prompts deliverable here)
 *   lara/session/list                            -> { sessions: SessionEntry[] }
 *   lara/session/stat     { sessionId }          -> { session: SessionEntry | null }
 *   lara/session/create   { sessionId, cwd }     -> { sessionId, cwd, createdAt }
 *   lara/session/prompt   { text, sessionId?, cwd? }
 *                                                -> { sessionId, messageId, created }   (attach only)
 *   lara/session/resume   { sessionId }          -> { sessionId, status }               (attach only)
 *   lara/session/select-model { sessionId, provider, model } -> { sessionId, provider, model }  (attach only)
 *   lara/model/route                             -> { default?, providers, apiKey, account, baseURL }
 *                                                   (the official model-config view: agentDefaultModel plus
 *                                                    the credential material dsh itself would use)
 *   lara/session/cancel   { sessionId }          -> { cancelled: boolean }
 *   lara/events/subscribe                        -> { ok }   this socket becomes the Lara wire (below)
 *   lara/models/list                             -> { providers: [{ id, name, models: [{ id, name, description? }] }] }
 *   lara/credentials/describe { ref }            -> { configured, source?, writable?, masked? }
 *   lara/credentials/resolve  { ref }            -> { value, source } | { value: null }
 *   lara/credentials/set      { ref, value }     -> { ok }   (value "" unsets)
 *
 * Plugin → subscriber traffic mirrors the dsh SDK wire so Lara handles both
 * the same way:
 *   notification session.event   { sessionId, event }
 *   notification session.status  { sessionId, status }
 *   request      approval/request   { sessionId, toolName, callId?, reason? } -> { outcome }
 *   request      user-question/ask  { requestId, sessionId, questions }       -> { answers } | { cancelled: true }
 *   notification user-question.cancelled { requestId, sessionId }
 * Approvals and questions are claimed only for sessions Lara prompted through
 * this plugin; every other session falls through to dsh's own answerers.
 *
 * Attach-only methods ride on `sessionController`, the Host API behind dsh's
 * web and desktop clients, so Lara-created sessions are ordinary sessions of
 * that UI. No imports from dsh packages: everything comes through `ctx`, so
 * the file loads from any absolute path via a `--patch` overlay or as an
 * installed bundle.
 */

import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import Schema from '@deepseek-ai/schemastery'

export const name = 'lara-session-control'
export const inject = ['sessionPersistence', 'agents', 'sessions', 'subprocess']

/**
 * Plugin row configuration (override in the profile patch layer's config
 * entries for this plugin id). `petPath` names the desktop-pet executable;
 * empty uses the one shipped in this package (`bin/lara-pet.exe`).
 */
export const Config = Schema.object({
  autoLaunch: Schema.boolean().default(true),
  petPath: Schema.string(),
})

const VERSION = '0.5.0'
const DAEMON_STATE_FILENAME = 'dsh-daemon.json'
const ENDPOINT_FILENAME = 'dsh-plugin.json'
const JSONRPC_INVALID_REQUEST = -32600
const JSONRPC_METHOD_NOT_FOUND = -32601
const JSONRPC_INTERNAL_ERROR = -32603

export function apply(ctx, config) {
  const configuredPort = process.env.LARA_DSH_CONTROL_PORT
  const port = configuredPort === undefined ? 0 : Number(configuredPort)
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    process.stderr.write(`[${name}] LARA_DSH_CONTROL_PORT=${configuredPort} is not a port; control channel disabled\n`)
    return
  }

  const lara = new LaraWire(ctx)
  const server = net.createServer((socket) => lara.accept(socket))

  ctx.effect(() => {
    server.on('error', (error) => {
      process.stderr.write(`[${name}] control channel failed on 127.0.0.1:${port}: ${error.message}\n`)
    })
    let endpoint = null
    server.listen(port, '127.0.0.1', () => {
      if (configuredPort !== undefined) return
      endpoint = publishEndpoint(server.address().port)
      // The user's dsh owns Lara's lifecycle: bring the pet up with it (the
      // managed child dies with this dsh, so a restart brings a fresh pet).
      // Lara spawned this runtime itself (spawn mode) only when the port came
      // from the environment, and then it already manages the pet.
      if (config.autoLaunch) void autoLaunchPet(ctx, config)
    })
    return () => new Promise((resolve) => {
      lara.closeAll()
      server.close(() => {
        if (endpoint !== null) retractEndpoint(endpoint)
        resolve()
      })
    })
  }, 'lara.session-control.serve')

  ctx.on('session/event', (session, event) => {
    lara.notify('session.event', { sessionId: String(session.id), event })
  })
  ctx.on('agent/status', ({ agent, status }) => {
    lara.notify('session.status', { sessionId: String(agent.session.id), status })
  })
  // Prepended: the web/desktop app forwards these waterfalls to its browser
  // peers and parks them there until one answers, so a Lara-owned session's
  // approval or question would otherwise never reach Lara. Other sessions
  // still fall through to the UI untouched.
  ctx.on('approval/request', (request, next) => lara.answerApproval(request, next), { prepend: true })
  ctx.on('user-questions/request', (request, next) => lara.answerQuestion(request, next), { prepend: true })
}

/**
 * Start the desktop pet alongside this dsh through the managed subprocess
 * seam, at most one per machine.
 *
 * The pet executable ships in this package (`bin/lara-pet.exe`); `petPath`
 * in the plugin row config overrides it, as does `LARA_PET_EXE`. A live
 * answer on the daemon state file's port means the pet is already up, so a
 * dsh restart never spawns a second one. The spawn is managed: the child
 * dies with this dsh, so a restart brings a fresh pet.
 */
async function autoLaunchPet(ctx, config) {
  const explicit = (process.env.LARA_PET_EXE || '').trim()
  const petPath = explicit || (config.petPath || '').trim() || bundledPetPath()
  if (!petPath || !fs.existsSync(petPath)) {
    process.stderr.write(`[${name}] no pet executable at ${petPath || '<unset>'}; install it with 'dsh plugin add' or set LARA_PET_EXE\n`)
    return
  }
  const state = readDaemonState()
  if (state !== null && (await liveListener(state.port))) {
    return // the pet from a previous dsh run is still up
  }
  try {
    const handle = ctx.subprocess.spawn({
      argv: [petPath],
      cwd: path.dirname(petPath),
      stdio: { stdin: 'ignore', stdout: 'ignore', stderr: 'ignore' },
      graceMs: 2000,
      // The scrub drops every DSH_* entry; the pet and its sidecar must
      // target the same home this dsh serves, so forward it deliberately.
      env: { DSH_HOME: process.env.DSH_HOME || path.join(os.homedir(), '.dsh') },
    })
    process.stderr.write(`[${name}] launched the pet (pid ${handle.pid})\n`)
  } catch (error) {
    process.stderr.write(`[${name}] could not launch the pet at ${petPath}: ${error.message}\n`)
  }
}

/** The pet executable shipped next to this plugin file. */
function bundledPetPath() {
  const candidate = path.join(path.dirname(fileURLToPath(import.meta.url)), 'bin', 'lara-pet.exe')
  return fs.existsSync(candidate) ? candidate : null
}

function readDaemonState() {
  try {
    const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
    const record = JSON.parse(fs.readFileSync(path.join(home, 'lara', DAEMON_STATE_FILENAME), 'utf8'))
    const port = Number(record.port)
    return Number.isInteger(port) && port > 0 && record.pid !== process.pid ? { port, pid: record.pid } : null
  } catch {
    return null
  }
}

/** A daemon that accepts a TCP connection on its port is alive (the JSON-RPC layer needs no probe). */
function liveListener(port) {
  return new Promise((resolve) => {
    const probe = net.createConnection({ host: '127.0.0.1', port })
    const settle = (alive) => { probe.destroy(); resolve(alive) }
    probe.once('connect', () => settle(true))
    probe.once('error', () => settle(false))
    setTimeout(() => settle(false), 400).unref()
  })
}

/** Where Lara looks for a running dsh: `<DSH_HOME>/lara/dsh-plugin.json`. */
function endpointPath() {
  const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
  return path.join(home, 'lara', ENDPOINT_FILENAME)
}

function publishEndpoint(port) {
  const file = endpointPath()
  // Only where to find us: whether prompts can be delivered (`attach`) is
  // answered by lara/ping, since services settle after this listener opens.
  const record = { port, pid: process.pid, version: VERSION, startedAt: Date.now() }
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, `${JSON.stringify(record)}\n`, 'utf8')
  } catch (error) {
    process.stderr.write(`[${name}] could not publish ${file}: ${error.message}\n`)
    return null
  }
  return file
}

function retractEndpoint(file) {
  try {
    if (JSON.parse(fs.readFileSync(file, 'utf8')).pid === process.pid) fs.unlinkSync(file)
  } catch {
    // another runtime took the file over, or it is already gone
  }
}

/** One connected client: request/response in both directions on one socket. */
class Peer {
  constructor(socket, onRequest) {
    this.socket = socket
    this.onRequest = onRequest
    this.pending = new Map()
    this.nextId = 0
    let buffer = ''
    socket.setEncoding('utf8')
    socket.on('data', (chunk) => {
      buffer += chunk
      let newline = buffer.indexOf('\n')
      while (newline !== -1) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (line) void this.handleLine(line)
        newline = buffer.indexOf('\n')
      }
    })
    socket.on('error', () => { /* a dropped client is not a runtime fault */ })
    socket.on('close', () => this.failPending(new Error('lara disconnected')))
  }

  get open() {
    return !this.socket.destroyed
  }

  write(message) {
    if (this.open) this.socket.write(`${JSON.stringify(message)}\n`)
  }

  notify(method, params) {
    this.write({ jsonrpc: '2.0', method, params })
  }

  /** Ask the client; rejects when it disconnects or `signal` aborts first. */
  request(method, params, signal) {
    if (!this.open) return Promise.reject(new Error('lara disconnected'))
    const id = `plugin-${++this.nextId}`
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        this.pending.delete(id)
        reject(signal.reason ?? new Error('request aborted'))
      }
      this.pending.set(id, {
        resolve: (value) => { signal?.removeEventListener('abort', onAbort); resolve(value) },
        reject: (error) => { signal?.removeEventListener('abort', onAbort); reject(error) },
      })
      signal?.addEventListener('abort', onAbort, { once: true })
      this.write({ jsonrpc: '2.0', id, method, params })
    })
  }

  close() {
    this.socket.destroy()
  }

  failPending(error) {
    for (const entry of this.pending.values()) entry.reject(error)
    this.pending.clear()
  }

  async handleLine(line) {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      this.write({ jsonrpc: '2.0', id: null, error: { code: JSONRPC_INVALID_REQUEST, message: 'invalid JSON' } })
      return
    }
    if (message === null || typeof message !== 'object' || message.jsonrpc !== '2.0') {
      this.write({ jsonrpc: '2.0', id: message?.id ?? null, error: { code: JSONRPC_INVALID_REQUEST, message: 'invalid JSON-RPC message' } })
      return
    }
    if (typeof message.method !== 'string') {
      const entry = this.pending.get(message.id)
      if (entry === undefined) return
      this.pending.delete(message.id)
      if ('error' in message) entry.reject(new Error(`${message.error?.message} (code ${message.error?.code})`))
      else entry.resolve(message.result)
      return
    }
    const id = message.id ?? null
    try {
      const result = await this.onRequest(this, message.method, message.params ?? {})
      if (id !== null) this.write({ jsonrpc: '2.0', id, result })
    } catch (error) {
      const code = error instanceof MethodNotFound ? JSONRPC_METHOD_NOT_FOUND : JSONRPC_INTERNAL_ERROR
      if (id !== null) this.write({ jsonrpc: '2.0', id, error: { code, message: error instanceof Error ? error.message : String(error) } })
    }
  }
}

class MethodNotFound extends Error {}

/** The plugin's state: connected peers, the Lara subscriber, Lara-owned sessions. */
class LaraWire {
  constructor(ctx) {
    this.ctx = ctx
    this.peers = new Set()
    this.subscriber = null
    this.owned = new Set()
  }

  accept(socket) {
    const peer = new Peer(socket, (from, method, params) => this.dispatch(from, method, params))
    this.peers.add(peer)
    socket.on('close', () => {
      this.peers.delete(peer)
      if (this.subscriber === peer) this.subscriber = null
    })
  }

  closeAll() {
    for (const peer of this.peers) peer.close()
    this.peers.clear()
    this.subscriber = null
  }

  notify(method, params) {
    this.subscriber?.notify(method, params)
  }

  /** The subscriber decides approvals for Lara-owned sessions; the rest fall through. */
  answerApproval(request, next) {
    const subscriber = this.subscriber
    const sessionId = String(request.agent.session.id)
    if (subscriber === null || !this.owned.has(sessionId)) return next()
    const params = {
      sessionId,
      toolName: request.toolName,
      ...(request.callId === undefined ? {} : { callId: request.callId }),
      ...(request.reason === undefined ? {} : { reason: request.reason }),
    }
    return subscriber.request('approval/request', params, request.signal).then((result) => {
      const outcome = result?.outcome
      if (outcome !== 'allowed-once' && outcome !== 'rejected' && outcome !== 'cancelled') {
        throw new TypeError('approval/request response requires an allowed-once, rejected, or cancelled outcome')
      }
      return outcome
    })
  }

  async answerQuestion(request, next) {
    const subscriber = this.subscriber
    const agent = request.agent
    if (subscriber === null || agent === undefined || !this.owned.has(String(agent.session.id))) return next()
    const sessionId = String(agent.session.id)
    const params = { requestId: randomUUID(), sessionId, questions: request.questions }
    const signal = request.signal
    const cancelled = () => subscriber.notify('user-question.cancelled', { requestId: params.requestId, sessionId })
    signal?.addEventListener('abort', cancelled, { once: true })
    try {
      return parseAnswer(await subscriber.request('user-question/ask', params, signal), request.questions.map((q) => q.id))
    } finally {
      signal?.removeEventListener('abort', cancelled)
    }
  }

  async dispatch(peer, method, params) {
    const ctx = this.ctx
    switch (method) {
      case 'lara/ping':
        return { ok: true, name, version: VERSION, pid: process.pid, attach: ctx.get('sessionController') !== undefined }
      case 'lara/events/subscribe':
        this.subscriber = peer
        return { ok: true }
      case 'lara/session/list':
        return { sessions: await listSessions(ctx) }
      case 'lara/session/stat': {
        const sessionId = requireString(params, 'sessionId')
        const sessions = await listSessions(ctx)
        return { session: sessions.find((entry) => entry.sessionId === sessionId) ?? null }
      }
      case 'lara/session/create':
        return createSession(ctx, params)
      case 'lara/session/prompt':
        return this.prompt(params)
      case 'lara/session/resume': {
        const sessionId = requireString(params, 'sessionId')
        const agent = await resolveAgent(ctx, sessionId)
        return { sessionId, status: agent.status }
      }
      case 'lara/session/select-model': {
        const sessionId = requireString(params, 'sessionId')
        const selection = { provider: requireString(params, 'provider'), model: requireString(params, 'model') }
        await selectModel(ctx, await resolveAgent(ctx, sessionId), selection)
        return { sessionId, ...selection }
      }
      case 'lara/session/cancel': {
        const agent = ctx.agents.get(requireString(params, 'sessionId'))
        if (agent === undefined) return { cancelled: false }
        await agent.cancel()
        return { cancelled: true }
      }
      case 'lara/models/list':
        return { providers: await listModels(ctx) }
      case 'lara/model/route':
        return await modelRoute(ctx)
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

  /**
   * Queue one user prompt on a session, creating it first when unknown.
   * Goes through `sessionController` (the web/desktop Host API), so the
   * session gets the deployment's default model and preset and shows up in
   * the dsh UI like any other. The model is dsh's own default
   * (agentDefaultModel — what the web/desktop model picker saves); use
   * lara/session/select-model for an explicit session-local override.
   */
  async prompt(params) {
    const ctx = this.ctx
    const controller = requireService(ctx, 'sessionController')
    const text = requireString(params, 'text')
    let sessionId = typeof params.sessionId === 'string' && params.sessionId !== '' ? params.sessionId : undefined
    let created = false
    const known = sessionId !== undefined
      && (ctx.agents.get(sessionId) !== undefined || (await ctx.sessionPersistence.stat(sessionId)) !== undefined)
    if (!known) {
      const cwd = typeof params.cwd === 'string' && params.cwd !== '' ? params.cwd : undefined
      const value = await controller.create({
        ...(sessionId === undefined ? {} : { sessionId }),
        ...(cwd === undefined ? {} : { cwd }),
      })
      sessionId = String(value.sessionId)
      created = true
    }
    this.owned.add(sessionId)
    const requestId = randomUUID()
    await controller.prompt({
      requestId,
      sessionId,
      mode: 'queue',
      content: [{ type: 'text', text }],
    }, new AbortController().signal)
    return { sessionId, messageId: requestId, created }
  }
}

/** Validate the subscriber's answer the way the dsh SDK validates its host's. */
function parseAnswer(value, questionIds) {
  const bad = () => { throw new Error('answer each question id exactly once with selected labels and optional custom text') }
  if (value !== null && typeof value === 'object' && 'cancelled' in value) {
    if (value.cancelled !== true) bad()
    throw new Error('the user dismissed the question')
  }
  if (value === null || typeof value !== 'object' || !Array.isArray(value.answers)) bad()
  const answers = value.answers.map((answer) => {
    if (answer === null || typeof answer !== 'object' || typeof answer.id !== 'string'
      || !Array.isArray(answer.selected) || !answer.selected.every((label) => typeof label === 'string')
      || ('custom' in answer && typeof answer.custom !== 'string')) bad()
    return { id: answer.id, selected: answer.selected, ...('custom' in answer ? { custom: answer.custom } : {}) }
  })
  const ids = new Set(answers.map((answer) => answer.id))
  if (ids.size !== answers.length || answers.length !== questionIds.length || !questionIds.every((id) => ids.has(id))) bad()
  return { answers }
}

function requireString(params, key) {
  const value = params[key]
  if (typeof value !== 'string' || value === '') throw new Error(`${key} must be a non-empty string`)
  return value
}

function requireService(ctx, key) {
  const service = ctx.get(key)
  if (service === undefined) throw new Error(`the ${key} service is not mounted in this profile`)
  return service
}

/**
 * Session-local model selection, validated against the llm service first.
 * Unlike the UI's `sessionController.selectModel`, dsh's saved default model
 * is left alone: Lara chooses for its own sessions, not for the user.
 */
async function selectModel(ctx, agent, selection) {
  await requireService(ctx, 'llm').resolveCallConfig(selection)
  agent.model.select(selection)
}

/** The live agent for a session, resuming a persisted one through the Host API. */
async function resolveAgent(ctx, sessionId) {
  const live = ctx.agents.get(sessionId)
  if (live !== undefined) return live
  const found = await requireService(ctx, 'sessionController').resolveAgent(sessionId)
  if ('error' in found) throw found.error
  return found.agent
}

/** The official inference origin for the DeepSeek API-key route. */
const OFFICIAL_BASE_URL = 'https://api.deepseek.com/v1'

/**
 * The official model-config view in one call: the saved default selection,
 * the provider catalogs, and the credential material dsh itself would use.
 *
 * `default` is agentDefaultModel (what the web/desktop model picker saves);
 * new sessions ride it unless a caller selects per session. `apiKey` is the
 * deepseek-official credential (credential store first, then the trusted
 * launch environment), and `account` reports whether a DeepSeek account
 * sign-in would authenticate — the account provider's token is host-scoped
 * and speaks the official Messages protocol, not an OpenAI-compatible one,
 * so OpenAI-protocol consumers can only ride the api-key route.
 */
async function modelRoute(ctx) {
  const defaults = ctx.get('agentDefaultModel')
  const selection = defaults !== undefined ? defaults.currentSelection() : null
  const baseURL = (process.env.DEEPSEEK_BASE_URL || '').trim() || OFFICIAL_BASE_URL
  let apiKey = { configured: false, value: null }
  try {
    const credentials = requireService(ctx, 'credentials')
    const described = await credentials.describe('DEEPSEEK_API_KEY')
    apiKey = { configured: Boolean(described.configured), value: null }
    if (apiKey.configured) {
      const resolved = await credentials.resolve('DEEPSEEK_API_KEY')
      apiKey.value = resolved === undefined ? null : resolved.value
    }
  } catch {
    // no credentials service: the launch environment is the only source
    const ambient = (process.env.DEEPSEEK_API_KEY || '').trim()
    apiKey = { configured: ambient.length > 0, value: ambient.length > 0 ? ambient : null }
  }
  let account = { signedIn: false }
  try {
    const deepseekAccount = ctx.get('deepseekAccount')
    if (deepseekAccount !== undefined) {
      // resolveToken only releases the grant for the account's own inference
      // origin, so probe that origin — not the api-key route's baseURL, which
      // DEEPSEEK_BASE_URL may point anywhere.
      account = { signedIn: (await deepseekAccount.resolveToken('https://api.deepseek.com')) !== undefined }
    }
  } catch {
    account = { signedIn: false }
  }
  return {
    default: selection === null ? null : { ...selection },
    providers: await listModels(ctx),
    apiKey,
    account,
    baseURL,
  }
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
