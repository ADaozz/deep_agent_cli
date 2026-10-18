#!/usr/bin/env node
/**
 * Local mock of the documented deep-agent HTTP service, for UI development only.
 *
 * It implements exactly the endpoints in docs/server.md §4 and the SSE frames in
 * §5 in memory, with scripted runs, so the console can be exercised without a
 * real service, sandbox or model. It is NOT part of the product surface: never
 * point production config at it and never treat its responses as authoritative.
 *
 * Usage:  npm run mock   (MOCK_TOKEN / PORT env overrides)
 */
import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'

const PORT = Number(process.env.PORT || 8099)
const TOKEN = process.env.MOCK_TOKEN || 'dev-token'
const MAX_RUNS = 2

const projects = [{ id: 'app' }, { id: 'worker' }]
const sessions = new Map()
const runs = new Map()
const events = []
const listeners = new Set()
const waiters = new Map()
const checkpoints = new Map() // sessionId -> interruption snapshot used by resume=true
let eventId = 0

const now = () => new Date().toISOString()
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted'])

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
  'Access-Control-Allow-Headers': 'authorization,content-type,last-event-id',
  'Access-Control-Max-Age': '600',
}

function json(res, status, data) {
  res.writeHead(status, { ...cors, 'Content-Type': 'application/json; charset=utf-8' })
  res.end(JSON.stringify(data))
}

function emit(runId, type, data) {
  const event = { id: ++eventId, runId, type, data }
  events.push(event)
  for (const listener of [...listeners]) listener(event)
}

function snapshot(run) {
  // Only the documented Run fields; mock bookkeeping stays internal.
  return {
    id: run.id,
    session_id: run.session_id,
    project_id: run.project_id,
    status: run.status,
    created_at: run.created_at,
    updated_at: run.updated_at,
    result: JSON.parse(JSON.stringify(run.result ?? {})),
    interrupt_id: run.interrupt_id ?? null,
  }
}

function setRun(runId, patch) {
  const run = runs.get(runId)
  Object.assign(run, patch, { updated_at: now() })
  return run
}

function finish(runId, status, result) {
  const run = setRun(runId, { status, result, interrupt_id: null })
  emit(runId, 'run_finished', snapshot(run))
  const session = sessions.get(run.session_id)
  if (session && status === 'interrupted') {
    checkpoints.set(run.session_id, checkpoints.get(run.session_id) || null)
  }
}

function liveRuns() {
  return [...runs.values()].filter((run) => !TERMINAL.has(run.status))
}

function newRun(session, text, resume) {
  const stamp = now()
  const run = {
    id: randomUUID(),
    session_id: session.id,
    project_id: session.project_id,
    status: 'running',
    created_at: stamp,
    updated_at: stamp,
    result: {},
    interrupt_id: null,
    cancelRequested: false,
    text,
    resume,
  }
  runs.set(run.id, run)
  emit(run.id, 'run_started', snapshot(run))
  return run
}

async function pause(runId, ms) {
  const run = runs.get(runId)
  const end = Date.now() + ms
  while (Date.now() < end) {
    if (run.cancelRequested) return true
    await delay(Math.min(60, Math.max(10, end - Date.now())))
  }
  return run.cancelRequested
}

function delta(runId, kind, text) {
  emit(runId, 'delta', { kind, text })
}

function agent(runId, payload) {
  emit(runId, 'agent_event', {
    type: payload.type,
    content: payload.content ?? '',
    message_id: '',
    tool_call_id: payload.tool_call_id ?? '',
    name: payload.name ?? '',
    arguments: payload.arguments ?? {},
    result: payload.result ?? {},
    is_error: payload.is_error ?? false,
    stream: payload.stream ?? '',
    artifact: payload.artifact ?? {},
  })
}

function waitInteraction(runId) {
  return new Promise((resolve) => waiters.set(runId, resolve))
}

