// Run from the project root: node --test tests/frontend_conversation.mjs
// Uses the frontend's existing esbuild/React dependencies; no browser or network.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const frontend = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend');
const requireFrontend = createRequire(join(frontend, 'package.json'));
const { buildSync } = requireFrontend('esbuild');
const React = requireFrontend('react');
const { renderToStaticMarkup } = requireFrontend('react-dom/server');

function loadSource(name) {
  const { outputFiles } = buildSync({
    entryPoints: [join(frontend, 'src', name)], absWorkingDir: frontend,
    bundle: true, platform: 'node', format: 'cjs', packages: 'external',
    jsx: 'automatic', loader: { '.css': 'empty' }, write: false,
  });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputFiles[0].text)(requireFrontend, module, module.exports);
  return module.exports;
}
const { conversationItems } = loadSource('conversation.ts');
const { runTiming } = loadSource('runTiming.ts');
const { default: RunTimeline, MediaSkillRecords } = loadSource('RunTimeline.tsx');
const projectId = 'project-test';

const job = (id, created_at, overrides = {}) => ({
  id, project_id: projectId, revision: 1, kind: 'plan', status: 'ready',
  phase: 'plan_ready', error: null, progress: {}, created_at,
  started_at: created_at, updated_at: created_at + 10, ...overrides,
});
const event = (seq, job_id, created_at, kind, state, label = `${kind}-${seq}`) => ({
  seq, project_id: projectId, job_id, created_at, kind, state, label,
});
const message = (id, role, created_at, job_id = null) => ({ id, role, created_at, job_id, content: id });
const gate = (id, job_id, created_at, overrides = {}) => ({
  id, project_id: projectId, job_id, created_at, kind: 'clarification',
  status: 'answered', message: '请确认', questions: [], options: [],
  answers: [], answered_at: null, ...overrides,
});
const detail = (overrides = {}) => ({
  project: { id: projectId }, assets: [], renders: [],
  messages: [], jobs: [], gates: [], events: [], ...overrides,
});
const order = items => items.map(item => `${item.type}:${item.id}`);
const timelineMarkup = item => renderToStaticMarkup(React.createElement(RunTimeline, { job: item.job, events: item.events }));
const mediaSkill = (stage, overrides = {}) => ({ id: `ads-video-${stage}`, display_name: stage === 'asset_prompts' ? 'Ads Video Asset Prompts' : 'Ads Video Video Prompts',
  version: '1.0.0', mode: 'compiler', stage, content_sha256: `saved-${stage}-hash`, ...overrides });

test('retried jobs measure the latest attempt rather than the failed waiting interval', () => {
  const current = job('retry', 10, { started_at: 10, updated_at: 745 });
  const rows = [
    event(1, 'retry', 10, 'status', 'running', '开始执行任务'),
    event(2, 'retry', 35, 'terminal', 'failed'),
    event(3, 'retry', 700, 'status', 'running', '开始执行任务'),
    event(4, 'retry', 710, 'step', 'running', '检查分镜'),
    event(5, 'retry', 745, 'terminal', 'done'),
  ];
  const snapshot = structuredClone(rows);
  assert.deepEqual(runTiming(current, rows), { start: 700, end: 745 });
  assert.ok(timelineMarkup({ job: current, events: rows }).includes('>45s</time>'));
  assert.deepEqual(rows, snapshot, 'the historical first attempt remains intact');
});

test('ordinary running steps and running labels never reset an attempt clock', () => {
  const current = job('running', 10, { status: 'running', updated_at: 35 });
  const rows = [event(1, 'running', 10, 'status', 'running', '开始执行任务'),
    event(2, 'running', 20, 'step', 'running', '编排画面'),
    event(3, 'running', 30, 'status', 'running', '检查声画对应')];
  assert.equal(runTiming(current, rows).start, 10);
  rows.push(event(4, 'running', 34, 'step', 'running', '开始执行任务'));
  assert.equal(runTiming(current, rows).start, 10, 'a matching label alone is not a claim event');
});

test('a retried attempt ignores old terminal and pause events but stops at its own pause', () => {
  const current = job('paused-retry', 10, { updated_at: 800 });
  const rows = [event(1, current.id, 10, 'status', 'running', '开始执行任务'),
    event(2, current.id, 30, 'gate', 'waiting'), event(3, current.id, 35, 'terminal', 'failed'),
    event(4, current.id, 700, 'status', 'running', '开始执行任务')];
  assert.deepEqual(runTiming(current, rows), { start: 700, end: 800 });
  rows.push(event(5, current.id, 730, 'gate', 'waiting'), event(6, current.id, 799, 'gate', 'done'));
  assert.deepEqual(runTiming(current, rows), { start: 700, end: 730 });
  rows.push(event(7, current.id, 800, 'terminal', 'done'));
  assert.deepEqual(runTiming(current, rows), { start: 700, end: 730 });
});

test('legacy jobs fall back to stored starts and first running records', () => {
  const current = job('legacy', 10, { updated_at: 35 });
  const rows = [event(1, current.id, 15, 'step', 'running'), event(2, current.id, 30, 'terminal', 'done')];
  assert.deepEqual(runTiming(current, rows), { start: 10, end: 30 });
  assert.deepEqual(runTiming({ ...current, started_at: null }, rows), { start: 15, end: 30 });
  assert.deepEqual(runTiming(current, []), { start: 10, end: 35 });
  assert.deepEqual(runTiming({ ...current, started_at: null }, []), { start: undefined, end: 35 });
});

test('persisted media Skill records stay inside the existing collapsed timeline without invented execution events', () => {
  const records = [mediaSkill('asset_prompts', { prompt: 'PRIVATE_PROMPT', path: '/private/template', reasoning: 'PRIVATE_REASONING' }), mediaSkill('video_prompts', { version: '1.2.3' })];
  const current = job('historical', 10, { kind: 'generate', media_skill_usage: records });
  const collapsed = timelineMarkup({ job: current, events: [] });
  assert.ok(collapsed.includes('2 个提示词模板'));
  assert.ok(collapsed.includes('aria-expanded="false"'));
  assert.ok(!collapsed.includes('Ads Video Asset Prompts'), 'the ordinary conversation remains concise');
  assert.ok(!collapsed.includes('条执行记录'), 'stored compiler usage must not be counted as newly executed events');
  const html = renderToStaticMarkup(React.createElement(MediaSkillRecords, { records }));
  for (const text of ['Ads Video Asset Prompts', 'Ads Video Video Prompts', '1.0.0', '1.2.3', '图片提示词编译', '视频提示词编译']) assert.ok(html.includes(text));
  assert.doesNotMatch(html, /PRIVATE_PROMPT|\/private\/template|PRIVATE_REASONING|saved-asset_prompts-hash|saved-video_prompts-hash|<time|执行中|调用模型/);
  assert.equal(renderToStaticMarkup(React.createElement(MediaSkillRecords, { records: [] })), '');
  assert.ok(!timelineMarkup({ job: job('without-trace', 10), events: [] }).includes('Skill'), 'no trace must not infer a Skill from job kind');
});

