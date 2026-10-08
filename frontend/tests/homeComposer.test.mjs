// Homepage type cards and the removed duplicate entries. No network or providers.
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

const Home = loadSource('Home.tsx').default;
const { homeAdChoices, homeAdChoice, nextHomeAdForm } = loadSource('adForm.ts');

const base = {
  brand: '映作', draft: '', onDraft() {}, adForm: null, onAdFormChange() {}, durationSeconds: 20, onDurationChange() {},
  files: [], onFiles() {}, onRemoveFile() {}, onSubmit() {}, onOpenProjects() {}, busy: false, online: true,
  plannerConfigured: true, onSettings() {}, loading: false, error: '', onDismissError() {},
};
const html = props => renderToStaticMarkup(React.createElement(Home, { ...base, ...props }));

test('the three homepage cards are the stored ad forms, and 人物讲解 is presenter_pitch', () => {
  assert.deepEqual(homeAdChoices.map(item => item.id), ['brand_tvc', 'scene_story', 'presenter_pitch']);
  assert.deepEqual(homeAdChoices.map(item => item.name), ['产品展示', '剧情', '人物讲解']);
  assert.equal(homeAdChoice('presenter_pitch').name, '人物讲解');
  assert.equal(homeAdChoice('auto'), null);
  assert.equal(nextHomeAdForm(null, 'scene_story'), 'scene_story');
  assert.equal(nextHomeAdForm('scene_story', 'presenter_pitch'), 'presenter_pitch');
  assert.equal(nextHomeAdForm('presenter_pitch', 'presenter_pitch'), null);
});

test('an unselected home has no type tag and no duplicate entries', () => {
  const page = html();
  assert.equal(page.includes('home-prompt-label'), false);
  assert.equal(page.includes('商品广告'), false);
  assert.equal(page.includes('>媒体<'), false);
  assert.equal(page.includes('创作设置'), false);
  assert.equal(page.includes('>设置<'), false);
  assert.equal(page.includes('最近的创作'), false);
  assert.equal(page.includes('干净产品片'), false);
  for (const name of ['产品展示', '剧情', '人物讲解']) assert.match(page, new RegExp(name));
  assert.match(page, /连接设置/);
  assert.match(page, /开始创作/);
  assert.match(page, /20秒/);
  assert.equal(page.includes('aria-pressed="true"'), false);
});

test('a chosen type is the only label on the input', () => {
  const page = html({ adForm: 'presenter_pitch' });
  assert.match(page, /class="home-prompt-label"[^>]*>.*?人物讲解/);
  assert.equal(page.includes('商品广告'), false);
  assert.match(page, /aria-pressed="true"/);
  const pressed = page.match(/aria-pressed="true"[\s\S]*?<strong>([^<]+)/);
  assert.equal(pressed[1], '人物讲解');
});
