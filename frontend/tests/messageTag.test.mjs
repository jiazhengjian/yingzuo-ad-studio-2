// First-message chip uses the homepage type card only. No network or providers.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const frontend = join(dirname(fileURLToPath(import.meta.url)), '..');
const requireFrontend = createRequire(join(frontend, 'package.json'));
const { buildSync } = requireFrontend('esbuild');
const React = requireFrontend('react');
const { renderToStaticMarkup } = requireFrontend('react-dom/server');

const memory = new Map();
globalThis.sessionStorage = {
  getItem(key) { return memory.has(key) ? memory.get(key) : null; },
  setItem(key, value) { memory.set(key, String(value)); },
  removeItem(key) { memory.delete(key); },
};

function loadSource(name) {
  const { outputFiles } = buildSync({ entryPoints: [join(frontend, 'src', name)], absWorkingDir: frontend,
    bundle: true, platform: 'node', format: 'cjs', packages: 'external', jsx: 'automatic', loader: { '.css': 'empty' }, write: false });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputFiles[0].text)(requireFrontend, module, module.exports);
  return module.exports;
}

const { rememberHomepageAdForm, homepageMessageTag } = loadSource('adForm.ts');
const HomeTypeTag = loadSource('HomeTypeTag.tsx').default;
const appSource = readFileSync(join(frontend, 'src', 'App.tsx'), 'utf8');

const chip = projectId => renderToStaticMarkup(React.createElement(HomeTypeTag, { projectId }));

test('a homepage type becomes the matching chip, using the card words', () => {
  const cases = [
    ['brand_tvc', '@ 产品展示'],
    ['scene_story', '@ 剧情'],
    ['presenter_pitch', '@ 人物讲解'],
  ];
  for (const [form, label] of cases) {
    rememberHomepageAdForm(form, form);
    assert.equal(homepageMessageTag(form), label);
    const html = chip(form);
    assert.match(html, new RegExp(`class="message-tag">${label}`));
    assert.equal(html.includes('商品广告'), false);
    assert.equal(html.includes('产品展示广告'), false);
    assert.equal(html.includes('剧情／场景'), false);
    assert.equal(html.includes('口播'), false);
  }
});

test('no homepage card means no chip, including auto and a later project form', () => {
  rememberHomepageAdForm('plain', null);
  rememberHomepageAdForm('auto-start', 'auto');
  assert.equal(homepageMessageTag('plain'), null);
  assert.equal(homepageMessageTag('auto-start'), null);
  assert.equal(homepageMessageTag(null), null);
  assert.equal(homepageMessageTag('never-picked'), null);
  assert.equal(chip('plain'), '');
  assert.equal(chip('never-picked'), '');
  assert.equal(chip(null), '');
});

test('a reloaded session still shows only the homepage card that was saved', () => {
  rememberHomepageAdForm('kept', 'presenter_pitch');
  rememberHomepageAdForm('skipped', null);
  const reloaded = loadSource('adForm.ts');
  assert.equal(reloaded.homepageMessageTag('kept'), '@ 人物讲解');
  assert.equal(reloaded.homepageMessageTag('skipped'), null);
  const ReloadedTag = loadSource('HomeTypeTag.tsx').default;
  const html = renderToStaticMarkup(React.createElement(ReloadedTag, { projectId: 'kept' }));
  assert.match(html, /class="message-tag">@ 人物讲解/);
  assert.equal(renderToStaticMarkup(React.createElement(ReloadedTag, { projectId: 'skipped' })), '');
});

test('the conversation no longer hardcodes the product-ad chip', () => {
  assert.equal(appSource.includes('@ 商品广告'), false);
  assert.match(appSource, /rememberHomepageAdForm\(created\.id, selectedForm\)/);
  assert.match(appSource, /<HomeTypeTag projectId=\{project\?\.id\}\/>/);
});