test('attempt timing sorts sequence numbers and excludes unrelated job and project records', () => {
  const current = job('mine', 10, { updated_at: 80 });
  const rows = [event(5, current.id, 80, 'terminal', 'done'),
    event(3, current.id, 60, 'status', 'running', '开始执行任务'),
    event(1, current.id, 10, 'status', 'running', '开始执行任务'),
    event(6, 'other', 90, 'status', 'running', '开始执行任务'),
    { ...event(7, current.id, 95, 'status', 'running', '开始执行任务'), project_id: 'other-project' }];
  assert.deepEqual(runTiming(current, rows), { start: 60, end: 80 });
});

function creativeFixture() {
  return detail({
    jobs: [job('generation', 10, { kind: 'generate', revision: 2, started_at: 50.03, updated_at: 80.02 })],
    gates: [gate('creative', 'generation', 20, { kind: 'creative', answered_at: 50, note: '', selected_option_id: 'current' })],
    messages: [message('design-ready', 'assistant', 20.01, 'generation'), message('selected-opening', 'user', 50.01, 'generation'), message('video-ready', 'assistant', 80.01, 'generation')],
    events: [
      event(1, 'generation', 10, 'status', 'running'),
      event(2, 'generation', 19, 'artifact', 'done'),
      event(3, 'generation', 20.01, 'message', 'done'),
      event(4, 'generation', 20.02, 'gate', 'waiting'),
      event(5, 'generation', 50.01, 'message', 'done'),
      event(6, 'generation', 50.015, 'gate', 'done'),
      event(7, 'generation', 50.02, 'status', 'waiting'),
      event(8, 'generation', 50.03, 'status', 'running'),
      event(9, 'generation', 80.01, 'message', 'done'),
      event(10, 'generation', 80.02, 'terminal', 'done'),
    ],
  });
}

test('empty project yields an empty conversation', () => {
  assert.deepEqual(conversationItems(null), []);
  assert.deepEqual(conversationItems(detail()), []);
});

test('saved script and shots remain beside the planning response during image review, video generation and completion', () => {
  const plan = { script: '下午茶，停一下。', segments: [{ id: 's1', shots: [{ start: 0, end: 3, description: '办公室桌面上的蛋糕' }] }] };
  const snapshot = detail({
    project: { id: projectId, revision: 2, plan, updated_at: 90 },
    jobs: [job('plan', 1), job('generation', 30, { kind: 'generate', revision: 2 })],
    messages: [message('plan-ready', 'assistant', 11, 'plan'), message('design-ready', 'assistant', 50, 'generation'), message('video-ready', 'assistant', 90, 'generation')],
  });
  for (const [status, generationStatus, gateStatus] of [['awaiting_creative', 'awaiting_input', 'pending'], ['generating', 'running', 'answered'], ['ready', 'ready', 'answered']]) {
    snapshot.project.status = status;
    snapshot.jobs[1].status = generationStatus;
    snapshot.gates = [gate('images', 'generation', 50, { kind: 'creative', status: gateStatus })];
    const original = structuredClone(snapshot), items = conversationItems(snapshot);
    assert.equal(items.filter(item => item.type === 'plan').length, 1);
    assert.equal(items[items.findIndex(item => item.id === 'plan-ready') + 1].type, 'plan');
    assert.deepEqual(items.find(item => item.type === 'plan').plan, plan);
    assert.ok(items.findIndex(item => item.type === 'plan') < items.findIndex(item => item.id === 'design-ready'));
    assert.deepEqual(snapshot, original);
  }
});

test('a new clarification, reply, or unfinished plan cannot relocate the previous saved plan', () => {
  const snapshot = detail({
    project: { id: projectId, revision: 3, plan: { script: '已保存台词', segments: [] }, updated_at: 90 },
    jobs: [job('saved-plan', 1), job('reply', 20, { revision: 3, phase: 'reply_ready' }), job('clarification', 40, { revision: 3, phase: 'clarification_answered' }), job('new-plan', 60, { revision: 3, status: 'running' })],
    messages: [message('saved-response', 'assistant', 11, 'saved-plan'), message('reply-response', 'assistant', 30, 'reply'), message('questions', 'assistant', 50, 'clarification')],
  });
  assert.equal(conversationItems(snapshot)[2].type, 'plan');
  snapshot.jobs[3] = { ...snapshot.jobs[3], revision: 4, status: 'ready', updated_at: 70 };
  snapshot.project.revision = 4;
  snapshot.messages.push(message('new-response', 'assistant', 70, 'new-plan'));
  const items = conversationItems(snapshot);
  assert.equal(items.filter(item => item.type === 'plan').length, 1);
  assert.equal(items[items.findIndex(item => item.id === 'new-response') + 1].type, 'plan');
});

test('imported or manually saved plans remain visible even without a planning job', () => {
  const snapshot = detail({ project: { id: projectId, revision: 1, updated_at: 10, plan: { script: '手动保存台词', segments: [] } }, messages: [message('brief', 'user', 1)] });
  assert.deepEqual(conversationItems(snapshot).map(item => item.type), ['message', 'plan']);
});

test('ordinary clarification preserves question, answer, new run and response order', () => {
  const snapshot = detail({
    jobs: [job('clarification', 2, { phase: 'clarification_answered', updated_at: 50 }), job('plan', 50.02, { updated_at: 60.02 })],
    gates: [gate('questions', 'clarification', 10, { answered_at: 50 })],
    messages: [message('brief', 'user', 1), message('question-intro', 'assistant', 10.01, 'clarification'), message('answers', 'user', 50.01, 'plan'), message('plan-ready', 'assistant', 60.01, 'plan')],
    events: [event(1, 'clarification', 2, 'status', 'running'), event(2, 'clarification', 10.02, 'gate', 'waiting'), event(3, 'clarification', 50, 'gate', 'done'), event(4, 'plan', 50.02, 'status', 'running'), event(5, 'plan', 60.02, 'terminal', 'done')],
  });
  const items = conversationItems(snapshot);
  assert.deepEqual(order(items), ['message:brief', 'run:clarification', 'message:question-intro', 'gate:questions', 'message:answers', 'run:plan', 'message:plan-ready']);
  assert.ok(timelineMarkup(items[1]).includes('>8s</time>'), 'clarification clock ends at waiting, excluding merchant answer time');
});

