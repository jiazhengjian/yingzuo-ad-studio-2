// Run from the project root: node --test tests/frontend_ad_form.mjs
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
const draftTools = loadSource('gateDraft.ts');
const ConversationGate = loadSource('ConversationGate.tsx').default;
const AdFormSummary = loadSource('AdFormSummary.tsx').default;
const { adFormOptionDescription } = loadSource('adForm.ts');
const formOptions = [
  { id: 'brand_tvc', label: '产品展示广告' },
  { id: 'scene_story', label: '剧情／场景广告' },
  { id: 'presenter_pitch', label: '人物讲解广告（口播）' },
  { id: 'auto', label: '根据商品推荐' },
];
const question = { id: 'ad_form', prompt: '这条广告更倾向哪种表达？', options: formOptions, allow_custom: true, allow_skip: false };
const gate = {
  id: 'gate-ad-form', project_id: 'project-test', job_id: 'job-test', kind: 'clarification',
  status: 'pending', message: '补充广告倾向', created_at: 1,
  questions: [question, { id: 'product_fact', prompt: '还有哪些已确认的商品信息？', options: [{ id: 'none', label: '暂时没有' }], allow_custom: true, allow_skip: true }],
  options: [], answers: [],
};
const gateMarkup = value => renderToStaticMarkup(React.createElement(ConversationGate, {
  gate: value, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
}));
const summaryMarkup = props => renderToStaticMarkup(React.createElement(AdFormSummary, props));

test('all four real ad-form choices survive reload alongside unfinished factual answers', () => {
  assert.equal(draftTools.questionOptions(question).length, 4, 'the recommendation choice must not be filtered as a fake custom answer');
  for (const option of formOptions) {
    const chosen = draftTools.answerCurrentQuestion(gate, draftTools.initialDraft(gate), { question_id: 'ad_form', option_id: option.id });
    const cached = { ...chosen, composerDrafts: { product_fact: '厂商说明书上写了两个档位' } };
    const restored = draftTools.restoreDraft(gate, JSON.stringify(cached));
    assert.equal(restored.answers.ad_form.option_id, option.id);
    assert.equal(restored.step, 1);
    assert.equal(restored.composerDrafts.product_fact, '厂商说明书上写了两个档位');
    const finished = draftTools.answerCurrentWithText(gate, restored, restored.composerDrafts.product_fact);
    assert.equal(finished.step, 2);
    assert.equal(finished.answers.ad_form.option_id, option.id);
    assert.equal(draftTools.answerLabel(question, finished.answers.ad_form), option.label);
  }
});

test('ad-form choice can be revised before submission without losing other answers', () => {
  const selected = draftTools.answerCurrentQuestion(gate, draftTools.initialDraft(gate), { question_id: 'ad_form', option_id: 'brand_tvc' });
  const complete = draftTools.answerCurrentQuestion(gate, selected, { question_id: 'product_fact', option_id: 'none' });
  const changed = draftTools.answerCurrentQuestion(gate, { ...complete, step: 0 }, { question_id: 'ad_form', option_id: 'scene_story' });
  assert.equal(changed.answers.ad_form.option_id, 'scene_story');
  assert.equal(changed.answers.product_fact.option_id, 'none');
  assert.equal(complete.answers.ad_form.option_id, 'brand_tvc');
});

test('custom form preference stays verbatim for server interpretation instead of frontend keyword guessing', () => {
  const text = '不要剧情，做产品展示广告';
  const draft = draftTools.answerCurrentWithText(gate, draftTools.initialDraft(gate), text);
  assert.deepEqual(draft.answers.ad_form, { question_id: 'ad_form', text });
  assert.equal(draftTools.restoreDraft(gate, JSON.stringify(draft)).answers.ad_form.text, text);
  assert.equal(draftTools.validAnswer(question, { question_id: 'ad_form', skipped: true }), false);
});

test('ad-form question explains the choices in the existing card with no second input or form', () => {
  const html = gateMarkup(gate);
  for (const option of formOptions) assert.ok(html.includes(option.label));
  assert.ok(html.includes('展示外观、功能或使用效果'));
  assert.ok(html.includes('人物处境、行动与产品参与后的变化'));
  assert.ok(html.includes('人物面向观众讲解、演示或分享产品'));
  assert.ok(html.includes('暂不指定主要表达方式'));
  assert.ok(!html.includes('<textarea') && !html.includes('<input') && !html.includes('<form'));
  assert.ok(!html.includes('已完成推荐') && !html.includes('稳定生成'));
  assert.equal(adFormOptionDescription('other_question', 'brand_tvc'), undefined, 'unrelated questions may reuse option IDs');
});

test('answered and superseded cards preserve the historical ad-form answer', () => {
  const legacyQuestion = { ...question, options: formOptions.map(option => option.id === 'presenter_pitch' ? { ...option, label: '人物口播广告' } : option) };
  const answered = { ...gate, questions: [legacyQuestion], status: 'answered', answers: [{ question_id: 'ad_form', option_id: 'presenter_pitch' }] };
  assert.ok(gateMarkup(answered).includes('人物口播广告'));
  const html = gateMarkup({ ...answered, status: 'superseded' });
  assert.ok(html.includes('此确认已由后续要求替代'));
  assert.ok(html.includes('人物口播广告'));
});

test('saved summary follows only persisted ad_form and keeps automatic preference distinct from a recommendation result', () => {
  assert.equal(summaryMarkup({}), '', 'legacy projects must not be silently assigned a form');
  assert.equal(summaryMarkup({ adForm: null }), '');
  assert.equal(summaryMarkup({ adForm: 'unrecognized' }), '');
  for (const option of formOptions) assert.ok(summaryMarkup({ adForm: option.id }).includes(option.label));
  const html = summaryMarkup({ adForm: 'auto' });
  assert.ok(html.includes('根据商品推荐'));
  assert.ok(!html.includes('TVC') && !html.includes('已推荐'));
});

test('saved preference offers only the shared-composer focus action when that route is available', () => {
  const editable = summaryMarkup({ adForm: 'scene_story', onChange: () => {} });
  assert.ok(editable.includes('aria-label="修改广告倾向"'));
  assert.ok(editable.includes('可在下方输入'));
  assert.ok(editable.includes('改成产品展示广告'));
  assert.ok(!editable.includes('TVC'));
  assert.ok(!editable.includes('<input') && !editable.includes('<form') && !editable.includes('<select'));
  const blocked = summaryMarkup({ adForm: 'scene_story' });
  assert.ok(blocked.includes('剧情／场景广告'));
  assert.ok(!blocked.includes('<button') && !blocked.includes('可在下方输入'));
});
