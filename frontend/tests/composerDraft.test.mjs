// Attaching a product image only stages a draft. No upload, no message, no provider.
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

function loadSource(name) {
  const { outputFiles } = buildSync({ entryPoints: [join(frontend, 'src', name)], absWorkingDir: frontend,
    bundle: true, platform: 'node', format: 'cjs', packages: 'external', jsx: 'automatic', loader: { '.css': 'empty' }, write: false });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputFiles[0].text)(requireFrontend, module, module.exports);
  return module.exports;
}

const { stageProductImages } = loadSource('composerDraft.ts');
const ComposerImageDraft = loadSource('ComposerImageDraft.tsx').default;
const file = (name, type, size) => ({ name, type, size, lastModified: 1 });

test('staging images does not submit and keeps only valid files', () => {
  const first = stageProductImages([], [file('蛋糕.jpg', 'image/jpeg', 1000), file('说明.txt', 'text/plain', 20)]);
  assert.equal(first.error, '请使用15 MB以内的JPG、PNG或WebP图片');
  assert.deepEqual(first.files.map(item => item.name), ['蛋糕.jpg']);
  assert.equal('submit' in first, false);
  const capped = stageProductImages(first.files, [
    file('侧面.png', 'image/png', 1000),
    file('细节.webp', 'image/webp', 1000),
    file('多余.jpg', 'image/jpeg', 1000),
  ]);
  assert.equal(capped.files.length, 3);
  assert.match(capped.error, /最多添加3张/);
  const huge = stageProductImages([], [file('太大.jpg', 'image/jpeg', 15 * 1024 * 1024 + 1)]);
  assert.deepEqual(huge.files, []);
  assert.match(huge.error, /15 MB/);
});

test('the draft preview can remove an image and has no send control', () => {
  const files = [file('蛋糕.jpg', 'image/jpeg', 1000), file('侧面.png', 'image/png', 800)];
  let remaining = files;
  const html = renderToStaticMarkup(React.createElement(ComposerImageDraft, {
    files, onRemove(index) { remaining = remaining.filter((_, item) => item !== index); },
  }));
  assert.match(html, /待发送的商品图片/);
  assert.match(html, /移除商品图 蛋糕\.jpg/);
  assert.match(html, /移除商品图 侧面\.png/);
  assert.doesNotMatch(html, /<form|type="submit"|重新确认需求/);
  remaining = remaining.filter(item => item.name !== '蛋糕.jpg');
  assert.deepEqual(remaining.map(item => item.name), ['侧面.png']);
});

test('the project composer attaches images without sending a turn', () => {
  const source = readFileSync(join(frontend, 'src', 'App.tsx'), 'utf8');
  assert.match(source, /stageProductImages/);
  assert.match(source, /stageIncoming/);
  assert.doesNotMatch(source, /重新确认需求/);
  assert.doesNotMatch(source, /acceptFiles/);
});