function cancelResult() {
  return { status: 'cancelled', output: '', pending_tool_calls: [], human_input: {}, error: '' }
}

async function openInteraction(runId, status, command) {
  const run = runs.get(runId)
  const interruptId = randomUUID()
  setRun(runId, { status, interrupt_id: interruptId, result: command })
  emit(runId, 'interaction_requested', snapshot(run))
  const decision = await waitInteraction(runId)
  return decision
}

function approvalCommand() {
  return {
    status: 'waiting_confirmation',
    output: '',
    pending_tool_calls: [
      {
        toolCallId: `call-${randomUUID().slice(0, 8)}`,
        name: 'read_file',
        args: { file_path: '/workspace/app/src/config.ts' },
      },
      {
        toolCallId: `call-${randomUUID().slice(0, 8)}`,
        name: 'search_files',
        args: { pattern: 'loadConfig(', path: '/workspace/app/src' },
      },
      {
        toolCallId: `call-${randomUUID().slice(0, 8)}`,
        name: 'write_file',
        args: {
          file_path: '/workspace/app/docs/config-notes.md',
          content: '# Config notes\n\n- the loader ignored CONFIG_PATH\n',
        },
      },
      {
        toolCallId: `call-${randomUUID().slice(0, 8)}`,
        name: 'execute',
        args: { command: 'npm test -- --reporter=dot', network: false },
      },
    ],
    human_input: {},
    error: '',
  }
}

function humanCommand() {
  return {
    status: 'waiting_human',
    output: '',
    pending_tool_calls: [],
    human_input: {
      type: 'human_input',
      interactionType: 'clarification',
      title: 'Confirm the scope',
      reason: 'I need to bound which directories may change so nothing outside the plan is touched.',
      question: 'Which directories may I change, and should I back up first?',
      blocking: true,
      impact: ['Files under the selected directories may be edited.', 'A write tool call runs after you submit.'],
      recommendation: { value: 'tests', reason: 'Changing only the test directory is the lowest risk.' },
      fields: [
        {
          id: 'scope',
          type: 'multi_select',
          label: 'Scope',
          required: true,
          placeholder: '',
          options: [
            { value: 'tests', label: 'tests/', description: 'test cases under tests/' },
            { value: 'src', label: 'src/', description: 'implementation under src/' },
            { value: '__other__', label: 'Other:', description: '', allowsCustomText: true },
          ],
        },
        {
          id: 'notes',
          type: 'textarea',
          label: 'Notes',
          required: false,
          placeholder: 'Optional: extra requirements or constraints',
          options: [],
        },
        {
          id: 'confirmed',
          type: 'boolean',
          label: 'Already backed up',
          required: true,
          placeholder: '',
          options: [],
        },
      ],
    },
    error: '',
  }
}

const TOOL_OUTPUT = {
  read_file: 'export function loadConfig() {\n  return readJson("config.json")\n}\n',
  search_files: 'src/config.ts:12\nsrc/main.ts:4',
  write_file: 'Wrote 4 lines',
  execute: 'Tests: 12 passed, 1 failed',
}

function answerMarkdown() {
  return [
    '### What I found',
    '',
    '`loadConfig()` in **src/config.ts** ignores the `CONFIG_PATH` override, so the test fixture is never read.',
    '',
    '1. Read `src/config.ts` and the failing test',
    '2. Confirmed the only two call sites',
    '3. Wrote the notes down and re-ran the suite',
    '',
    '| File | Planned change |',
    '| --- | --- |',
    '| `src/config.ts` | honour `CONFIG_PATH` before the default file |',
    '| `docs/config-notes.md` | notes on the loader order |',
    '',
    '12 of 13 tests pass. The remaining one needs `tests/fixtures/config.json`, which is missing from the repo.',
    'See the [config loader docs](https://example.com/config) for the documented precedence order.',
  ].join('\n')
}