test('same-job creative resume is split around the actual user confirmation', () => {
  const snapshot = creativeFixture();
  snapshot.jobs[0].media_skill_usage = [mediaSkill('asset_prompts'), mediaSkill('video_prompts')];
  const original = structuredClone(snapshot);
  const items = conversationItems(snapshot);
  assert.deepEqual(order(items), ['run:generation:preparation', 'message:design-ready', 'gate:creative', 'message:selected-opening', 'run:generation:video', 'message:video-ready']);
  const [before, after] = items.filter(item => item.type === 'run');
  assert.equal(before.job.id, after.job.id, 'durable billing identity must remain unchanged');
  assert.notEqual(before.id, after.id, 'UI instances need distinct React keys');
  assert.equal(before.job.started_at, 10);
  assert.equal(before.job.updated_at, 20.02);
  assert.equal(before.job.status, 'ready');
  assert.equal(before.job.error, null);
  assert.deepEqual(before.events.map(item => item.seq), [1, 2, 4, 6]);
  assert.deepEqual(after.events.map(item => item.seq), [7, 8, 10]);
  assert.equal(after.job.started_at, 50.03);
  assert.deepEqual(before.job.media_skill_usage.map(record => record.stage), ['asset_prompts']);
  assert.deepEqual(after.job.media_skill_usage.map(record => record.stage), ['video_prompts']);
  assert.ok(timelineMarkup(before).includes('>10s</time>'));
  assert.ok(timelineMarkup(after).includes('>29s</time>'));
  assert.deepEqual(snapshot, original, 'rendering must not mutate the server snapshot');
  assert.deepEqual(conversationItems(snapshot), items, 'repeated snapshots must not accumulate records');
});

test('attempt timers respect the sliced preparation and video histories of one durable job', () => {
  const snapshot = creativeFixture();
  for (const row of snapshot.events) {
    if (row.kind === 'status' && row.state === 'running') row.label = '开始执行任务';
  }
  const [before, after] = conversationItems(snapshot).filter(item => item.type === 'run');
  assert.deepEqual(runTiming(before.job, before.events), { start: 10, end: 20.02 });
  assert.deepEqual(runTiming(after.job, after.events), { start: 50.03, end: 80.02 });
  assert.ok(timelineMarkup(before).includes('>10s</time>'));
  assert.ok(timelineMarkup(after).includes('>29s</time>'));
});

