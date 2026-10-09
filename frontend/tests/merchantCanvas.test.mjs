// Offline presentation checks. No network, providers, or production project data.
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

const { originalAssetsForMessage, unplacedOriginalAssets } = loadSource('messageAttachments.ts');
const { canvasShowsVideoSection } = loadSource('canvasStage.ts');

const message = (id, role, content, created_at) => ({ id, role, content, created_at });
const image = (id, created_at, metadata = {}) => ({
  id, project_id: 'p', kind: 'product', name: '香水.png', url: `/media/${id}`, mime: 'image/png',
  revision: 0, metadata: { generated: false, ...metadata }, created_at,
});

test('attachments follow the message that was sent', () => {
  const messages = [
    message('m1', 'user', '你好', 10),
    message('a1', 'assistant', '先发一下商品的原图', 20),
    message('m2', 'user', '我想做一个香水广告', 40),
  ];
  const assets = [image('img', 30)];
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[0]).map(item => item.id), []);
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[2]).map(item => item.id), ['img']);
});

test('an opening image stays on the first message after later replies', () => {
  const messages = [
    message('m1', 'user', '这是香水', 10),
    message('a1', 'assistant', '收到', 30),
    message('m2', 'user', '改短一点', 40),
  ];
  const assets = [image('img', 12)];
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[0]).map(item => item.id), ['img']);
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[2]).map(item => item.id), []);
});

test('a saved message id keeps the image on that send', () => {
  const messages = [
    message('m1', 'user', '你好', 10),
    message('m2', 'user', '我想做一个香水广告', 15),
  ];
  const assets = [image('img', 12, { message_id: 'm2' })];
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[0]).map(item => item.id), []);
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[1]).map(item => item.id), ['img']);
});

test('an image-only follow-up is not pinned to the first message', () => {
  const messages = [
    message('m1', 'user', '你好', 10),
    message('a1', 'assistant', '先发原图', 20),
  ];
  const assets = [image('img', 30, { sent_without_message: true })];
  assert.deepEqual(originalAssetsForMessage(messages, assets, messages[0]).map(item => item.id), []);
  assert.deepEqual(unplacedOriginalAssets(messages, assets).map(item => item.id), ['img']);
});

function videoMarkup(state) {
  if (!canvasShowsVideoSection(state)) return '';
  return renderToStaticMarkup(React.createElement('section', { className: 'canvas-card video-card' },
    React.createElement('h2', null, '视频生成')));
}

test('the canvas does not render video generation while reference images are prepared', () => {
  const preparing = { hasRender: false, hasSegments: false, kind: 'generate', pendingGateKind: null };
  for (const phase of ['queued', 'creating_designs', 'synthesizing_voice', 'awaiting_creative']) {
    assert.equal(videoMarkup({ ...preparing, phase }), '', phase);
  }
  assert.equal(videoMarkup({ hasRender: false, hasSegments: false, phase: null, kind: null, pendingGateKind: 'creative' }), '');
  const started = videoMarkup({ hasRender: false, hasSegments: false, phase: 'submitting_video', kind: 'generate', pendingGateKind: null });
  assert.match(started, /视频生成/);
  assert.match(videoMarkup({ hasRender: true, hasSegments: false, phase: 'creating_designs', kind: 'generate', pendingGateKind: null }), /视频生成/);
});
