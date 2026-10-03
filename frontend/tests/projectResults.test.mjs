// Pure presentation checks; no network, providers, or production project data.
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
const { playableRender, resultWorkLabel, projectListStatus } = loadSource('projectResults.ts');
const SavedResult = loadSource('SavedResultCard.tsx').default;
const RecoveryCard = loadSource('JobRecoveryCard.tsx').default;
const render = { id: 'saved-render', asset_id: 'saved-video', revision: 3, status: 'ready',
  created_at: 100, url: '/media/saved-video', manifest: {} };
const job = { id: 'new-attempt', project_id: 'project', kind: 'plan', revision: 3, status: 'failed', phase: 'reviewing_plan',
  created_at: 200, updated_at: 210, progress: { error_code: 'planning_content_failed', retryable: false, retry_action: 'edit_plan' } };
const fixture = (overrides = {}) => ({ project: { id: 'project', revision: 3, context_version: 9, plan_context_version: 8,
  status: 'failed', work_status: 'failed', result_status: 'ready', result_revision: 3, plan: {}, current_render_id: render.id },
  renders: [render], jobs: [job], assets: [], messages: [], events: [], gates: [], ...overrides });
const markup = detail => renderToStaticMarkup(React.createElement(SavedResult, { detail, onPlay() {} }));

test('a failed new request leaves the original video immediately playable and downloadable', () => {
  const detail = fixture();
  const snapshot = structuredClone(detail);
  assert.equal(playableRender(detail), render);
  const html = markup(detail) + renderToStaticMarkup(React.createElement(RecoveryCard, { job, busy: '',
    hasExistingResult: true, canEditVoice: true, canShorten: true, onRetry() {}, onEditVoice() {},
    onShorten() {}, onChangePlan() {}, onSettings() {} }));
  assert.match(html, /已有成片 V3/);
  assert.match(html, /播放成片/);
  assert.match(html, /href="\/media\/saved-video\?download=true"/);
  assert.match(html, /本次修改未完成，已有成片保留/);
  assert.doesNotMatch(html, /恢复任务|重新尝试/);
  assert.deepEqual(detail, snapshot, 'rendering does not modify the saved project or attempt');
});

test('playing the saved result uses the existing viewer without a retry or generation action', () => {
  let opens = 0;
  const element = SavedResult({ detail: fixture(), onPlay: () => { opens++; } });
  const actions = element.props.children.find(child => child.props.className === 'saved-result-actions');
  const play = actions.props.children.find(child => child.type === 'button');
  play.props.onClick();
  assert.equal(opens, 1);
  assert.equal(play.props.disabled, undefined);
});

test('automatic repair and service retries preserve playback without recovery buttons', () => {
  for (const phase of ['refining_storyboard', 'retrying_planner', 'retrying_planner_request']) {
    const running = { ...job, status: 'running', phase };
    const detail = fixture({ jobs: [running] });
    const html = markup(detail) + renderToStaticMarkup(React.createElement(RecoveryCard, { job: running }));
    assert.match(html, /正在修改方案，已有成片保留/);
    assert.match(html, /播放成片/);
    assert.doesNotMatch(html, /恢复任务|重新尝试|输入修改要求/);
  }
});

test('a failed or pending replacement cannot shadow the ready render', () => {
  const replacement = { ...render, id: 'replacement', asset_id: 'pending-video', created_at: 300, status: 'failed', revision: 4 };
  const detail = fixture({ renders: [render, replacement] });
  detail.project.current_render_id = replacement.id;
  detail.project.revision = 4;
  assert.equal(playableRender(detail).asset_id, 'saved-video');
  assert.match(markup(detail), /已有成片 V3/);
  assert.doesNotMatch(markup(detail), /pending-video/);
});

test('confirmed replacement becomes displayed work and the earlier version stays in history', () => {
  const replacement = { ...render, id: 'replacement', asset_id: 'new-video', created_at: 300, revision: 4 };
  const detail = fixture({ renders: [render, replacement], jobs: [] });
  Object.assign(detail.project, { current_render_id: replacement.id, revision: 4, plan_context_version: 9 });
  assert.equal(playableRender(detail), replacement);
  assert.equal(resultWorkLabel(detail), '成片已保存，可随时播放或下载');
  assert.equal(detail.renders.length, 2);
});

test('a finished new plan does not claim its video has already been generated', () => {
  const detail = fixture({ jobs: [{ ...job, revision: 4, status: 'ready' }] });
  Object.assign(detail.project, { revision: 4, plan_context_version: 9 });
  assert.equal(resultWorkLabel(detail), '新方案尚未生成视频，已有成片保留');
});

test('home and sidebar status distinguish saved work from the failed or active modification', () => {
  const project = fixture().project;
  assert.equal(projectListStatus(project), '已有成片 · 本次修改未完成');
  assert.equal(projectListStatus({ ...project, work_status: 'running' }), '已有成片 · 正在修改');
  assert.equal(projectListStatus({ ...project, result_status: 'none' }), '本次创作未完成');
  assert.equal(projectListStatus({ ...project, result_status: undefined }), '已有成片 · 本次修改未完成');
});

test('no saved result means no fabricated playback or download control', () => {
  const detail = fixture({ renders: [] });
  assert.equal(playableRender(detail), undefined);
  assert.equal(markup(detail), '');
  assert.equal(resultWorkLabel(detail), '');
});