test('replayed SSE records neither duplicate messages/gates nor visible event counts', () => {
  const snapshot = creativeFixture();
  snapshot.events = [...snapshot.events, ...snapshot.events];
  const items = conversationItems(snapshot);
  assert.equal(items.filter(item => item.type === 'message').length, 3);
  assert.equal(items.filter(item => item.type === 'gate').length, 1);
  assert.equal(new Set(items.map(item => item.id)).size, items.length);
  const [before, after] = items.filter(item => item.type === 'run');
  assert.ok(timelineMarkup(before).includes('4 条执行记录'));
  assert.ok(timelineMarkup(after).includes('3 条执行记录'));
  const html = renderToStaticMarkup(React.createElement(React.Fragment, null,
    React.createElement(RunTimeline, { key: before.id, job: before.job, events: before.events }),
    React.createElement(RunTimeline, { key: after.id, job: after.job, events: after.events })));
  const controls = [...html.matchAll(/aria-controls="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(controls).size, 2, 'same-job timeline instances need distinct DOM controls');
});

test('pending creative gate has one waiting run and no running clock', () => {
  const snapshot = creativeFixture();
  snapshot.jobs[0] = { ...snapshot.jobs[0], status: 'awaiting_input', started_at: 10, updated_at: 20.02 };
  snapshot.gates[0] = { ...snapshot.gates[0], status: 'pending', answered_at: null };
  snapshot.messages = snapshot.messages.slice(0, 1);
  snapshot.events = snapshot.events.filter(item => item.seq <= 4);
  const items = conversationItems(snapshot);
  assert.deepEqual(order(items), ['run:generation', 'message:design-ready', 'gate:creative']);
  const html = timelineMarkup(items[0]);
  assert.ok(!html.includes('<time'));
  assert.ok(!html.includes('run-event-spinner'));
});

test('creative revision note opens a separate planner run, without a fictitious video run', () => {
  const snapshot = creativeFixture();
  snapshot.jobs[0] = { ...snapshot.jobs[0], status: 'canceled', started_at: 10, updated_at: 50.02 };
  snapshot.jobs.push(job('revision', 50.03, { updated_at: 60 }));
  snapshot.gates[0].note = '开头节奏慢一点';
  snapshot.messages = [...snapshot.messages.slice(0, 2), message('revised-plan', 'assistant', 60, 'revision')];
  snapshot.events = [...snapshot.events.filter(item => item.seq <= 6), event(7, 'revision', 50.03, 'status', 'running'), event(8, 'revision', 60.01, 'terminal', 'done')];
  const items = conversationItems(snapshot);
  assert.deepEqual(order(items), ['run:generation', 'message:design-ready', 'gate:creative', 'message:selected-opening', 'run:revision', 'message:revised-plan']);
  assert.ok(!items.some(item => item.id.endsWith(':video')));
});

const draftTools = loadSource('gateDraft.ts');
const ConversationGate = loadSource('ConversationGate.tsx').default;
function clarificationFixture() {
  return gate('draft-questions', 'planning', 20, {
    status: 'pending', questions: [
      { id: 'audience', prompt: '广告给谁看？', allow_custom: true, allow_skip: true, options: [{ id: 'office', label: '上班族' }, { id: 'custom', label: '我来补充' }] },
      { id: 'style', prompt: '喜欢什么风格？', allow_custom: true, allow_skip: true, options: [{ id: 'clean', label: '简洁明亮' }, { id: 'write', label: '自行填写' }] },
      { id: 'selling', prompt: '突出什么卖点？', allow_custom: true, allow_skip: true, options: [{ id: 'portable', label: '便携' }, { id: 'fill', label: '自填' }] },
    ],
  });
}
test('composer custom answer preserves earlier selections and advances to unified review', () => {
  const fixture = clarificationFixture();
  const initial = draftTools.initialDraft(fixture);
  const first = draftTools.answerCurrentQuestion(fixture, initial, { question_id: 'audience', option_id: 'office' });
  const second = draftTools.answerCurrentWithText(fixture, first, '  温暖自然，真实家居  ');
  const final = draftTools.answerCurrentQuestion(fixture, second, { question_id: 'selling', skipped: true });
  assert.equal(final.step, 3);
  assert.deepEqual(final.answers, {
    audience: { question_id: 'audience', option_id: 'office' },
    style: { question_id: 'style', text: '温暖自然，真实家居' },
    selling: { question_id: 'selling', skipped: true },
  });
  assert.deepEqual(initial.answers, {}, 'answering must not mutate a previous snapshot');
  assert.throws(() => draftTools.answerCurrentWithText(fixture, final, '不能当成新一轮覆盖'), /选择要修改的问题/);
  const restored = draftTools.restoreDraft(fixture, JSON.stringify(final));
  assert.deepEqual(restored, final, 'all unsent answers and review state survive a reload');
  const edited = draftTools.answerCurrentWithText(fixture, { ...restored, step: 0 }, '常出差的人');
  assert.equal(edited.answers.audience.text, '常出差的人');
  assert.deepEqual(edited.answers.style, final.answers.style);
  assert.deepEqual(edited.answers.selling, final.answers.selling);
});
test('fake free-text options are absent and cannot be accepted as actual selections', () => {
  const fixture = clarificationFixture();
  assert.deepEqual(fixture.questions.map(question => draftTools.questionOptions(question).map(option => option.id)), [['office'], ['clean'], ['portable']]);
  assert.equal(draftTools.validAnswer(fixture.questions[0], { question_id: 'audience', option_id: 'custom' }), false);
  assert.equal(draftTools.isFreeInputOption('我提供商品实拍图'), false, 'a genuine choice is not the duplicate free-input entry');
  const html = renderToStaticMarkup(React.createElement(ConversationGate, { gate: fixture, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {} }));
  assert.ok(!html.includes('<textarea'));
  assert.ok(!html.includes('我来补充'));
  assert.ok(!html.includes('填写自己的答案'));
  assert.ok(html.includes('下方输入答案'));
});
test('invalid restored answers and overlong custom input do not reach final review', () => {
  const fixture = clarificationFixture();
  const saved = { ...draftTools.initialDraft(fixture), step: 3, answers: { audience: { question_id: 'audience', option_id: 'custom' } } };
  assert.equal(draftTools.restoreDraft(fixture, JSON.stringify(saved)).step, 0);
  assert.throws(() => draftTools.answerCurrentWithText(fixture, draftTools.initialDraft(fixture), '字'.repeat(501)), /500字/);
  assert.throws(() => draftTools.answerCurrentWithText(fixture, draftTools.initialDraft(fixture), '  '), /500字/);
});
test('image check defaults to current opening and only the shared composer can add a note', () => {
  const fixture = gate('images', 'generation', 20, { status: 'pending', kind: 'creative', options: [
    { id: 'current', label: '当前方案', description: '保留当前开场', segment: {} },
    { id: 'detail', label: '细节开场', description: '先展示商品细节', segment: {} },
  ] });
  assert.equal(draftTools.initialDraft(fixture).selectedOptionId, 'current');
  const props = { gate: fixture, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {} };
  const html = renderToStaticMarkup(React.createElement(ConversationGate, props));
  assert.ok(html.includes('视频将使用这些参考图片'));
  assert.ok(html.includes('检查图片是否符合已确认的方案'));
  assert.ok(html.includes('图片没问题，生成视频'));
  assert.ok(html.includes('开场：当前方案'));
  assert.ok(!html.includes('<textarea'));
  assert.ok(!html.match(/class="gate-submit-button" disabled/), 'current option is immediately confirmable');
  const withDraft = renderToStaticMarkup(React.createElement(ConversationGate, { ...props, composerText: '背景请更简洁' }));
  assert.ok(withDraft.match(/class="gate-submit-button" disabled/), 'an unsent note must not be silently ignored by video confirmation');
});

const planEditing = loadSource('planEditing.ts');
test('sound labels follow the saved plan or render decision without changing historical labels', () => {
  const currentPlan = { audio_strategy: 'native', production: { speech_mode: 'none' } };
  const previousRender = { manifest: { audio_strategy: 'native', production: { speech_mode: 'character_dialogue' } } };
  const legacyRender = { manifest: { audio_strategy: 'native' } };
  const label = owner => planEditing.soundLabel(owner.audio_strategy, owner.production);
  assert.equal(label(currentPlan), '无台词');
  assert.equal(label(previousRender.manifest), '角色对白');
  assert.equal(label(legacyRender.manifest), '中文原声');
  assert.equal(label({ audio_strategy: 'native', production: { speech_mode: 'voiceover' } }), '画外旁白');
  assert.equal(label({ audio_strategy: 'voiceover' }), '中文画外音');
  assert.equal(planEditing.soundLabel('native', null), '中文原声');
  assert.equal(planEditing.soundLabel('native', 'none'), '中文原声', 'only a saved production object changes a legacy label');
});
test('native script editing keeps per-segment speech and full script consistent for short tails', () => {
  const plan = { audio_strategy: 'native', script: '已有前段台词。', segments: [{ id: 'one', duration_seconds: 10, narration: '已有前段台词。' }, { id: 'tail', duration_seconds: 1, narration: '' }] };
  const updated = planEditing.updateNarration(plan, 0, '保留商品事实，突出真实细节。');
  assert.equal(updated.script, '保留商品事实，突出真实细节。');
  assert.equal(updated.segments[1].narration, '', 'one-second tail can end without speech');
  assert.equal(planEditing.narrationLimit(1), 2);
  assert.equal(planEditing.planScriptLimit(updated), 31);
  assert.ok(planEditing.validEditedPlan(updated));
  assert.ok(!planEditing.validEditedPlan({ ...updated, script: '不一致的整条文案' }));
  assert.ok(!planEditing.validEditedPlan(planEditing.updateNarration(updated, 1, '尾段太长')));
  assert.equal(plan.segments[0].narration, '已有前段台词。', 'editing must leave saved snapshot intact');
  assert.equal(planEditing.soundLabel('native'), '中文原声');
  assert.equal(planEditing.soundLabel('voiceover'), '中文画外音');
});

const { default: PlanEditor, PlanShotDetails } = loadSource('PlanEditor.tsx');
function shotPlanFixture() {
  return {
    audio_strategy: 'native', script: '柔和的光。映出真实细节。轻松收纳。',
    segments: [
      { id: 'one', duration_seconds: 10, purpose: '展示外观', narration: '柔和的光。映出真实细节。', shots: [
        { start: 0, end: 3, description: '商品在柔和灯光中出现', narration: '柔和的光。' },
        { start: 3, end: 10, description: '镜头靠近商品细节', narration: '映出真实细节。' },
      ] },
      { id: 'tail', duration_seconds: 7, purpose: '日常收束', narration: '轻松收纳。', shots: [
        { start: 0, end: 2, description: '商品收进抽屉', narration: '轻松收纳。' },
        { start: 2, end: 7, description: '镜头自然停留', narration: '' },
      ] },
    ],
  };
}
test('editing one shot synchronizes its segment and full script without changing timing or other shots', () => {
  const plan = shotPlanFixture(), previous = structuredClone(plan);
  assert.ok(planEditing.hasBoundShotNarration(plan));
  assert.ok(planEditing.validEditedPlan(plan));
  const edited = planEditing.updateShotNarration(plan, 0, 1, '每个细节都看得见。');
  assert.equal(edited.segments[0].shots[1].narration, '每个细节都看得见。');
  assert.equal(edited.segments[0].narration, '柔和的光。每个细节都看得见。');
  assert.equal(edited.script, '柔和的光。每个细节都看得见。轻松收纳。');
  assert.deepEqual(edited.segments[1], plan.segments[1]);
  assert.deepEqual(edited.segments[0].shots[0], plan.segments[0].shots[0]);
  assert.deepEqual(edited.segments[0].shots[1], { ...plan.segments[0].shots[1], narration: '每个细节都看得见。' });
  assert.deepEqual(plan, previous);
  assert.ok(planEditing.validEditedPlan(edited));
  assert.throws(() => planEditing.updateNarration(plan, 0, '不能直接覆盖镜头台词'), /按镜头/);
});
test('per-shot timing rejects speech that fits the segment but not the specific shot', () => {
  const plan = shotPlanFixture();
  assert.equal(planEditing.shotNarrationLimit({ start: 0, end: 3 }), 8);
  assert.equal(planEditing.shotNarrationLimit({ start: 0.1, end: 2.1 }), 5);
  assert.equal(planEditing.shotNarrationLimit({ start: 0, end: 0.2 }), 0);
  const tooLong = planEditing.updateShotNarration(plan, 1, 0, '字'.repeat(6));
  assert.ok(tooLong.segments[1].narration.length < planEditing.narrationLimit(7));
  assert.equal(planEditing.validEditedPlan(tooLong), false);
  const inconsistent = structuredClone(plan);
  inconsistent.segments[0].shots[0].narration = '不同台词';
  assert.equal(planEditing.validEditedPlan(inconsistent), false);
});
test('clearing all shot speech is reversible while editing and cannot save an empty script', () => {
  let edited = shotPlanFixture();
  for (let segmentIndex = 0; segmentIndex < edited.segments.length; segmentIndex++) {
    for (let shotIndex = 0; shotIndex < edited.segments[segmentIndex].shots.length; shotIndex++) edited = planEditing.updateShotNarration(edited, segmentIndex, shotIndex, '');
  }
  assert.equal(edited.script, '');
  assert.ok(planEditing.hasBoundShotNarration(edited));
  assert.equal(planEditing.validEditedPlan(edited), false);
  edited = planEditing.updateShotNarration(edited, 0, 0, '保留真实细节。');
  assert.equal(edited.script, '保留真实细节。');
  assert.ok(planEditing.validEditedPlan(edited));
});
test('legacy native narration stays editable by segment without inventing or clearing shot assignments', () => {
  for (const useDefaultEmpty of [false, true]) {
    const plan = shotPlanFixture();
    for (const segment of plan.segments) for (const shot of segment.shots) {
      if (useDefaultEmpty) shot.narration = '';
      else delete shot.narration;
    }
    const saved = structuredClone(plan);
    assert.equal(planEditing.hasBoundShotNarration(plan), false);
    assert.ok(planEditing.validEditedPlan(plan));
    assert.throws(() => planEditing.updateShotNarration(plan, 0, 0, '新台词'), /更新方案/);
    const html = renderToStaticMarkup(React.createElement(PlanEditor, { plan, onChange: () => {} }));
    assert.equal((html.match(/<textarea/g) || []).length, 2);
    assert.ok(html.includes('柔和的光。映出真实细节。'));
    assert.ok(html.includes('先按段修改'));
    const edited = planEditing.updateNarration(plan, 0, '新的整段台词。');
    assert.deepEqual(edited.segments[0].shots, saved.segments[0].shots);
    assert.deepEqual(edited.segments[1], saved.segments[1]);
    assert.ok(planEditing.validEditedPlan(edited));
    assert.deepEqual(plan, saved);
  }
});
test('native editor and plan card pair timeline, visible action and speech on each shot row', () => {
  const plan = shotPlanFixture();
  const html = renderToStaticMarkup(React.createElement(PlanEditor, { plan, onChange: () => {} }));
  assert.equal((html.match(/<textarea/g) || []).length, 4);
  assert.ok(html.includes('maxLength="8"'));
  assert.ok(html.includes('maxLength="5"'));
  assert.ok(html.includes('10–12 秒'), 'second segment starts at its cumulative video time');
  assert.ok(html.includes('商品收进抽屉'));
  assert.ok(html.includes('轻松收纳。'));
  const card = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan }));
  assert.ok(card.includes('<details class="shot-details">'), 'storyboard starts collapsed with its disclosure entry visible');
  assert.ok(card.includes('分镜脚本 · 时间、画面与台词'));
  assert.equal((card.match(/class="plan-shot-detail"/g) || []).length, 4);
  assert.ok(card.includes('12–17 秒'));
  assert.ok(card.includes('画面</span>镜头自然停留'));
  assert.ok(card.includes('台词</span>无台词'));
});

