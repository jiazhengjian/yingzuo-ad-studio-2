// Local-only state/render tests; no fetches, provider calls or production data.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const frontend = join(dirname(fileURLToPath(import.meta.url)), '..');
const requireFrontend = createRequire(join(frontend, 'package.json'));
const { buildSync } = requireFrontend('esbuild');
const React = requireFrontend('react');
const { renderToStaticMarkup } = requireFrontend('react-dom/server');
function loadSource(name) {
  const { outputFiles } = buildSync({ entryPoints: [join(frontend, 'src', name)], absWorkingDir: frontend,
    bundle: true, platform: 'node', format: 'cjs', packages: 'external', jsx: 'automatic', loader: { '.css': 'empty' }, write: false });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputFiles[0].text)(requireFrontend, module, module.exports);
  return module.exports;
}
const { recoveryForJob, retryRequest } = loadSource('jobRecovery.ts');
const RecoveryCard = loadSource('JobRecoveryCard.tsx').default;
const Timeline = loadSource('RunTimeline.tsx').default;
const fixture = (overrides = {}) => ({ id: 'job-test', project_id: 'project-test', revision: 5, kind: 'generate', status: 'failed',
  phase: 'waiting_provider', progress: {}, error: '供应商生成失败', created_at: 10, updated_at: 55, ...overrides });
const terminalFailure = { retry_action: 'retry_failed_segments', error_code: 'reference_image_unreadable', failed_segment_ids: ['seg_1', 'seg_2'] };
const renderCard = (job, extra = {}) => renderToStaticMarkup(React.createElement(RecoveryCard, {
  job, busy: '', canEditVoice: true, canShorten: true, onRetry() {}, onEditVoice() {}, onShorten() {}, onChangePlan() {}, onSettings() {}, ...extra,
}));

test('confirmed terminal video failures offer an explicit failed-only retry', () => {
  const job = fixture({ progress: terminalFailure });
  assert.deepEqual(recoveryForJob(job), { kind: 'retry_failed_segments', label: '重试失败片段' });
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: true });
  assert.equal(recoveryForJob({ ...job, status: 'partial' }).kind, 'retry_failed_segments');
  const html = renderCard(job);
  assert.match(html, /重试失败片段/);
  assert.match(html, /保留已有图片与成功片段/);
  assert.match(html, /视频服务未能读取参考图片/);
  assert.doesNotMatch(html, /恢复任务|role="dialog"/);
});

test('uncertain submissions cannot be resubmitted even with stale retry metadata', () => {
  for (const status of ['submitted_unknown', 'needs_review']) {
    const job = fixture({ status, progress: terminalFailure });
    assert.equal(recoveryForJob(job).kind, 'reconcile');
    assert.equal(retryRequest(job), null);
    const html = renderCard(job);
    assert.match(html, /提交结果待核对/);
    assert.doesNotMatch(html, /<button|重试失败片段|恢复任务/);
  }
});

test('voice-too-long failures offer editing instead of another provider call', () => {
  for (const job of [fixture({ progress: { ...terminalFailure, error_code: 'voice_too_long' } }),
    fixture({ error: '实测配音长于视频计划，请缩短台词后重新确认' })]) {
    assert.equal(recoveryForJob(job).kind, 'edit_voice');
    assert.equal(retryRequest(job), null);
    const html = renderCard(job);
    assert.match(html, /修改台词/);
    assert.match(html, /自动精简台词/);
    assert.doesNotMatch(html, /重试失败片段|恢复任务/);
  }
});

test('only exhausted planning request failures expose a last-resort resume', () => {
  const job = fixture({ kind: 'plan', progress: { error_code: 'planning_request_failed', retry_action: 'retry_planning', retryable: true } });
  assert.deepEqual(recoveryForJob(job), { kind: 'retry_planning', label: '继续处理' });
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
  const html = renderCard(job);
  assert.match(html, /继续处理/);
  assert.match(html, /这是系统问题，已记录/);
  assert.doesNotMatch(html, /重新尝试|请重试|恢复任务|输入修改要求|检查素材|请重新策划/);
  for (const progress of [{ error_code: 'planning_request_failed' },
    { ...job.progress, retryable: false }, { error_code: 'planning_content_failed', retry_action: 'retry_planning' }]) {
    assert.equal(retryRequest({ ...job, progress }), null);
  }
});