async function runApprovalFlow(runId, text) {
  const long = text.includes('长') || text.toLowerCase().includes('long')
  agent(runId, { type: 'assistant_started' })
  for (const value of [
    'Looking at the config loader',
    'Looking at the config loader and the failing test',
  ]) {
    if (await pause(runId, 260)) return finish(runId, 'cancelled', cancelResult())
    delta(runId, 'reasoning', value)
  }
  delta(runId, 'assistant', 'I will read the loader, find its call sites, note what I see and run the tests.')

  const command = approvalCommand()
  if (long) {
    command.pending_tool_calls.push({
      toolCallId: `call-${randomUUID().slice(0, 8)}`,
      name: 'execute',
      args: { command: 'npm run build -- --verbose', network: true },
    })
  }
  const decision = await openInteraction(runId, 'waiting_confirmation', command)
  if (decision.kind === 'cancel') return finish(runId, 'cancelled', cancelResult())

  setRun(runId, { status: 'running', interrupt_id: null, result: {} })
  emit(runId, 'interaction_submitted', { interrupt_id: decision.interruptId })
  emit(runId, 'agent_event', { type: 'assistant_started', content: '', message_id: '', tool_call_id: '', name: '', arguments: {}, result: {}, is_error: false, stream: '', artifact: {} })

  for (const call of command.pending_tool_calls) {
    const choice = decision.decisions.find((item) => item.tool_call_id === call.toolCallId)
    if (choice?.decision === 'approve') {
      agent(runId, { type: 'tool_started', tool_call_id: call.toolCallId, name: call.name, arguments: call.args })
      if (await pause(runId, 320)) return finish(runId, 'cancelled', cancelResult())
      if (long && call.name === 'execute' && String(call.args.command).includes('build')) {
        for (const chunk of ['> build started\n', '> compiling 42 modules\n', '> bundling assets\n']) {
          agent(runId, { type: 'tool_output_delta', tool_call_id: call.toolCallId, content: chunk, stream: 'merged' })
        }
        agent(runId, {
          type: 'tool_output_delta',
          tool_call_id: call.toolCallId,
          content: '… earlier output omitted …\n> build finished in 12.4s\n',
          stream: 'tail_snapshot',
        })
        agent(runId, {
          type: 'tool_completed',
          tool_call_id: call.toolCallId,
          name: call.name,
          content: '> build finished in 12.4s\n',
          is_error: false,
          artifact: {
            exit_code: 0,
            truncated: true,
            termination_reason: 'max_output_bytes',
            max_output_bytes: 120000,
            host_log_path: '/srv/projects/app/.deep-agent/logs/build.log',
          },
        })
      } else {
        const content = TOOL_OUTPUT[call.name] ?? `${call.name} finished`
        agent(runId, {
          type: 'tool_output_delta',
          tool_call_id: call.toolCallId,
          content: call.name === 'execute' ? 'running tests…\n' : '',
          stream: 'merged',
        })
        agent(runId, {
          type: 'tool_completed',
          tool_call_id: call.toolCallId,
          name: call.name,
          content,
          is_error: false,
          artifact: call.name === 'execute' ? { exit_code: 1, truncated: false } : {},
        })
      }
    } else {
      agent(runId, {
        type: 'tool_completed',
        tool_call_id: call.toolCallId,
        name: call.name,
        content: `Denied by the user: ${choice?.message || 'no reason given'}`,
        is_error: true,
        artifact: { exit_code: null },
      })
    }
  }

  const answer = answerMarkdown()
  delta(runId, 'assistant', answer)
  agent(runId, { type: 'assistant_completed', content: answer, result: { thinking: 'Read the loader and the test, then the tests.' } })
  agent(runId, { type: 'usage', result: { input_tokens: 1832, output_tokens: 214, total_tokens: 2046 } })
  agent(runId, { type: 'turn_completed', content: answer, result: { elapsed_seconds: 4.2, finished_at: now() } })
  finish(runId, 'completed', {
    status: 'completed',
    output: answer,
    pending_tool_calls: [],
    human_input: {},
    error: '',
  })
}