function speechSpanPlanFixture() {
  const plan = shotPlanFixture();
  plan.segments.forEach((segment, segmentIndex) => {
    segment.shots.forEach((shot, shotIndex) => {
      shot.id = `s${segmentIndex + 1}-shot${shotIndex + 1}`;
      shot.narration = '';
      shot.beat_id = `internal-beat${segmentIndex + 1}`;
    });
    segment.speech_spans = [{
      id: `speech${segmentIndex + 1}`, beat_id: `internal-beat${segmentIndex + 1}`, text: segment.narration,
      start: segmentIndex === 0 ? 0.5 : 0, end: segmentIndex === 0 ? 9 : 5,
      shot_ids: segment.shots.map(shot => shot.id),
      anchors: [{ text: segment.narration, start: 1, end: 2, shot_ids: [segment.shots[0].id], visual_evidence: 'internal-visual-evidence' }],
    }];
  });
  return plan;
}
test('speech spanning shots appears once with its own time and shot numbers inside the collapsed storyboard', () => {
  const plan = speechSpanPlanFixture(), before = structuredClone(plan);
  const html = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan, onEditNarration: () => {} }));
  assert.ok(html.includes('<details class="shot-details">'));
  assert.equal((html.match(/class="plan-shot-detail"/g) || []).length, 4);
  assert.equal((html.match(/class="plan-shot-detail plan-speech-detail"/g) || []).length, 2);
  assert.ok(html.includes('旁白 · 0.5–9 秒 · 镜头 1、2'));
  assert.ok(html.includes('旁白 · 10–15 秒 · 镜头 1、2'), 'speech uses cumulative video time like the shots');
  assert.equal(html.split(plan.segments[0].narration).length - 1, 1);
  assert.equal(html.split(plan.segments[1].narration).length - 1, 1);
  assert.ok(html.includes('镜头 2 · 12–17 秒'));
  assert.ok(html.includes('可连续覆盖多个镜头'));
  assert.ok(html.includes('修改台词'));
  for (const hidden of ['整条台词', '整段台词', '尚未按镜头分配', '无台词', 'internal-beat', 'internal-visual-evidence', 'anchors']) assert.ok(!html.includes(hidden), hidden);
  assert.deepEqual(plan, before);
});