test('a capacity planning failure can be resumed with 继续处理', () => {
  const job = fixture({
    kind: 'plan',
    error: '本次方案未能满足已确认要求，暂未采用。已有方案和素材已保留',
    progress: { error_code: 'planning_content_failed', retryable: false, retry_action: 'edit_plan' },
  });
  assert.deepEqual(recoveryForJob(job), { kind: 'retry_planning', label: '继续处理' });
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
});

test('a storyboard content failure retries by regenerating the board', () => {
  const job = fixture({
    kind: 'plan',
    error: '背景人物没有跟着床入画，这次不采用。已有方案和素材已保留',
    progress: { error_code: 'planning_content_failed', retryable: true, retry_action: 'retry_planning',
      issue_summary: ['背景人物没有跟着床入画'] },
  });
  assert.deepEqual(recoveryForJob(job), { kind: 'retry_planning', label: '继续处理' });
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
  const html = renderCard(job);
  assert.match(html, /继续处理/);
  assert.match(html, /重新生成分镜/);
  assert.match(html, /背景人物没有跟着床入画/);
  assert.doesNotMatch(html, /输入修改要求|这是系统问题/);
});

test('a structure failure can be resumed with 继续处理', () => {
  const job = fixture({
    kind: 'plan',
    error: '本次方案结构不完整，暂未采用。已有方案和素材已保留',
    progress: { error_code: 'planning_content_failed', retryable: false, retry_action: 'edit_plan' },
  });
  assert.deepEqual(recoveryForJob(job), { kind: 'retry_planning', label: '继续处理' });
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
  assert.match(renderCard(job), /继续处理/);
});

test('content and legacy planning failures focus the existing composer without resubmission', () => {
  for (const progress of [{}, terminalFailure, { error_code: 'planning_content_failed', retryable: false, retry_action: 'edit_plan' }]) {
    const job = fixture({ kind: 'plan', progress });
    assert.equal(recoveryForJob(job).kind, 'edit_plan');
    assert.equal(retryRequest(job), null);
    const html = renderCard(job, { hasExistingResult: true });
    assert.match(html, /本次修改未完成/);
    assert.match(html, /已有成片仍可播放、下载/);
    assert.match(html, /输入修改要求/);
    assert.doesNotMatch(html, /恢复任务|重新尝试|检查素材/);
  }
});

test('planning configuration failure points to settings instead of another attempt or editing', () => {
  const job = fixture({ kind: 'plan', progress: { error_code: 'planning_configuration_failed', retryable: false, retry_action: 'check_connection' } });
  assert.equal(recoveryForJob(job).kind, 'check_connection');
  assert.equal(retryRequest(job), null);
  const html = renderCard(job);
  assert.match(html, /检查连接设置/);
  assert.doesNotMatch(html, /输入修改要求|恢复任务|重新尝试|检查素材/);
});

test('composition continuation never becomes a video resubmission', () => {
  for (const kind of ['recompose', 'cta']) {
    const job = fixture({ kind, progress: terminalFailure });
    assert.deepEqual(recoveryForJob(job), { kind: 'retry_existing', label: '重新合成' });
    assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
    assert.match(renderCard(job), /重新合成/);
    assert.doesNotMatch(renderCard(job), /恢复任务/);
  }
});

test('legacy video and preflight failures only continue the existing task', () => {
  for (const progress of [{}, { error_code: 'reference_image_unreadable' },
    { retry_action: 'retry_failed_segments' }, { ...terminalFailure, failed_segment_ids: [] },
    { ...terminalFailure, failed_segment_ids: [''] }, { ...terminalFailure, failed_segment_ids: [42] }]) {
    const job = fixture({ progress });
    assert.equal(recoveryForJob(job).kind, 'retry_existing');
    assert.equal(retryRequest(job).retry_failed_segments, false);
  }
});