async function runHumanFlow(runId) {
  agent(runId, { type: 'assistant_started' })
  for (const value of ['Checking the scope', 'Checking the scope and whether a backup exists']) {
    if (await pause(runId, 260)) return finish(runId, 'cancelled', cancelResult())
    delta(runId, 'reasoning', value)
  }
  delta(runId, 'assistant', 'Before I touch anything I need the scope and the backup state.')

  const command = humanCommand()
  const decision = await openInteraction(runId, 'waiting_human', command)
  if (decision.kind === 'cancel') return finish(runId, 'cancelled', cancelResult())

  setRun(runId, { status: 'running', interrupt_id: null, result: {} })
  emit(runId, 'interaction_submitted', { interrupt_id: decision.interruptId })
  emit(runId, 'agent_event', { type: 'assistant_started', content: '', message_id: '', tool_call_id: '', name: '', arguments: {}, result: {}, is_error: false, stream: '', artifact: {} })

  const values = decision.values || {}
  const scope = Array.isArray(values.scope) ? values.scope.join(', ') : String(values.scope ?? '')
  agent(runId, {
    type: 'tool_started',
    tool_call_id: 'call-human-write',
    name: 'write_file',
    arguments: { file_path: '/workspace/app/docs/scope.md', content: `scope: ${scope}` },
  })
  if (await pause(runId, 300)) return finish(runId, 'cancelled', cancelResult())
  agent(runId, { type: 'tool_output_delta', tool_call_id: 'call-human-write', content: 'writing file…\n', stream: 'merged' })
  agent(runId, {
    type: 'tool_completed',
    tool_call_id: 'call-human-write',
    name: 'write_file',
    content: 'Wrote 2 lines',
    is_error: false,
    artifact: {},
  })
  const output = `Continuing with scope **${scope || 'not given'}**; backup: ${values.confirmed === true ? 'done' : 'not done'}.`
  delta(runId, 'assistant', output)
  agent(runId, { type: 'assistant_completed', content: output, result: { thinking: 'Confirmed the scope and the backup state.' } })
  agent(runId, { type: 'usage', result: { input_tokens: 940, output_tokens: 96, total_tokens: 1036 } })
  agent(runId, { type: 'turn_completed', content: output, result: { elapsed_seconds: 2.6, finished_at: now() } })
  finish(runId, 'completed', {
    status: 'completed',
    output,
    pending_tool_calls: [],
    human_input: {},
    error: '',
  })
}

async function runInterruptDemo(runId, sessionId) {
  const session = sessions.get(sessionId)
  agent(runId, { type: 'assistant_started' })
  if (await pause(runId, 200)) return finish(runId, 'cancelled', cancelResult())
  delta(runId, 'assistant', 'Reading the project layout…')
  const run = runs.get(runId)
  const command = approvalCommand()
  updateCheckpoint(sessionId, command)
  return finish(runId, 'interrupted', {
    status: 'interrupted',
    output: run.result?.output || '',
    pending_tool_calls: [],
    human_input: {},
    error: 'Service restarted; resume the session explicitly',
  })
}

function updateCheckpoint(sessionId, result) {
  checkpoints.set(sessionId, { status: result.status, result })
}