test('character dialogue uses the actual speaker while multi-beat shots retain their existing schedule', () => {
  const plan = speechSpanPlanFixture();
  plan.production = { version: '1.0.0', product_constraints: [], characters: [{ id: 'worker', name: '白领', description: '办公穿着' }],
    speech_mode: 'character_dialogue', speaker_id: 'worker', voice_description: '自然中文说话' };
  plan.segments[0].speech_spans[0].speaker_id = 'worker';
  delete plan.segments[0].shots[0].beat_id;
  plan.segments[0].shots[0].beat_ids = ['opening', 'product'];
  const before = structuredClone(plan), html = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan }));
  assert.ok(html.includes('白领说话 · 0.5–9 秒 · 镜头 1、2'));
  assert.ok(html.includes('白领说话 · 10–15 秒 · 镜头 1、2'), 'a span may inherit the confirmed sole speaker');
  assert.ok(html.includes('实际声音与口型需看成片确认'));
  assert.ok(!html.includes('旁白'));
  assert.equal((html.match(/class="plan-shot-detail"/g) || []).length, 4);
  assert.deepEqual(plan, before);
});
test('span-based narration cannot enter legacy direct editing and retain stale visual anchors', () => {
  const plan = speechSpanPlanFixture(), before = structuredClone(plan);
  assert.equal(planEditing.hasSpeechSpans(plan), true);
  assert.equal(planEditing.hasBoundShotNarration(plan), false);
  assert.equal(planEditing.validEditedPlan(plan), false, 'new plans are revised through conversation, not saved by the legacy modal');
  assert.throws(() => planEditing.updateNarration(plan, 0, '改写的内容。'), /在对话中修改旁白/);
  assert.throws(() => planEditing.updateShotNarration(plan, 0, 0, '改写的内容。'), /在对话中修改旁白/);
  const html = renderToStaticMarkup(React.createElement(PlanEditor, { plan, onChange: () => assert.fail('read-only fallback must not edit') }));
  assert.ok(!html.includes('<textarea'));
  assert.ok(html.includes('请在对话中描述要修改的旁白'));
  assert.equal(html.split(plan.segments[0].narration).length - 1, 1);
  assert.ok(!html.includes('字以内'), 'a cross-shot sentence is not constrained to one shot in the UI');
  assert.deepEqual(plan, before, 'text, timings, and anchors must remain unchanged');
});
test('silent span lists retain their format while missing or null spans preserve legacy editing', () => {
  const plan = speechSpanPlanFixture();
  plan.segments[1].speech_spans = [];
  plan.segments[1].narration = '';
  plan.script = plan.segments[0].narration;
  const html = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan }));
  assert.ok(html.includes('这一段不安排旁白'));
  assert.ok(!html.includes('尚未按镜头分配'));
  plan.segments[0].speech_spans = [];
  assert.equal(planEditing.hasSpeechSpans(plan), true, 'silent new plans must not fall back to old editors');
  assert.throws(() => planEditing.updateNarration(plan, 0, '新增台词。'), /在对话中修改旁白/);
  const legacy = shotPlanFixture();
  legacy.segments[0].speech_spans = null;
  assert.equal(planEditing.hasSpeechSpans(legacy), false);
  assert.equal(planEditing.hasBoundShotNarration(legacy), true);
  assert.equal(planEditing.validEditedPlan(planEditing.updateShotNarration(legacy, 0, 0, '柔和光线。')), true);
});
test('an explicit no-speech plan shows pictures and time without a speech editing entrance', () => {
  const plan = speechSpanPlanFixture();
  plan.production = { speech_mode: 'none' };
  plan.script = '';
  plan.segments.forEach(segment => { segment.narration = ''; segment.speech_spans = []; });
  for (const spans of [[], null]) {
    plan.segments.forEach(segment => { segment.speech_spans = spans; });
    const html = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan, onEditNarration: () => assert.fail('no speech to edit') }));
    assert.match(html, /时间与画面/);
    assert.match(html, /这版不安排台词/);
    assert.equal((html.match(/class="plan-shot-detail"/g) || []).length, 4);
    assert.doesNotMatch(html, /修改台词|修改旁白|旁白按时段|尚未按镜头分配|<button/);
    const editor = renderToStaticMarkup(React.createElement(PlanEditor, { plan, onChange: () => assert.fail('no speech editor') }));
    assert.doesNotMatch(editor, /<textarea|修改的旁白|修改的角色台词/);
  }
});
test('mixed saved formats render each segment faithfully without repeating or inventing speech', () => {
  const plan = speechSpanPlanFixture();
  plan.segments[1] = shotPlanFixture().segments[1];
  const html = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan }));
  assert.ok(html.includes('旁白 · 0.5–9 秒 · 镜头 1、2'));
  assert.ok(html.includes('台词</span>轻松收纳。'));
  assert.ok(html.includes('台词</span>无台词'));
  assert.equal(html.split(plan.segments[0].narration).length - 1, 1);
  assert.equal(planEditing.validEditedPlan(plan), false);
});

