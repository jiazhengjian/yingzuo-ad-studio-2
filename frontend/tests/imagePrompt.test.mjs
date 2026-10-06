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
const { imageGenerationRecord, imageInputBasis, copyImagePrompt } = loadSource('imagePrompt.ts');
const ImagePromptPanel = loadSource('ImagePromptPanel.tsx').default;
const asset = (metadata) => ({ metadata });
const markup = (metadata) => renderToStaticMarkup(React.createElement(ImagePromptPanel, { asset: asset(metadata) }));

test('a generated image shows the exact prompt, model and reference that were saved', () => {
  const prompt = '这是对起始状态四视图的改图，不是重画一个人。';
  const record = imageGenerationRecord(asset({
    image_prompt: prompt, model: 'gpt-image-2-all',
    image_inputs: [{ role: 'state_start', label: '起始状态参考' }],
    design_request: { prompt: '不要用这份旧提示' },
  }));
  assert.equal(record.prompt, prompt);
  assert.equal(record.model, 'gpt-image-2-all');
  assert.equal(imageInputBasis(record.inputs), '依据：起始状态参考');
  const html = markup({
    image_prompt: prompt, model: 'gpt-image-2-all',
    image_inputs: [{ role: 'state_start', label: '起始状态参考' }],
  });
  assert.match(html, /<h4>提示词<\/h4>/);
  assert.match(html, /模型：gpt-image-2-all/);
  assert.match(html, /依据：起始状态参考/);
  assert.match(html, /这是对起始状态四视图的改图，不是重画一个人。/);
  assert.match(html, /复制提示词/);
  assert.doesNotMatch(html, /disabled/);
  assert.doesNotMatch(html, /<textarea/);
});

test('an empty input list says there was no reference, and a missing record says 无记录', () => {
  const sheet = imageGenerationRecord(asset({ image_prompt: '四视图', model: 'gpt-image-2-all', image_inputs: [] }));
  assert.equal(imageInputBasis(sheet.inputs), '依据：没有参考图');
  const html = markup({ image_prompt: '四视图', model: 'gpt-image-2-all', image_inputs: [] });
  assert.match(html, /依据：没有参考图/);
  assert.match(html, /四视图/);
  const missing = imageGenerationRecord(asset({ generated: false }));
  assert.equal(missing.prompt, null);
  assert.equal(missing.model, null);
  assert.equal(missing.inputs, null);
  assert.equal(imageInputBasis(missing.inputs), '依据：无记录');
  const empty = markup({});
  assert.match(empty, /模型：无记录/);
  assert.match(empty, /依据：无记录/);
  assert.match(empty, /<pre class="image-prompt-text">无记录<\/pre>/);
  assert.match(empty, /disabled/);
});

test('an older image uses the saved design prompt and does not invent its inputs', () => {
  const record = imageGenerationRecord(asset({
    model: 'gpt-image-2-all',
    design_request: { prompt: '已确认的场景与视觉方案' },
  }));
  assert.equal(record.prompt, '已确认的场景与视觉方案');
  assert.equal(record.inputs, null);
  const html = markup({ model: 'gpt-image-2-all', design_request: { prompt: '已确认的场景与视觉方案' } });
  assert.match(html, /已确认的场景与视觉方案/);
  assert.match(html, /依据：无记录/);
});

test('copy writes the exact prompt and does nothing when there is no record', async () => {
  const written = [];
  const previous = navigator.clipboard;
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { written.push(value); } } });
  try {
    assert.equal(await copyImagePrompt('脸、五官、肤色都按这句'), true);
    assert.equal(await copyImagePrompt(null), false);
    const panel = ImagePromptPanel({ asset: asset({
      image_prompt: '脸、五官、肤色都按这句', model: 'gpt-image-2-all',
      image_inputs: [{ role: 'state_start', label: '起始状态参考' }],
    }) });
    const button = panel.props.children.find(child => child && child.type === 'button');
    button.props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(written, ['脸、五官、肤色都按这句', '脸、五官、肤色都按这句']);
    assert.equal(button.props.disabled, false);
  } finally {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: previous });
  }
});
