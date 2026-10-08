import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createElement } from 'react';

const frontend = join(dirname(fileURLToPath(import.meta.url)), '..');
const requireFrontend = createRequire(join(frontend, 'package.json'));
const { buildSync } = requireFrontend('esbuild');

function loadSource(name) {
  const { outputFiles } = buildSync({ entryPoints: [join(frontend, 'src', name)], absWorkingDir: frontend,
    bundle: true, platform: 'node', format: 'cjs', packages: 'external', jsx: 'automatic', loader: { '.css': 'empty' }, write: false });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputFiles[0].text)(requireFrontend, module, module.exports);
  return module.exports;
}

const timeline = loadSource('RunTimeline.tsx');
const { merchantFacingStatus } = timeline;
const ReactDOMServer = requireFrontend('react-dom/server');

test('a skill id stays out of the status line', () => {
  assert.equal(merchantFacingStatus('reviewing_directions', '已载入共用参考 ad-form-playbook-brand-tvc · 1.4.6'), '正在想方向');
  assert.equal(merchantFacingStatus('writing_script', '已载入阶段指令 ads-video · 1.0'), '正在策划广告内容');
  assert.equal(merchantFacingStatus('storyboarding', '硬门没通过，请修改'), '正在安排画面和台词');
  assert.equal(merchantFacingStatus('storyboarding', '正在安排画面和台词'), '正在安排画面和台词');
});

test('the closed timeline shows the step instead of the skill id', () => {
  const html = ReactDOMServer.renderToStaticMarkup(createElement(timeline.default, {
    job: { id: 'job-1', project_id: 'project-1', kind: 'plan', status: 'running', phase: 'reviewing_directions', created_at: 1, progress: {} },
    events: [{ seq: 1, job_id: 'job-1', project_id: 'project-1', kind: 'step', state: 'running', created_at: 1,
      label: '已载入共用参考 ad-form-playbook-brand-tvc · 1.4.6' }],
  }));
  assert.match(html, /run-current-label/);
  assert.match(html, /正在想方向/);
  assert.doesNotMatch(html, /run-current-label[^<]*ad-form-playbook/);
  assert.match(html, /1 条执行记录/);
});