async function runResumeFlow(runId, sessionId) {
  const checkpoint = checkpoints.get(sessionId)
  if (!checkpoint) {
    if (await pause(runId, 260)) return finish(runId, 'cancelled', cancelResult())
    return finish(runId, 'failed', {
      status: 'failed',
      output: '',
      pending_tool_calls: [],
      human_input: {},
      error: 'Session has no interruption to resume; submit a new text task',
    })
  }
  checkpoints.delete(sessionId)
  const command = JSON.parse(JSON.stringify(checkpoint.result))
  const decision = await openInteraction(runId, checkpoint.status, command)
  if (decision.kind === 'cancel') return finish(runId, 'cancelled', cancelResult())
  if (checkpoint.status === 'waiting_confirmation') {
    setRun(runId, { status: 'running', interrupt_id: null, result: {} })
    emit(runId, 'interaction_submitted', { interrupt_id: decision.interruptId })
    emit(runId, 'agent_event', { type: 'assistant_started', content: '', message_id: '', tool_call_id: '', name: '', arguments: {}, result: {}, is_error: false, stream: '', artifact: {} })
    for (const call of command.pending_tool_calls) {
      agent(runId, {
        type: 'tool_completed',
        tool_call_id: call.toolCallId,
        name: call.name,
        content: 'resumed and skipped in mock',
        is_error: false,
        artifact: { exit_code: 0 },
      })
    }
    const output = 'Resumed the interrupted session and finished the remaining steps (the mock does not execute tools).'
    delta(runId, 'assistant', output)
    agent(runId, { type: 'assistant_completed', content: output, result: {} })
    return finish(runId, 'completed', {
      status: 'completed',
      output,
      pending_tool_calls: [],
      human_input: {},
      error: '',
    })
  }
  setRun(runId, { status: 'running', interrupt_id: null, result: {} })
  emit(runId, 'interaction_submitted', { interrupt_id: decision.interruptId })
  return finish(runId, 'completed', {
    status: 'completed',
    output: 'Resumed from the checkpoint and finished (mock).',
    pending_tool_calls: [],
    human_input: {},
    error: '',
  })
}

async function runPauseFlow(runId) {
  agent(runId, { type: 'assistant_started' })
  if (await pause(runId, 220)) return finish(runId, 'cancelled', cancelResult())
  delta(runId, 'reasoning', 'Finish stage one, then wait for the user before continuing.')
  const staged = 'Stage one is done: dependencies installed and the static checks pass.'
  delta(runId, 'assistant', staged)
  agent(runId, { type: 'assistant_completed', content: staged, result: {} })

  const decision = await openInteraction(runId, 'paused', {
    status: 'paused',
    output: staged,
    pending_tool_calls: [],
    human_input: {},
    error: '',
  })
  if (decision.kind === 'cancel') return finish(runId, 'cancelled', cancelResult())
  if (decision.kind !== 'continue') {
    return finish(runId, 'failed', {
      status: 'failed',
      output: staged,
      pending_tool_calls: [],
      human_input: {},
      error: 'Mock expected a continue decision',
    })
  }

  setRun(runId, { status: 'running', interrupt_id: null, result: {} })
  emit(runId, 'interaction_submitted', { interrupt_id: decision.interruptId })
  emit(runId, 'agent_event', { type: 'assistant_started', content: '', message_id: '', tool_call_id: '', name: '', arguments: {}, result: {}, is_error: false, stream: '', artifact: {} })
  if (await pause(runId, 240)) return finish(runId, 'cancelled', cancelResult())
  const output = 'Continued from the checkpoint: stage two build finished (mock).'
  delta(runId, 'assistant', output)
  agent(runId, { type: 'assistant_completed', content: output, result: {} })
  agent(runId, { type: 'usage', result: { input_tokens: 1024, output_tokens: 88, total_tokens: 1112 } })
  agent(runId, { type: 'turn_completed', content: output, result: { elapsed_seconds: 1.4, finished_at: now() } })
  finish(runId, 'completed', {
    status: 'completed',
    output,
    pending_tool_calls: [],
    human_input: {},
    error: '',
  })
}