test('non-retryable failures direct the user to materials or plan changes without a POST action', () => {
  for (const progress of [{ retryable: false, retry_action: null, error_code: 'reference_image_unreadable' },
    { ...terminalFailure, retryable: false }]) {
    const job = fixture({ progress });
    assert.equal(recoveryForJob(job).kind, 'check_materials');
    assert.equal(retryRequest(job), null);
    const html = renderCard(job);
    assert.match(html, /重新上传商品图/);
    assert.match(html, /已有方案仍保留/);
    assert.doesNotMatch(html, /恢复任务|重试失败片段|请重新策划/);
    assert.equal((html.match(/<button/g) || []).length, 1);
  }
  assert.equal(recoveryForJob(fixture({ progress: { retryable: false, error_code: 'voice_too_long' } })).kind, 'edit_voice');
});

test('a missed image submission can continue and resubmits only that image', () => {
  const job = fixture({ phase: 'creating_designs', progress: {
    error_code: 'image_submission_unavailable', retryable: true, retry_action: 'retry_existing',
  }, error: '无法连接土豆API，尚未收到任务编号' });
  assert.equal(recoveryForJob(job).kind, 'retry_existing');
  assert.equal(recoveryForJob(job).label, '继续制作');
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
});

test('terminal image failure offers a plan change without another generation attempt', () => {
  const job = fixture({ phase: 'creating_designs', progress: { error_code: 'image_generation_failed', retryable: false },
    error: '图片生成失败；已完成图片保留，请调整方案后开始新版本' });
  assert.equal(recoveryForJob(job).kind, 'edit_plan');
  assert.equal(retryRequest(job), null);
  const html = renderCard(job);
  assert.match(html, /修改方案/);
  assert.match(html, /已完成图片保留/);
  assert.doesNotMatch(html, /恢复任务|重试失败片段|role="dialog"/);
  assert.equal((html.match(/<button/g) || []).length, 1);
});

test('active repair, automatic retry, completed and canceled runs expose no failure action', () => {
  assert.equal(recoveryForJob(undefined), null);
  for (const status of ['queued', 'running', 'ready', 'canceled', 'awaiting_input']) {
    const job = fixture({ status, kind: 'plan', phase: 'retrying_planner_request', progress: { error_code: 'planning_request_failed', retry_action: 'retry_planning' } });
    assert.equal(recoveryForJob(job), null);
    assert.equal(retryRequest(job), null);
    assert.equal(renderCard(job), '');
  }
});

