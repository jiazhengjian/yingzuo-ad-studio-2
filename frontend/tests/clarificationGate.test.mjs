// Picking 剧情 on the same card hides the destination question and recounts the pager.
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

const draft = loadSource('gateDraft.ts');
const ConversationGate = loadSource('ConversationGate.tsx').default;
const memory = new Map();
globalThis.sessionStorage = {
  getItem: key => memory.get(key) ?? null,
  setItem: (key, value) => { memory.set(key, String(value)); },
  removeItem: key => { memory.delete(key); },
};

const option = (id, label) => ({ id, label });
const adForm = {
  id: 'ad_form', prompt: '这条广告主要采用哪种表达方式？', allow_custom: true, allow_skip: true,
  options: [option('brand_tvc', '产品展示广告'), option('scene_story', '剧情／场景广告'), option('presenter_pitch', '人物讲解广告（口播）'), option('auto', '根据商品推荐')],
};
const cta = {
  id: 'cta_target', prompt: '看完视频后，希望观众去哪里？', allow_custom: true, allow_skip: true,
  options: [option('link', '商品页 / 购买链接'), option('ai_decide', '让 AI 决定')],
};
const highlight = {
  id: 'highlight', prompt: '最想让观众记住这个商品的哪一点？例如外观、口味、夹心、制作工艺、适合的场合。',
  options: [], allow_custom: true, allow_skip: true,
};
const audience = {
  id: 'audience', prompt: '这条广告主要拍给谁看、在什么情境下使用？',
  options: [], allow_custom: true, allow_skip: true,
};
const gate = {
  id: 'gate-cake', project_id: 'p', job_id: 'j', kind: 'clarification', status: 'pending',
  message: '先确认', questions: [adForm, highlight, audience, cta], answers: [], options: [], created_at: 1,
};

function choose(optionId) {
  const initial = draft.initialDraft(gate);
  return draft.answerCurrentQuestion(gate, initial, { question_id: 'ad_form', option_id: optionId });
}

test('剧情 hides the destination and leaves highlight as question 2 of 3', () => {
  const next = choose('scene_story');
  const visible = draft.visibleQuestions(gate, next.answers);
  assert.deepEqual(visible.map(item => item.id), ['ad_form', 'highlight', 'audience']);
  assert.equal(visible[next.step].id, 'highlight');
  assert.equal(next.step + 1, 2);
  assert.equal(visible.length, 3);
  const submitted = draft.submitAnswers(gate, { ...next, answers: {
    ...next.answers, highlight: { question_id: 'highlight', skipped: true },
  }});
  assert.deepEqual(submitted.find(item => item.question_id === 'cta_target'), { question_id: 'cta_target', skipped: true });
  assert.equal(submitted.find(item => item.question_id === 'ad_form').option_id, 'scene_story');
});

test('产品展示、口播 and 根据商品推荐 still show the destination', () => {
  for (const optionId of ['brand_tvc', 'presenter_pitch', 'auto']) {
    const next = choose(optionId);
    const visible = draft.visibleQuestions(gate, next.answers);
    assert.deepEqual(visible.map(item => item.id), ['ad_form', 'highlight', 'audience', 'cta_target']);
    assert.equal(visible[next.step].id, 'highlight');
    assert.equal(visible.length, 4);
  }
  const typed = draft.answerCurrentQuestion(gate, draft.initialDraft(gate), { question_id: 'ad_form', text: '剧情／场景广告' });
  assert.deepEqual(draft.visibleQuestions(gate, typed.answers).map(item => item.id), ['ad_form', 'highlight', 'audience']);
});

test('the card recounts live once 剧情 is the saved answer', () => {
  const answered = choose('scene_story');
  memory.set(draft.draftKey(gate.id), JSON.stringify({ ...answered, signature: draft.signatureOf(gate) }));
  const story = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
  }));
  assert.match(story, /最想让观众记住/);
  assert.match(story, /2 \/ 3/);
  assert.doesNotMatch(story, /看完视频后/);
  memory.set(draft.draftKey(gate.id), JSON.stringify({ ...choose('brand_tvc'), signature: draft.signatureOf(gate) }));
  const product = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
  }));
  assert.match(product, /最想让观众记住/);
  assert.match(product, /2 \/ 4/);
});

test('a confirmed option shows its label when the stored text is null', () => {
  const answered = {
    ...gate,
    status: 'answered',
    questions: [
      adForm,
      { id: 'audience_use', prompt: '主要给谁看？', allow_custom: true, allow_skip: true,
        options: [option('styling', '造型')] },
    ],
    answers: [
      { question_id: 'ad_form', option_id: 'brand_tvc', text: null },
      { question_id: 'audience_use', option_id: 'styling', text: null },
    ],
  };
  const html = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate: answered, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
  }));
  assert.match(html, /选项已确认/);
  assert.match(html, /产品展示广告/);
  assert.match(html, /造型/);
  assert.equal(draft.answerLabel(adForm, answered.answers[0]), '产品展示广告');
  assert.equal(draft.answerLabel(answered.questions[1], answered.answers[1]), '造型');
  const written = { ...answered.answers[1], text: '送给妈妈' };
  assert.equal(draft.answerLabel(answered.questions[1], written), '送给妈妈');
});

test('reopening fills the original card and does not ask which part', () => {
  memory.clear();
  const reopened = {
    ...gate,
    id: 'gate-reopen',
    message: '先确认广告怎么拍、最想让观众记住的一点、拍给谁看、在什么情境下使用、看完去哪里，再继续准备方案。',
    answers: [
      { question_id: 'ad_form', option_id: 'brand_tvc' },
      { question_id: 'highlight', text: '瓶身透亮' },
      { question_id: 'audience', text: '上班族通勤' },
      { question_id: 'cta_target', option_id: 'link' },
    ],
  };
  const seeded = draft.initialDraft(reopened);
  assert.equal(seeded.step, 0);
  assert.equal(seeded.answers.ad_form.option_id, 'brand_tvc');
  assert.equal(seeded.answers.highlight.text, '瓶身透亮');
  assert.equal(seeded.answers.audience.text, '上班族通勤');
  assert.equal(seeded.answers.cta_target.option_id, 'link');
  const html = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate: reopened, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
  }));
  assert.match(html, /产品展示广告/);
  assert.match(html, /aria-pressed="true"/);
  assert.doesNotMatch(html, /这次想改的是哪一部分|哪一部分|请修改|没通过|硬门/);
  const story = {
    ...reopened,
    id: 'gate-story',
    answers: [
      { question_id: 'ad_form', option_id: 'scene_story' },
      { question_id: 'highlight', text: '瓶身透亮' },
      { question_id: 'audience', text: '上班族通勤' },
      { question_id: 'cta_target', option_id: 'link' },
    ],
  };
  assert.deepEqual(draft.visibleQuestions(story, draft.initialDraft(story).answers).map(item => item.id), ['ad_form', 'highlight', 'audience']);
  const storyHtml = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate: story, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
  }));
  assert.doesNotMatch(storyHtml, /看完视频后|这次想改的是哪一部分|请修改|没通过|硬门/);
});