async function runFlow(runId, text, resume) {
  const run = runs.get(runId)
  const lower = text.toLowerCase()
  try {
    if (resume) {
      await runResumeFlow(runId, run.session_id)
      return
    }
    if (text.includes('中断') || lower.includes('interrupt')) {
      await runInterruptDemo(runId, run.session_id)
      return
    }
    if (text.includes('暂停') || lower.includes('pause')) {
      await runPauseFlow(runId)
      return
    }
    if (text.includes('人工') || text.includes('输入') || text.includes('询问') || lower.includes('ask me')) {
      await runHumanFlow(runId)
      return
    }
    await runApprovalFlow(runId, text)
  } catch (error) {
    const current = runs.get(runId)
    if (current && !TERMINAL.has(current.status)) {
      finish(runId, 'failed', {
        status: 'failed',
        output: '',
        pending_tool_calls: [],
        human_input: {},
        error: `Mock flow error: ${error.message}`,
      })
    }
  }
}

// ------------------------------------------------------------------ HTTP layer

async function readBody(req) {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  if (!chunks.length) return {}
  return JSON.parse(Buffer.concat(chunks).toString('utf8'))
}

function badRequest(res, detail) {
  json(res, 422, { detail })
}

function unauthorized(res) {
  res.writeHead(401, { ...cors, 'WWW-Authenticate': 'Bearer', 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ detail: 'Invalid service token' }))
}