const { currentCharacterAssets, currentSceneAssets, designImageLabel, messageDesignAssets, planStateReferenceCount } = loadSource('designAssets.ts');
const MessageDesignImages = loadSource('MessageDesignImages.tsx').default;
function recomposedDesignFixture() {
  return detail({
    project: { id: projectId, revision: 4, context_version: 2, plan_context_version: 2, current_render_id: 'render4', plan: { segments: [{ reference_asset_ids: ['original'] }] } },
    assets: [
      { id: 'original', project_id: projectId, kind: 'product', revision: 0, metadata: {} },
      { id: 'used-scene', project_id: projectId, kind: 'scene', revision: 2, metadata: { generated: true } },
      { id: 'unrelated-scene', project_id: projectId, kind: 'scene', revision: 1, metadata: { generated: true } },
    ],
    jobs: [job('generation3', 30, { kind: 'generate', revision: 3, progress: { design_asset_ids: ['used-scene'] } }), job('recompose4', 40, { kind: 'recompose', revision: 4 })],
    renders: [
      { id: 'render4', asset_id: 'render-asset4', revision: 4, status: 'ready', created_at: 40, manifest: { base_render_id: 'render3' } },
      { id: 'render3', asset_id: 'render-asset3', revision: 3, status: 'ready', created_at: 30, manifest: {} },
    ],
  });
}
test('current recomposition follows the source render to retain its actual generated scene', () => {
  const snapshot = recomposedDesignFixture(), before = structuredClone(snapshot);
  assert.deepEqual(currentSceneAssets(snapshot).map(asset => asset.id), ['used-scene']);
  assert.deepEqual(snapshot, before, 'resolving lineage must not mutate the project snapshot');
  snapshot.project.current_render_id = 'render-asset4';
  assert.deepEqual(currentSceneAssets(snapshot).map(asset => asset.id), ['used-scene'], 'legacy asset-id render pointers remain supported');
  snapshot.project.revision = 5;
  snapshot.project.current_render_id = 'render5';
  snapshot.renders.unshift({ id: 'render5', asset_id: 'render-asset5', revision: 5, status: 'ready', created_at: 50, manifest: { base_render_id: 'render4' } });
  assert.deepEqual(currentSceneAssets(snapshot).map(asset => asset.id), ['used-scene'], 'multiple recompositions retain the generation reference');
});
test('a new plan revision never inherits a stale render scene through lineage', () => {
  const snapshot = recomposedDesignFixture();
  snapshot.project.revision = 5;
  assert.deepEqual(currentSceneAssets(snapshot), []);
  snapshot.project.revision = 4;
  snapshot.project.context_version = 3;
  assert.deepEqual(currentSceneAssets(snapshot), [], 'new unresolved requirements also invalidate old design state');
  snapshot.project.context_version = 2;
  snapshot.project.revision = 5;
  snapshot.project.plan.segments[0].reference_asset_ids.push('used-scene');
  assert.deepEqual(currentSceneAssets(snapshot).map(asset => asset.id), ['used-scene'], 'explicit scene references in the current plan are intentional reuse');
});
test('missing or cyclic render ancestry cannot borrow an unrelated historical scene', () => {
  const snapshot = recomposedDesignFixture();
  snapshot.renders[0].manifest.base_render_id = 'missing';
  assert.deepEqual(currentSceneAssets(snapshot), []);
  snapshot.renders[0].manifest.base_render_id = 'render4';
  assert.deepEqual(currentSceneAssets(snapshot), []);
  snapshot.renders[0].manifest.base_render_id = 'render3';
  snapshot.renders[1].manifest.base_render_id = 'render4';
  assert.deepEqual(currentSceneAssets(snapshot).map(asset => asset.id), ['used-scene']);
  snapshot.renders[0].status = 'failed';
  assert.deepEqual(currentSceneAssets(snapshot), []);
});
test('the shot schedule is explicitly labeled as a plan rather than measured output timing', () => {
  const html = renderToStaticMarkup(React.createElement(PlanShotDetails, { plan: shotPlanFixture() }));
  assert.ok(html.includes('分镜脚本 · 时间、画面与台词'));
  assert.ok(html.includes('实际画面与说话时点需看成片确认'));
});

const { downloadUrl, mediaUrl } = loadSource('api.ts');
test('downloads use backing asset attachment URLs while preview URLs stay unchanged', () => {
  const render = { id: 'render-version4', asset_id: 'asset-final-video', url: '/media/asset-final-video' };
  const segment = { id: 'asset-segment', url: '/media/asset-segment' };
  const image = { id: 'asset-scene', url: '/media/asset-scene' };
  assert.equal(downloadUrl(render.asset_id), '/media/asset-final-video?download=true');
  assert.ok(!downloadUrl(render.asset_id).includes(render.id), 'render identity must not be used as the media asset identity');
  assert.equal(downloadUrl(segment.id), '/media/asset-segment?download=true');
  assert.equal(downloadUrl(image.id), '/media/asset-scene?download=true');
  assert.equal(mediaUrl(segment), segment.url);
  assert.equal(mediaUrl(image), image.url);
  assert.equal(render.url, '/media/asset-final-video', 'download resolution must not modify the playback URL');
});

function messageDesignFixture() {
  const snapshot = creativeFixture();
  snapshot.messages[0].content = snapshot.gates[0].message;
  snapshot.gates[0].options = [{ id: 'current', segment: { reference_asset_ids: ['original', 'scene'] } }];
  snapshot.jobs[0].progress = { design_asset_ids: ['scene'] };
  snapshot.assets = [
    { id: 'scene', project_id: projectId, kind: 'scene', name: '场景参考图', url: '/media/scene', metadata: { generated: true, source_asset_ids: ['original'] } },
    { id: 'original', project_id: projectId, kind: 'product', name: '商品.jpg', url: '/media/original', metadata: {} },
    { id: 'later-upload', project_id: projectId, kind: 'product', metadata: {} },
    { id: 'other-scene', project_id: projectId, kind: 'scene', metadata: { generated: true } },
  ];
  return snapshot;
}
test('chat images remain tied to the saved design message after confirmation and new revisions', () => {
  const snapshot = messageDesignFixture();
  snapshot.project = { id: projectId, revision: 9, context_version: 8, plan_context_version: 7, plan: { segments: [{ reference_asset_ids: ['later-upload', 'other-scene'] }] } };
  const before = structuredClone(snapshot);
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[0]).map(asset => asset.id), ['original', 'scene']);
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[1]), []);
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[2]), [], 'video result must not repeat the design images');
  assert.deepEqual(snapshot, before);
  snapshot.gates[0].status = 'superseded';
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[0]).map(asset => asset.id), ['original', 'scene']);
});
test('chat image references never borrow unrelated uploads or assets from another project', () => {
  const snapshot = messageDesignFixture();
  snapshot.gates[0].options = [];
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[0]).map(asset => asset.id), ['original', 'scene'], 'saved scene provenance retains its product');
  snapshot.assets[1].project_id = 'other-project';
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[0]).map(asset => asset.id), ['scene']);
  snapshot.jobs[0].progress.design_asset_ids = {};
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[0]), []);
});

