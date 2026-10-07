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
const { videoGenerationRecord, copyVideoPrompt, videoViewerNote, isCanvasVideo } = loadSource('videoPrompt.ts');
const VideoPromptPanel = loadSource('VideoPromptPanel.tsx').default;
const viewer = loadSource('VideoViewer.tsx');
const VideoViewer = viewer.default;
const { CanvasVideoButton } = viewer;
const asset = (metadata, extra = {}) => ({ id: 'asset-1', name: '视频段 s0', url: '/media/asset-1', metadata, ...extra });
const markup = (node) => renderToStaticMarkup(node);

test('a generated clip shows the exact prompt and model, not the reference images', () => {
  const prompt = '机位停住，产品从第一帧就以近景留在故事里。';
  const record = videoGenerationRecord(asset({
    video_prompt: prompt, model: 'wan3.0',
    video_inputs: [
      { role: 'product', label: '商品原图', asset_id: 'prod' },
      { role: 'scene', label: '场景参考' },
      { role: 'state', label: '头发湿·起始' },
    ],
    image_prompt: '不要显示图片提示',
  }));
  assert.equal(record.prompt, prompt);
  assert.equal(record.model, 'wan3.0');
  assert.equal(record.inputs, undefined);
  const html = markup(React.createElement(VideoPromptPanel, { asset: asset({
    video_prompt: prompt, model: 'wan3.0',
    video_inputs: [{ role: 'product', label: '商品原图' }, { role: 'scene', label: '场景参考' }],
  }) }));
  assert.match(html, /<h4>提示词<\/h4>/);
  assert.match(html, /模型：wan3.0/);
  assert.match(html, /机位停住，产品从第一帧就以近景留在故事里。/);
  assert.match(html, /复制提示词/);
  assert.doesNotMatch(html, /依据|商品原图|场景参考|头发湿/);
  assert.doesNotMatch(html, /disabled/);
  assert.doesNotMatch(html, /<textarea|<img/);
  assert.doesNotMatch(html, /重新生成|修改提示/);
});

test('an old clip with no stored request says 暂无记录 and does not reuse the plan prompt', () => {
  const missing = videoGenerationRecord(asset({ prompt: '方案里的短提示', image_prompt: '图片提示' }));
  assert.equal(missing.prompt, null);
  assert.equal(missing.model, null);
  const html = markup(React.createElement(VideoPromptPanel, { asset: asset({
    video_inputs: [{ role: 'product', label: '商品原图' }],
  }) }));
  assert.match(html, /模型：暂无记录/);
  assert.match(html, /<pre class="image-prompt-text">暂无记录<\/pre>/);
  assert.match(html, /disabled/);
  assert.doesNotMatch(html, /方案里的短提示|图片提示|依据|商品原图/);
});

test('the viewer plays the clip beside the read-only prompt, and a canvas click only opens it', async () => {
  const prompt = 'SENT_s0：产品从第一帧就在。';
  const clip = asset({ video_prompt: prompt, model: 'wan3.0', video_inputs: [{ role: 'product', label: '商品原图' }] }, { kind: 'segment' });
  const html = markup(React.createElement(VideoViewer, { asset: clip, note: videoViewerNote(clip) }));
  assert.match(html, /<video [^>]*controls=""[^>]*autoPlay=""[^>]*muted=""[^>]*playsInline=""/);
  assert.match(html, /aria-label="视频段 s0"/);
  assert.match(html, /素材与修改意见/);
  assert.match(html, /这一段是发给视频模型的片段/);
  assert.match(html, /SENT_s0：产品从第一帧就在。/);
  assert.doesNotMatch(html, /依据|商品原图/);
  assert.match(html, /下载视频/);
  assert.match(html, /提示词只读，这里不能修改或重新生成。/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.doesNotMatch(html, /引用到对话|<textarea/);
  const film = asset({}, { kind: 'render', name: '广告成片' });
  assert.match(videoViewerNote(film), /每一段发给模型的提示写在对应片段上/);
  assert.equal(isCanvasVideo(clip), true);
  assert.equal(isCanvasVideo(asset({}, { kind: 'scene' })), false);
  let opened = 0;
  const button = CanvasVideoButton({ label: '查看生成片段1', src: '/media/asset-1', onOpen: () => { opened += 1; } });
  const thumb = markup(button);
  assert.match(thumb, /aria-label="查看生成片段1"/);
  assert.match(thumb, /<video [^>]*muted=""[^>]*playsInline=""/);
  assert.doesNotMatch(thumb, /controls/);
  button.props.onClick();
  assert.equal(opened, 1);
  const written = [];
  const previous = navigator.clipboard;
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { written.push(value); } } });
  try {
    assert.equal(await copyVideoPrompt(prompt), true);
    assert.equal(await copyVideoPrompt(null), false);
    assert.deepEqual(written, [prompt]);
  } finally {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: previous });
  }
});
