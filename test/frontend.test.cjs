'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');

function renderer() {
  const messages = [];
  const ctx = vm.createContext({
    state: { lastTrace: {} }, DOMAINS: { hr: { title: 'HR' } }, STAGES: ['m1', 'skills', 'm2', 'v1'],
    markHot() {}, stage() {}, showEvidence() {}, setText() {}, uid: () => 'test',
    pushMsg: msg => messages.push(msg),
  });
  vm.runInContext(source.slice(source.indexOf('function applyLiveResult('), source.indexOf('async function liveRun(')), ctx);
  return result => {
    ctx.applyLiveResult(result, 'query', { s: {}, typing: { remove() {} }, hints: [] });
    return messages.at(-1);
  };
}

const result = {
  status: 'completed', routing: {}, skills: [{ domain: 'hr', answer: 'Raw department draft.' }],
  response: 'Final merged answer.', draft: 'Unverified draft.', citations: [],
  verification: { status: 'passed' },
};

test('renders the final verified response, not raw department output', () => {
  const msg = renderer()(result);
  assert.equal(msg.sections.map(s => s.text).join(' '), 'Final merged answer.');
  assert.equal(msg.badge, 'verified');
});

test('uncertain output shows the checked draft with a review badge', () => {
  const msg = renderer()({ ...result, status: 'needs_review', response: null, verification: { status: 'uncertain' } });
  assert.equal(msg.sections[0].text, 'Unverified draft.');
  assert.equal(msg.badge, 'review');
});

test('failed grounding never displays the rejected answer as useful prose', () => {
  const msg = renderer()({ ...result, status: 'needs_review', response: null, verification: { status: 'failed' } });
  assert.doesNotMatch(msg.sections.map(s => s.text).join(' '), /Raw department|Unverified draft/);
  assert.notEqual(msg.badge, 'verified');
});

test('merge failure retains explicitly unverified department fallback', () => {
  const msg = renderer()({ ...result, status: 'aggregation_unavailable', response: null, draft: null,
    verification: { status: 'not_run' } });
  assert.equal(msg.sections[0].text, 'Raw department draft.');
  assert.equal(msg.badge, 'review');
});

async function streamRun(parts) {
  let rendered;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({ start(controller) {
    for (const part of parts) controller.enqueue(encoder.encode(part));
    controller.close();
  } });
  const ctx = vm.createContext({
    TextDecoder, API: '', state: { user: { role: 'user' }, available: new Set(['hr']) },
    stage() {}, liveStage() {}, apiFetch: async () => ({ ok: true, body: stream }),
    applyLiveResult: value => { rendered = value; },
  });
  vm.runInContext(source.slice(source.indexOf('async function liveRun('), source.indexOf('async function runPipeline(')), ctx);
  await ctx.liveRun('salary', null, { s: { clarifyAttempts: 0 } });
  return rendered;
}

test('SSE accepts fragmented CRLF frames', async () => {
  const output = await streamRun(['event: result\r', '\ndata: {"status":"completed"}\r\n\r', '\n']);
  assert.equal(output.status, 'completed');
});

test('SSE accepts LF frames and ignores malformed telemetry', async () => {
  const output = await streamRun(['event: stage\ndata: invalid\n\nevent: result\ndata: {"status":"completed"}\n\n']);
  assert.equal(output.status, 'completed');
});

test('SSE rejects streams without a final result', async () => {
  await assert.rejects(streamRun(['event: done\ndata: {}\n\n']), /without a result/);
});