test('pending retry disables the single retry button without another confirmation', () => {
  const html = renderCard(fixture({ progress: terminalFailure }), { busy: 'retry' });
  assert.match(html, /<button[^>]*disabled=""/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.doesNotMatch(html, /role="dialog"/);
});

test('current failure is presented once while historical timeline errors stay available', () => {
  const job = fixture({ error: '测试专用失败原因' });
  const current = renderToStaticMarkup(React.createElement(Timeline, { job, events: [], hideError: true })) + renderCard(job);
  assert.equal((current.match(/测试专用失败原因/g) || []).length, 1);
  const historical = renderToStaticMarkup(React.createElement(Timeline, { job, events: [] }));
  assert.match(historical, /测试专用失败原因/);
});

test('unclassified non-retryable failures do not claim the user supplied bad materials', () => {
  const job = fixture({ progress: { retryable: false, error_code: 'unknown' } });
  assert.equal(recoveryForJob(job).kind, 'unavailable');
  assert.equal(retryRequest(job), null);
  assert.doesNotMatch(renderCard(job), /请检查素材|恢复任务/);
});

test('planning content failure shows saved localized issues and never adds a second form', () => {
  const job = fixture({ kind: 'plan', progress: { error_code: 'planning_content_failed', retryable: false,
    issue_summary: ['第 3 镜：动作时长不足', '第 2 镜：物品状态不连贯'] } });
  const html = renderCard(job);
  assert.match(html, /第 3 镜：动作时长不足/);
  assert.match(html, /第 2 镜：物品状态不连贯/);
  assert.doesNotMatch(html, /<input|<textarea|<form|role="dialog"/);
});


test('an interrupted planner reply keeps the request and one last resume', () => {
  const job = fixture({ kind: 'plan', error: '策划回复中途断开；已有成果仍保留，尚未开始图片或视频生成',
    progress: { error_code: 'planning_response_interrupted', retryable: true, retry_action: 'retry_planning' } });
  assert.equal(recoveryForJob(job).kind, 'retry_planning');
  assert.equal(recoveryForJob(job).label, '继续处理');
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
  const html = renderCard(job);
  assert.match(html, /这是系统问题，已记录/);
  assert.match(html, /方案和图片都还在/);
  assert.match(html, /继续处理/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.doesNotMatch(html, /重新尝试|请重试|结构校验|输入修改要求|继续对话|请重新策划/);
});


test('incomplete checks and request limits do not ask the merchant to revise requirements', () => {
  for (const code of ['planning_review_incomplete', 'planning_request_limit']) {
    const job = fixture({ kind: 'plan', progress: { error_code: code, retryable: false,
      retry_action: 'continue_conversation', issue_summary: ['OLD_CONTENT_ISSUE'] } });
    assert.equal(recoveryForJob(job).kind, 'continue_conversation');
    assert.equal(retryRequest(job), null);
    const html = renderCard(job, { hasExistingResult: true });
    assert.match(html, /按已保存的需求继续/);
    assert.match(html, /已保存的需求/);
    assert.doesNotMatch(html, /继续对话/);
    assert.match(html, /已有成片仍可播放、下载/);
    assert.doesNotMatch(html, /输入修改要求|想调整的要求|OLD_CONTENT_ISSUE|恢复任务|重新尝试/);
    assert.equal((html.match(/<button/g) || []).length, 1);
    if (code === 'planning_review_incomplete') assert.match(html, /本次检查未完成/);
    if (code === 'planning_request_limit') assert.match(html, /次数上限/);
    assert.match(renderCard(job, { busy: 'plan' }), /<button[^>]*disabled=""/);
    assert.equal(retryRequest({ ...job, progress: { ...job.progress, retryable: true, retry_action: 'retry_planning' } }), null);
  }
});

test('a saved storyboard rejected for segment structure resumes that storyboard', () => {
  const job = fixture({ kind: 'plan', phase: 'reviewing_plan',
    error: '视频段镜头时轴或素材结构无效',
    progress: { error_code: 'planning_invalid_response', retryable: false, retry_action: 'edit_plan' } });
  assert.equal(recoveryForJob(job).kind, 'retry_planning');
  assert.equal(recoveryForJob(job).label, '继续处理');
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
  const html = renderCard(job);
  assert.match(html, /继续处理/);
  assert.match(html, /这是系统问题，已记录/);
  assert.doesNotMatch(html, /不能沿用刚才的进度|按已保存的需求继续|输入修改要求|请重新策划/);
  const exhausted = { ...job, progress: { ...job.progress, retry_action: 'continue_conversation' } };
  assert.equal(recoveryForJob(exhausted).kind, 'continue_conversation');
});

test('a structurally invalid planning reply resumes only after automatic recovery is exhausted', () => {
  const held = fixture({ kind: 'plan', error: '规划结果未通过结构校验，已有成果仍保留；尚未开始图片或视频生成',
    progress: { error_code: 'planning_invalid_response', retryable: false, retry_action: 'edit_plan' } });
  assert.equal(recoveryForJob(held).kind, 'continue_conversation');
  assert.equal(retryRequest(held), null);
  const job = fixture({ kind: 'plan', error: '规划结果未通过结构校验，已有成果仍保留；尚未开始图片或视频生成',
    progress: { error_code: 'planning_invalid_response', retryable: true, retry_action: 'retry_planning' } });
  assert.equal(recoveryForJob(job).kind, 'retry_planning');
  assert.equal(recoveryForJob(job).label, '继续处理');
  assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
  const html = renderCard(job);
  assert.match(html, /这是系统问题，已记录/);
  assert.match(html, /方案和图片都还在/);
  assert.match(html, /继续处理/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.doesNotMatch(html, /重新尝试|请重试|输入修改要求|继续对话/);
});

function buttons(node, found = []) {
  if (!node || typeof node !== 'object') return found;
  if (node.type === 'button') found.push(node);
  const children = node.props ? node.props.children : undefined;
  for (const child of [].concat(children ?? [])) buttons(child, found);
  return found;
}

test('a rejected recovery request shows the server reason on the card', () => {
  const job = fixture({ kind: 'plan', progress: { error_code: 'planning_invalid_response', retryable: true,
    retry_action: 'retry_planning' } });
  const html = renderCard(job, { error: '当前策划进度不能重新尝试，请通过输入框说明修改要求；已有成果仍保留' });
  assert.match(html, /role="alert"/);
  assert.match(html, /当前策划进度不能重新尝试/);
  assert.match(html, /继续处理/);
});

test('every planning recovery button calls an action that state can accept', () => {
  const cases = [
    [{ error_code: 'planning_invalid_response', retryable: true, retry_action: 'retry_planning' }, 'retry', '继续处理'],
    [{ error_code: 'planning_request_failed', retryable: true, retry_action: 'retry_planning' }, 'retry', '继续处理'],
    [{ error_code: 'planning_response_interrupted', retryable: true, retry_action: 'retry_planning' }, 'retry', '继续处理'],
    [{ error_code: 'planning_invalid_response', retryable: false, retry_action: 'edit_plan' }, 'resume', '按已保存的需求继续'],
    [{ error_code: 'planning_review_incomplete', retryable: false, retry_action: 'continue_conversation' }, 'resume', '按已保存的需求继续'],
    [{ error_code: 'planning_request_limit', retryable: false, retry_action: 'continue_conversation' }, 'resume', '按已保存的需求继续'],
    [{ error_code: 'planning_configuration_failed', retryable: false, retry_action: 'check_connection' }, 'settings', '检查连接设置'],
  ];
  for (const [progress, expected, label] of cases) {
    const calls = [];
    const job = fixture({ kind: 'plan', progress });
    const element = RecoveryCard({ job, busy: '', canEditVoice: true, canShorten: true,
      onRetry() { calls.push('retry'); }, onEditVoice() { calls.push('voice'); }, onShorten() { calls.push('shorten'); },
      onChangePlan() { calls.push('focus'); }, onRestart() { calls.push('resume'); }, onSettings() { calls.push('settings'); } });
    const found = buttons(element);
    assert.equal(found.length, 1, progress.error_code);
    assert.match(JSON.stringify(found[0].props.children), new RegExp(label));
    found[0].props.onClick(job);
    assert.deepEqual(calls, [expected], progress.error_code);
    if (expected === 'retry') assert.ok(retryRequest(job));
    else assert.equal(retryRequest(job), null);
  }
});

test('failure cards never show field names, pydantic, or validation wording', () => {
  const { SYSTEM_FAILURE_COPY } = loadSource('jobRecovery.ts');
  const samples = [
    fixture({ error: '4 validation errors for CreativeOption\nsegment.speech_characters_per_tenth\n  Value error' }),
    fixture({ error: '独立旁白方案的声音策略、内容或跨段对应无效，请重新策划' }),
    fixture({ kind: 'plan', error: 'speech_spans pydantic validation',
      progress: { error_code: 'planning_invalid_response', retry_action: 'retry_planning', retryable: true } }),
    fixture({ progress: { error_code: 'voice_too_long' }, error: 'speech_characters_per_tenth validation' }),
    fixture({ progress: { retryable: false, error_code: 'reference_image_unreadable' }, error: 'reference_asset_ids' }),
    fixture({ kind: 'plan', progress: { error_code: 'planning_content_failed', retryable: false, retry_action: 'edit_plan',
      issue_summary: ['shot.speech_characters_per_tenth failed validation', '第 1 镜：台词太满'] } }),
  ];
  for (const job of samples) {
    const html = renderCard(job, { error: 'pydantic ValidationError speech_characters_per_tenth' });
    assert.equal(SYSTEM_FAILURE_COPY, '这是系统问题，已记录；你的方案和图片都还在。');
    assert.doesNotMatch(html, /pydantic|validation|speech_characters_per_tenth|speech_spans|CreativeOption|reference_asset_ids|请重新策划/i);
  }
  const kept = renderCard(samples[5]);
  assert.match(kept, /第 1 镜：台词太满/);
  assert.doesNotMatch(kept, /speech_characters_per_tenth|validation/i);
});

test('an account or balance refusal offers 重新生成 and keeps the saved work', () => {
  const cases = [
    ['provider_balance', /请补充额度后再制作/],
    ['provider_auth', /账户修好后可以重新生成/],
  ];
  for (const [code, sentence] of cases) {
    const job = fixture({
      error: '账户问题',
      progress: { error_code: code, retryable: false, retry_action: null, failed_segment_ids: ['seg_1'] },
    });
    assert.deepEqual(recoveryForJob(job), { kind: 'retry_existing', label: '重新生成' });
    assert.deepEqual(retryRequest(job), { job_id: job.id, retry_failed_segments: false });
    const html = renderCard(job);
    assert.match(html, sentence);
    assert.match(html, /重新生成/);
    assert.match(html, /本次制作未完成/);
    assert.doesNotMatch(html, /重试失败片段|视频片段未生成|继续制作/);
    assert.equal((html.match(/<button/g) || []).length, 1);
  }
});

test('an exhausted or permanent video refusal uses the system sentence', () => {
  const job = fixture({
    error: '这是系统问题，已记录；你的方案和图片都还在。',
    progress: { error_code: 'provider_request_rejected', retryable: false, retry_action: 'retry_failed_segments', failed_segment_ids: ['seg_1'] },
  });
  assert.equal(recoveryForJob(job).kind, 'unavailable');
  assert.equal(retryRequest(job), null);
  const html = renderCard(job);
  assert.match(html, /这是系统问题，已记录/);
  assert.match(html, /方案和图片都还在/);
  assert.doesNotMatch(html, /重试失败片段|视频片段未生成|继续制作|<button/);
});

test('a spend-time recheck sends the merchant back to the composer', () => {
  const job = fixture({
    error: '这版方案需要重新检查。请在对话里改完再制作，这次不会生成图片或视频。',
    progress: { error_code: 'release_recheck', retryable: false, retry_action: 'edit_plan' },
  });
  assert.deepEqual(recoveryForJob(job), { kind: 'edit_plan', label: '回到对话修改' });
  assert.equal(retryRequest(job), null);
  const calls = [];
  const element = RecoveryCard({ job, busy: '', canEditVoice: true, canShorten: true, hasExistingResult: false,
    onRetry() { calls.push('retry'); }, onEditVoice() {}, onShorten() {},
    onChangePlan() { calls.push('focus'); }, onRestart() {}, onSettings() {} });
  const html = renderToStaticMarkup(element);
  assert.match(html, /这版方案需要重新检查/);
  assert.match(html, /回到对话修改/);
  assert.doesNotMatch(html, /继续制作|重新生成/);
  const button = buttons(element).find(node => [].concat(node.props.children).some(child => child === '回到对话修改'));
  assert.ok(button);
  button.props.onClick();
  assert.deepEqual(calls, ['focus']);
});

test('the last-resort card button requests a restart from the saved brief', () => {
  const calls = [];
  const job = fixture({ kind: 'plan', progress: { error_code: 'planning_invalid_response', retryable: false,
    retry_action: 'edit_plan' } });
  const element = RecoveryCard({ job, busy: '', canEditVoice: true, canShorten: true, hasExistingResult: false,
    onRetry() { calls.push('retry'); }, onEditVoice() {}, onShorten() {},
    onChangePlan() { calls.push('focus'); }, onRestart() { calls.push('resume'); }, onSettings() {} });
  const button = buttons(element).find(node => [].concat(node.props.children).some(child => child === '按已保存的需求继续'));
  assert.ok(button);
  button.props.onClick();
  assert.deepEqual(calls, ['resume']);
});