function streamEvents(req, res, runId, after) {
  res.writeHead(200, {
    ...cors,
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    'X-Accel-Buffering': 'no',
    Connection: 'keep-alive',
  })
  let cursor = after
  const write = (event) => {
    res.write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event.data)}\n\n`)
    cursor = event.id
  }
  for (const event of events) {
    if (event.runId === runId && event.id > cursor) write(event)
  }
  const run = runs.get(runId)
  if (TERMINAL.has(run.status)) {
    res.end()
    return
  }
  const listener = (event) => {
    if (event.runId !== runId || event.id <= cursor) return
    write(event)
    if (event.type === 'run_finished') {
      cleanup()
      res.end()
    }
  }
  const heartbeat = setInterval(() => res.write(': keepalive\n\n'), 15000)
  function cleanup() {
    clearInterval(heartbeat)
    listeners.delete(listener)
  }
  listeners.add(listener)
  req.on('close', cleanup)
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`)
  const path = url.pathname
  const method = req.method || 'GET'

  if (method === 'OPTIONS') {
    res.writeHead(204, cors)
    res.end()
    return
  }

  if (path === '/health') {
    json(res, 200, { status: 'ok' })
    return
  }

  if (!path.startsWith('/v1/')) {
    json(res, 404, { detail: 'Not Found' })
    return
  }

  const header = req.headers.authorization || ''
  if (header !== `Bearer ${TOKEN}`) {
    unauthorized(res)
    return
  }

  try {
    if (method === 'GET' && path === '/v1/projects') {
      json(res, 200, { projects })
      return
    }

    if (method === 'POST' && path === '/v1/sessions') {
      const body = await readBody(req)
      const extra = Object.keys(body).filter((key) => key !== 'project_id')
      if (extra.length || typeof body.project_id !== 'string') {
        badRequest(res, 'Body must be {"project_id": string}')
        return
      }
      if (!projects.some((project) => project.id === body.project_id)) {
        json(res, 404, { detail: 'Unknown project' })
        return
      }
      const session = { id: randomUUID(), project_id: body.project_id, created_at: now() }
      sessions.set(session.id, session)
      json(res, 201, session)
      return
    }

    if (method === 'GET' && path === '/v1/sessions') {
      const projectId = url.searchParams.get('project_id')
      if (projectId && !projects.some((project) => project.id === projectId)) {
        json(res, 404, { detail: 'Unknown project' })
        return
      }
      const list = [...sessions.values()]
        .filter((session) => !projectId || session.project_id === projectId)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 100)
      json(res, 200, { sessions: list })
      return
    }

    const sessionMatch = /^\/v1\/sessions\/([^/]+)$/.exec(path)
    if (method === 'GET' && sessionMatch) {
      const session = sessions.get(sessionMatch[1])
      if (!session) json(res, 404, { detail: 'Unknown session' })
      else json(res, 200, session)
      return
    }

    const submitMatch = /^\/v1\/sessions\/([^/]+)\/runs$/.exec(path)
    if (method === 'POST' && submitMatch) {
      const session = sessions.get(submitMatch[1])
      if (!session) {
        json(res, 404, { detail: 'Unknown session' })
        return
      }
      const body = await readBody(req)
      const extra = Object.keys(body).filter((key) => !['text', 'resume'].includes(key))
      const resume = body.resume === true
      const text = body.text
      if (extra.length) {
        badRequest(res, `Extra field ${extra.join(',')}`)
        return
      }
      if (resume === (text !== undefined && text !== null)) {
        badRequest(res, 'Provide either nonempty text or resume=true')
        return
      }
      if (!resume) {
        if (typeof text !== 'string' || !text.trim()) {
          badRequest(res, 'text must not be blank')
          return
        }
        if (text.length > 100000) {
          badRequest(res, 'text too long')
          return
        }
      }
      if (liveRuns().some((run) => run.project_id === session.project_id)) {
        json(res, 409, { detail: 'Project already has an unfinished run' })
        return
      }
      if (liveRuns().length >= MAX_RUNS) {
        json(res, 429, { detail: 'Run capacity reached' })
        return
      }
      const run = newRun(session, resume ? '' : text, resume)
      void runFlow(run.id, resume ? '' : text, resume)
      json(res, 202, snapshot(run))
      return
    }

    const runMatch = /^\/v1\/runs\/([^/]+)$/.exec(path)
    if (method === 'GET' && runMatch) {
      const run = runs.get(runMatch[1])
      if (!run) json(res, 404, { detail: 'Unknown run' })
      else json(res, 200, snapshot(run))
      return
    }

    const eventsMatch = /^\/v1\/runs\/([^/]+)\/events$/.exec(path)
    if (method === 'GET' && eventsMatch) {
      const run = runs.get(eventsMatch[1])
      if (!run) {
        json(res, 404, { detail: 'Unknown run' })
        return
      }
      let after = Number(url.searchParams.get('after') || 0)
      const lastEventId = req.headers['last-event-id']
      if (typeof lastEventId === 'string') {
        if (!/^\d+$/.test(lastEventId)) {
          json(res, 422, { detail: 'Last-Event-ID must be a nonnegative integer' })
          return
        }
        after = Number(lastEventId)
      }
      if (!Number.isInteger(after) || after < 0) {
        json(res, 422, { detail: 'after must be a nonnegative integer' })
        return
      }
      streamEvents(req, res, run.id, after)
      return
    }

    const approvalMatch = /^\/v1\/runs\/([^/]+)\/approvals$/.exec(path)
    if (method === 'POST' && approvalMatch) {
      const run = runs.get(approvalMatch[1])
      if (!run) {
        json(res, 404, { detail: 'Unknown run' })
        return
      }
      const body = await readBody(req)
      if (run.status !== 'waiting_confirmation' || run.interrupt_id !== body.interrupt_id || !waiters.has(run.id)) {
        json(res, 409, { detail: 'Interaction is no longer pending' })
        return
      }
      const pending = run.result.pending_tool_calls.map((call) => call.toolCallId)
      const ids = (body.decisions || []).map((item) => item.tool_call_id)
      if (
        !Array.isArray(body.decisions) ||
        body.decisions.length !== pending.length ||
        new Set(ids).size !== ids.length ||
        pending.some((id) => !ids.includes(id)) ||
        body.decisions.some((item) => !['approve', 'reject'].includes(item.decision))
      ) {
        json(res, 409, { detail: 'Decisions must cover each pending tool exactly once' })
        return
      }
      const interruptId = body.interrupt_id
      setRun(run.id, { status: 'running', interrupt_id: null, result: {} })
      emit(run.id, 'interaction_submitted', { interrupt_id: interruptId })
      const waiter = waiters.get(run.id)
      waiters.delete(run.id)
      waiter({ kind: 'approvals', interruptId, decisions: body.decisions })
      json(res, 202, snapshot(run))
      return
    }

    const humanMatch = /^\/v1\/runs\/([^/]+)\/human-input$/.exec(path)
    if (method === 'POST' && humanMatch) {
      const run = runs.get(humanMatch[1])
      if (!run) {
        json(res, 404, { detail: 'Unknown run' })
        return
      }
      const body = await readBody(req)
      if (run.status !== 'waiting_human' || run.interrupt_id !== body.interrupt_id || !waiters.has(run.id)) {
        json(res, 409, { detail: 'Interaction is no longer pending' })
        return
      }
      if (!body.values || typeof body.values !== 'object' || Array.isArray(body.values)) {
        badRequest(res, 'values must be an object')
        return
      }
      for (const field of run.result.human_input.fields || []) {
        if (!field.required) continue
        const value = body.values[field.id]
        const empty =
          value === undefined ||
          value === null ||
          (typeof value === 'string' && !value.trim()) ||
          (Array.isArray(value) && value.length === 0)
        if (empty) {
          badRequest(res, `Field ${field.id} is required`)
          return
        }
      }
      const interruptId = body.interrupt_id
      setRun(run.id, { status: 'running', interrupt_id: null, result: {} })
      emit(run.id, 'interaction_submitted', { interrupt_id: interruptId })
      const waiter = waiters.get(run.id)
      waiters.delete(run.id)
      waiter({ kind: 'human', interruptId, values: body.values })
      json(res, 202, snapshot(run))
      return
    }

    const continueMatch = /^\/v1\/runs\/([^/]+)\/continue$/.exec(path)
    if (method === 'POST' && continueMatch) {
      const run = runs.get(continueMatch[1])
      if (!run) {
        json(res, 404, { detail: 'Unknown run' })
        return
      }
      const body = await readBody(req)
      if (
        !['paused', 'runtime_config_boundary'].includes(run.status) ||
        run.interrupt_id !== body.interrupt_id ||
        !waiters.has(run.id)
      ) {
        json(res, 409, { detail: 'Interaction is no longer pending' })
        return
      }
      const interruptId = body.interrupt_id
      setRun(run.id, { status: 'running', interrupt_id: null, result: {} })
      emit(run.id, 'interaction_submitted', { interrupt_id: interruptId })
      const waiter = waiters.get(run.id)
      waiters.delete(run.id)
      waiter({ kind: 'continue', interruptId })
      json(res, 202, snapshot(run))
      return
    }

    const cancelMatch = /^\/v1\/runs\/([^/]+)\/cancel$/.exec(path)
    if (method === 'POST' && cancelMatch) {
      const run = runs.get(cancelMatch[1])
      if (!run) {
        json(res, 404, { detail: 'Unknown run' })
        return
      }
      if (TERMINAL.has(run.status)) {
        json(res, 409, { detail: 'Run has already ended' })
        return
      }
      run.cancelRequested = true
      setRun(run.id, { status: 'cancelling' })
      const waiter = waiters.get(run.id)
      if (waiter) {
        waiters.delete(run.id)
        waiter({ kind: 'cancel' })
      }
      json(res, 202, snapshot(run))
      return
    }

    json(res, 404, { detail: 'Not Found' })
  } catch (error) {
    console.error('[mock] request failed', error)
    json(res, 500, { detail: 'Mock server error' })
  }
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock] deep-agent mock listening on http://127.0.0.1:${PORT}`)
  console.log(`[mock] token: ${TOKEN}  (send Authorization: Bearer ${TOKEN})`)
  console.log(
    '[mock] prompts: default → 4 tool approvals + markdown answer; "ask me" → human input; "pause" → paused + continue; "interrupt" → interrupted (Chinese keywords still work)',
  )
})