test('character reference sheets stay with their saved image review and show one tile per role', () => {
  const snapshot = messageDesignFixture();
  const character = (id, characterId, name, revision = 1) => ({ id, project_id: projectId, kind: 'character', name, revision, url: `/media/${id}`,
    metadata: { generated: true, character_id: characterId, character_name: name, view_layout: 'four_view' } });
  snapshot.assets.push(character('actor-a-old', 'worker', '上班族'), character('actor-a', 'worker', '上班族', 2), character('actor-b', 'coworker', '同事'),
    character('unrelated-actor', 'other', '另一方案人物'), { ...character('other-project-actor', 'foreign', '其他项目人物'), project_id: 'other-project' });
  snapshot.jobs[0].progress.design_asset_ids.push('actor-a-old', 'actor-a', 'actor-b', 'other-project-actor');
  const before = structuredClone(snapshot);
  const assets = messageDesignAssets(snapshot, snapshot.messages[0]);
  assert.deepEqual(assets.map(asset => asset.id), ['original', 'scene', 'actor-a', 'actor-b']);
  const html = renderToStaticMarkup(React.createElement(MessageDesignImages, { assets, onPreview: () => {} }));
  assert.equal((html.match(/<figure/g) ?? []).length, 4, 'one image for each role, not separate angle tiles');
  assert.ok(html.includes('商品、场景与角色图片'));
  assert.ok(html.includes('角色四视图 · 上班族'));
  assert.ok(html.includes('角色四视图 · 同事'));
  assert.ok(!html.includes('另一方案人物'));
  assert.ok(!html.includes('其他项目人物'));
  assert.deepEqual(snapshot, before);
  snapshot.gates[0].status = 'superseded';
  snapshot.project.revision = 9;
  assert.deepEqual(messageDesignAssets(snapshot, snapshot.messages[0]).map(asset => asset.id), ['original', 'scene', 'actor-a', 'actor-b']);
});

test('state stills stay on the image gate and are captioned by phase', () => {
  const snapshot = messageDesignFixture();
  const still = (id, phase, label) => ({
    id, project_id: projectId, kind: 'start_frame', name: label, revision: 1, url: `/media/${id}`,
    metadata: { generated: true, state_reference: true, phase, state_label: label, sheet_layout: 'horizontal_16x9' },
  });
  snapshot.assets.push(still('wet', 'start', '湿发'), still('dry', 'result', '干发'),
    { id: 'plain-frame', project_id: projectId, kind: 'start_frame', name: '普通首帧', metadata: { generated: true } });
  snapshot.jobs[0].progress.design_asset_ids.push('wet', 'dry', 'plain-frame');
  const assets = messageDesignAssets(snapshot, snapshot.messages[0]);
  assert.deepEqual(assets.map(asset => asset.id), ['original', 'scene', 'wet', 'dry']);
  assert.equal(designImageLabel(assets[2]), '状态四视图 · 起始 · 湿发');
  assert.equal(designImageLabel(assets[3]), '状态四视图 · 结果 · 干发');
  assert.equal(designImageLabel({
    id: 'cup', project_id: projectId, kind: 'start_frame', name: '空杯', revision: 1, url: '/media/cup',
    metadata: { generated: true, state_reference: true, phase: 'result', state_label: '空了' },
  }), '结果状态 · 空了');
  const html = renderToStaticMarkup(React.createElement(MessageDesignImages, { assets, onPreview: () => {} }));
  assert.ok(html.includes('商品、场景与状态四视图'));
  assert.ok(html.includes('状态四视图 · 起始 · 湿发'));
  assert.ok(html.includes('状态四视图 · 结果 · 干发'));
  assert.ok(!html.includes('普通首帧'));
  assert.equal(planStateReferenceCount({ segments: [{ shots: [
    { description: '她从门口走到台前。' },
    { continuity: { start_state: '头发是湿的', end_state: '头发已经干' } },
  ] }] }), 1);
  assert.equal(planStateReferenceCount({ segments: [{ shots: [
    { id: 's3', description: '她吹着头发。', continuity: { start_state: '头发是湿的', end_state: '被吹开一半', allowed_changes: '发丝由湿到被吹开一半' } },
    { id: 's6', description: '她关掉吹风机，头发已经顺了。', continuity: { start_state: '头发半干', end_state: '头发顺开', allowed_changes: '发丝由半干到顺开' } },
  ] }] }), 2);
  assert.equal(planStateReferenceCount({ segments: [{ shots: [
    { id: 'sh4', continuity: { start_state: '头发仍是湿的', end_state: '头发仍是湿的' } },
    { id: 'sh5', continuity: { start_state: '头发仍是湿的', end_state: '头发仍是湿的' } },
    { id: 'sh6', continuity: { start_state: '省略继续吹干的过程后，头发已经吹干', end_state: '头发吹干且顺' } },
  ] }] }), 2);
});

test('current character sheets follow explicit render lineage and never stale or unrelated projects', () => {
  const snapshot = recomposedDesignFixture();
  snapshot.assets.push({ id: 'used-character', project_id: projectId, kind: 'character', revision: 2, metadata: { generated: true, character_id: 'worker' } },
    { id: 'other-character', project_id: projectId, kind: 'character', revision: 1, metadata: { generated: true, character_id: 'other' } },
    { id: 'foreign-character', project_id: 'other-project', kind: 'character', revision: 4, metadata: { generated: true, character_id: 'foreign' } });
  snapshot.jobs[0].progress.design_asset_ids.push('used-character');
  assert.deepEqual(currentCharacterAssets(snapshot).map(asset => asset.id), ['used-character']);
  snapshot.project.context_version = 3;
  assert.deepEqual(currentCharacterAssets(snapshot), []);
  snapshot.project.context_version = 2;
  snapshot.project.revision = 5;
  assert.deepEqual(currentCharacterAssets(snapshot), []);
  snapshot.project.revision = 4;
  snapshot.project.plan.production = { characters: [] };
  assert.deepEqual(currentCharacterAssets(snapshot), [], 'a new explicit no-character decision cannot show previous characters');
});
