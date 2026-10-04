// Isolated UI/state tests. No service, provider, private data or environment files.
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
  const { outputFiles } = buildSync({ entryPoints: [join(frontend, 'src', name)], absWorkingDir: frontend,
    bundle: true, platform: 'node', format: 'cjs', packages: 'external', jsx: 'automatic',
    loader: { '.css': 'empty' }, write: false });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputFiles[0].text)(requireFrontend, module, module.exports);
  return module.exports;
}
const ConversationGate = loadSource('ConversationGate.tsx').default;
const RunTimeline = loadSource('RunTimeline.tsx').default;
const { initialDraft, restoreDraft, signatureOf } = loadSource('gateDraft.ts');
const { directionSelectRequest, directionSubmission, isCurrentDirectionGate, pendingConversationGate, validDirectionOptions } = loadSource('directionSelection.ts');
const { messageDesignAssets } = loadSource('designAssets.ts');
const { conversationItems } = loadSource('conversation.ts');

const project = { id: 'project-directions', revision: 0, context_version: 3, plan: null };
const direction = (id, recommended = false) => ({ id, label: `创意${id}`, angle: `角度${id}`, reason: `选择商品的理由${id}`,
  audience_value: `受众在意${id}：想在工作间隙享受片刻休息。`, product_expression: `产品呈现${id}：从原图可见的蛋糕层次展开，让商品成为下午茶的主角。`,
  story_summary: `故事${id}：办公桌旁，一个人从忙碌中抬头，拿起蛋糕，享受片刻休息。`, attraction_mechanism: `吸引方式${id}：用熟悉的下午茶情境引起共鸣。`,
  hook: `开场画面${id}`, development: `展开画面${id}`, payoff: `收束画面${id}`, audience: '办公室年轻人', scenario: '下午茶',
  evidence_ids: ['fact-1'], production_notes: [], rule_id: 'product-demo', recommended });
const gate = (overrides = {}) => ({ id: 'gate-directions', project_id: project.id, job_id: 'job-directions', kind: 'direction',
  status: 'pending', message: '请选一个方向', questions: [], answers: [], options: [direction('a'), direction('b', true), direction('c')],
  revision: 0, context_version: 3, created_at: 10, ...overrides });
const markup = (current = gate(), overrides = {}) => renderToStaticMarkup(React.createElement(ConversationGate, {
  gate: current, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
  onDirectionSelect: async () => {}, onDirectionModify: async () => {}, ...overrides,
}));
const choiceButtons = html => html.match(/<button[^>]*aria-label="选择方向：[^"]*"[^>]*>/g) ?? [];
const filmDirection = (id, structure = 'product_visual') => ({ ...direction(id),
  story_summary: `影片展开${id}：从风口到完整机身，产品与环绕它的气流意象一起进入画面。`,
  film_concept: { structure, key_visual: `核心视觉${id}：吹风机悬于暗色空间，光线沿机身移动。`,
    memory_anchor: `记忆画面${id}：环绕产品的风与发丝。`, sound_role: `声音职责${id}：旁白补充已确认功能与造型师的选择关注。` },
});

test('an ad concept shows the one-liner, the picture and the opening plus ending', () => {
  const production = { version: '1.0.0', product_constraints: [], characters: [], speech_mode: 'voiceover', speaker_id: '', voice_description: '自然旁白' };
  const option = { ...filmDirection('concept'), production,
    big_idea: '先看见一条轮廓', consumer_insight: '还没看清整只瓶子，就想知道它长什么样。', hook_type: 'macro_detail' };
  const html = markup(gate({ options: [option, { ...filmDirection('plain'), production }, { ...filmDirection('other'), production }] }));
  const start = html.indexOf('方向：创意concept');
  const main = html.indexOf('class="direction-film-main"', start);
  const summary = html.indexOf('class="production-summary"', main);
  const visible = html.slice(main, summary);
  for (const text of ['一句话创意', '先看见一条轮廓', '核心画面', option.film_concept.key_visual, '怎么开头、怎么收尾', `${option.hook}；${option.payoff}`]) {
    assert.ok(visible.includes(text), text);
  }
  for (const text of ['观众处境', '还没看清整只瓶子', '特写里的一个细节', option.consumer_insight, option.story_summary, option.angle]) {
    assert.ok(!visible.includes(text), text);
  }
  assert.equal((html.match(/<dt>一句话创意<\/dt>/g) ?? []).length, 1);
  assert.ok(!html.includes('direction-film-details'));
});

test('direction cards stay on the short pitch and keep the production line', () => {
  const production = { version: '1.0.0', product_constraints: [], characters: [], speech_mode: 'voiceover', speaker_id: '', voice_description: '自然旁白' };
  const options = ['product_visual', 'world_intercut', 'lifestyle'].map((structure, index) => ({
    ...filmDirection(String(index), structure), production, big_idea: `一句话${index}`,
  }));
  const html = markup(gate({ options }));
  for (const label of ['产品视觉呈现', '产品与场景交替', '生活方式', '主打什么', '创意展开', '怎样表达卖点', '剧情／场景', '人物讲解']) {
    assert.ok(!html.includes(label), label);
  }
  for (const label of ['一句话创意', '核心画面', '怎么开头、怎么收尾']) {
    assert.equal((html.match(new RegExp(`<dt>${label}</dt>`, 'g')) ?? []).length, 3);
  }
  for (const option of options) {
    const start = html.indexOf(`方向：${option.label}`);
    const main = html.indexOf('class="direction-film-main"', start);
    const summary = html.indexOf('class="production-summary"', main);
    assert.ok(start < main && main < summary);
    const visible = html.slice(main, summary);
    assert.ok(visible.includes(option.big_idea));
    assert.ok(visible.includes(option.film_concept.key_visual));
    assert.ok(visible.includes(`${option.hook}；${option.payoff}`));
    for (const text of [option.angle, option.story_summary, option.product_expression, option.audience_value,
      option.attraction_mechanism, option.film_concept.memory_anchor, option.film_concept.sound_role, option.reason, option.development]) {
      assert.ok(!visible.includes(text));
    }
  }
  assert.equal((html.match(/<details class="direction-film-details">/g) ?? []).length, 0);
  assert.equal((html.match(/aria-label="人物、声音与参考图安排"/g) ?? []).length, 3);
  assert.equal(choiceButtons(html).length, 3);
  assert.ok(choiceButtons(html).every(button => !button.includes('disabled')));
  assert.doesNotMatch(html, /<input|<textarea|<select|role="dialog"|scene_action|presenter_explanation|product_visual|film_concept/);
});

test('identical pitch copy appears once and blank lines do not create empty rows', () => {
  const repeated = '吹风机与环绕它的气流一起进入画面。';
  const one = { ...filmDirection('a'), big_idea: repeated, story_summary: repeated, product_expression: repeated,
    hook: '', payoff: '',
    film_concept: { structure: 'product_visual', key_visual: repeated, memory_anchor: repeated, sound_role: '' } };
  const html = markup(gate({ options: [one, direction('b'), direction('c')] }));
  assert.equal(html.split(repeated).length - 1, 1);
  assert.ok(!html.includes('direction-film-details'));
  assert.doesNotMatch(html, /<dd>\s*<\/dd>/);
  assert.equal(choiceButtons(html).length, 3);
});

test('older cards still show the short lines they already have and hide the essays', () => {
  const html = markup(gate({ options: [filmDirection('a'), direction('b'), { ...direction('c'), film_concept: null }] }));
  assert.ok(!html.includes('direction-structure'));
  assert.equal((html.match(/class="direction-film-main"/g) ?? []).length, 3);
  assert.ok(!html.includes('direction-product-value'));
  for (const id of ['b', 'c']) {
    assert.ok(html.includes(`开场画面${id}；收束画面${id}`));
    for (const text of [`故事${id}`, `受众在意${id}`, `产品呈现${id}`, `吸引方式${id}`, `角度${id}`]) assert.ok(!html.includes(text));
  }
  assert.ok(html.includes('核心视觉a'));
  assert.equal(choiceButtons(html).length, 3);
});

test('film concept choices retain exact selection context and invalidate drafts when the visual idea changes', () => {
  const current = gate({ options: [filmDirection('a'), filmDirection('b', 'world_intercut'), filmDirection('c', 'lifestyle')] });
  assert.deepEqual(directionSelectRequest(current, 'b', project, current.id, 'film-choice'), {
    gate_id: current.id, direction_id: 'b', expected_context_version: 3, accepted_revision: 0, client_turn_id: 'film-choice',
  });
  const stored = JSON.stringify({ ...initialDraft(current), note: '保留环绕机身的气流画面' });
  assert.equal(restoreDraft(current, stored).note, '保留环绕机身的气流画面');
  const changed = { ...current, options: current.options.map(option => ({ ...option,
    film_concept: { ...option.film_concept, key_visual: '新的视觉组织' },
  })) };
  assert.notEqual(signatureOf(changed), signatureOf(current));
  assert.equal(restoreDraft(changed, stored).note, '');
  for (const state of [{ busy: true }, { composerText: '换成产品与场景交替' }]) {
    assert.ok(choiceButtons(markup(current, state)).every(button => button.includes('disabled')));
  }
  const history = markup({ ...current, status: 'answered', selected_option_id: 'b' });
  assert.equal(choiceButtons(history).length, 0);
  assert.ok(history.includes('已选择：创意b'));
  assert.ok(history.includes('核心视觉b'));
});

test('three directions show the short pitch and a recommendation without auto-selecting', () => {
  const html = markup();
  assert.equal(choiceButtons(html).length, 3);
  assert.equal((html.match(/class="direction-recommended"/g) ?? []).length, 1);
  for (const id of ['a', 'b', 'c']) {
    assert.ok(html.includes(`创意${id}`));
    assert.ok(html.includes(`开场画面${id}；收束画面${id}`));
    assert.ok(html.replace(/<!--.*?-->/g, '').includes(`<h4>方向：创意${id}</h4>`));
    for (const text of [`角度${id}`, `受众在意${id}`, `产品呈现${id}`, `故事${id}`, `吸引方式${id}`, `选择商品的理由${id}`]) {
      assert.ok(!html.includes(text), text);
    }
  }
  assert.equal((html.match(/<dt>一句话创意<\/dt>/g) ?? []).length, 0);
  assert.equal((html.match(/<dt>怎么开头、怎么收尾<\/dt>/g) ?? []).length, 3);
  assert.ok(html.includes('选好后，再展开完整脚本与分镜'));
  assert.ok(!html.includes('创意依据'));
  assert.ok(!html.includes('<span>选择理由</span>'));
  assert.ok(!html.includes('is-selected'));
  assert.ok(!html.includes('direction-film-details'));
  assert.doesNotMatch(html, /<img|<video|<textarea|<input|role="dialog"/);
  assert.ok(choiceButtons(html).every(button => !button.includes('disabled')));
});

test('saved essays stay off the card while the opening and ending remain', () => {
  const long = { ...direction('a'), story_summary: '午后，白领从鼠标上移开手，拿起蛋糕；同事的目光随之转过来。'.repeat(8),
    attraction_mechanism: '从同事之间的目光和动作让观众好奇发生了什么。',
    reason: '依据原图里可见的轮廓和已经确认的办公室场景。'.repeat(8), production_notes: ['保持商品原图外观。'] };
  const html = markup(gate({ options: [long, direction('b'), direction('c')] }));
  assert.ok(!html.includes(long.story_summary));
  assert.ok(!html.includes(long.attraction_mechanism));
  assert.ok(html.includes('开场画面a；收束画面a'));
  assert.ok(!html.includes('is-compact'));
  assert.ok(!html.includes(long.reason));
  assert.ok(!html.includes('保持商品原图外观。'));
  assert.ok(!html.includes('制作说明'));
  assert.ok(!html.includes('创意依据'));
  assert.ok(!html.includes('direction-detail-content'));
  assert.ok(!html.includes('direction-details-toggle'));
  assert.ok(!html.includes('direction-film-details'));
  assert.equal(choiceButtons(html).length, 3, 'removing supporting details preserves the same explicit choices');
  assert.doesNotMatch(html, /<textarea|<input|role="dialog"/);
});

test('each direction keeps its title prefix and selection without extra details controls', () => {
  const html = markup(gate({ options: [direction('a'), { ...direction('b'), story_summary: '第一行\n第二行\n第三行\n第四行' }, direction('c')] }));
  assert.equal((html.match(/class="direction-card-actions"/g) ?? []).length, 3);
  assert.equal((html.replace(/<!--.*?-->/g, '').match(/<h4>方向：/g) ?? []).length, 3);
  assert.equal(choiceButtons(html).length, 3);
  assert.ok(!html.includes('direction-details-toggle'));
  assert.ok(!html.includes('aria-expanded'));
  assert.ok(!html.includes('第一行\n第二行\n第三行\n第四行'));
  assert.ok(html.includes('开场画面b；收束画面b'));
});

test('legacy directions show opening and ending without inventing a story label', () => {
  const legacy = { ...direction('a'), audience_value: undefined, product_expression: undefined, story_summary: undefined, attraction_mechanism: undefined };
  const blank = { ...direction('b'), audience_value: '  ', product_expression: '', story_summary: '  ', attraction_mechanism: '' };
  const olderStory = { ...direction('c'), audience_value: undefined, product_expression: undefined };
  const html = markup(gate({ options: [legacy, blank, olderStory] }));
  assert.ok(html.includes('开场画面a；收束画面a'));
  assert.ok(html.includes('开场画面b；收束画面b'));
  assert.ok(html.includes('开场画面c；收束画面c'));
  assert.doesNotMatch(html, /原画面设想|原方向说明|故事概要|主打什么|为什么在意|如何体现|direction-product-value|角度a/);
  assert.equal(choiceButtons(html).length, 3);
  assert.ok(choiceButtons(html).every(button => !button.includes('disabled')));
  assert.ok(!html.includes('人物、声音与参考图安排'), 'legacy cards must not infer cast or voice choices');
});

test('historical prose is not put back on the card when some value fields are missing', () => {
  const html = markup(gate({ options: [
    { ...direction('a'), audience_value: ' ', product_expression: '仅展示可核对的外观，不推导护发效果。', attraction_mechanism: '' },
    { ...direction('b'), product_expression: undefined },
    direction('c'),
  ] }));
  assert.ok(!html.includes('仅展示可核对的外观，不推导护发效果。'));
  assert.ok(!html.includes('角度a'));
  assert.ok(html.includes('开场画面a；收束画面a'));
  assert.doesNotMatch(html, /<dd>\s*<\/dd>|<dt>为什么在意<\/dt>|<dt>如何体现<\/dt>/);
  assert.equal(choiceButtons(html).length, 3, 'historical field gaps do not invalidate the choice');
});

test('each direction states its actual cast, speech and reference-image choice in one compact line', () => {
  const person = { id: 'worker', name: '白领', description: '办公装束' };
  const production = { version: '1.0.0', product_constraints: [], characters: [], speech_mode: 'voiceover', speaker_id: '', voice_description: '自然旁白' };
  const html = markup(gate({ options: [
    { ...direction('a'), production },
    { ...direction('b'), production: { ...production, characters: [person, { id: 'coworker', name: '同事', description: '休闲装束' }], speech_mode: 'character_dialogue', speaker_id: person.id } },
    { ...direction('c'), production: { ...production, characters: [person], speech_mode: 'none', voice_description: '' } },
  ] }));
  const text = html.replace(/<!--.*?-->/g, '');
  assert.ok(text.includes('无人出镜 · 画外旁白 · 1 张场景图'));
  assert.ok(text.includes('2 人出镜 · 白领说话 · 1 张场景图＋2 张角色四视图'));
  assert.ok(text.includes('1 人出镜 · 无台词 · 1 张场景图＋1 张角色四视图'));
  assert.equal((html.match(/class="production-summary"/g) ?? []).length, 3);
  assert.equal(choiceButtons(html).length, 3);
  assert.doesNotMatch(html, /<input|<select|role="dialog"/);
});

test('recommendation badges require explicit true, never array position or a string flag', () => {
  const noRecommendation = gate({ options: [direction('a'), direction('b'), { ...direction('c'), recommended: 'true' }] });
  assert.equal((markup(noRecommendation).match(/class="direction-recommended"/g) ?? []).length, 0);
});

test('busy and unsent composer changes prevent all direction clicks', () => {
  for (const state of [{ busy: true }, { composerText: '不要淋面特写，突出切块分享' }]) {
    const html = markup(gate(), state);
    assert.equal(choiceButtons(html).length, 3);
    assert.ok(choiceButtons(html).every(button => button.includes('disabled')));
  }
  assert.ok(markup(gate(), { composerText: '修改' }).includes('请先发送修改意见'));
});

test('a malformed direction set cannot submit, while the shared composer still accepts revisions', () => {
  for (const options of [[direction('a')], [direction('a'), direction('a'), direction('c')], [direction('a'), direction('b'), { ...direction('c'), label: '  ', hook: '', payoff: '', angle: '' }]]) {
    const current = gate({ options });
    assert.equal(validDirectionOptions(current), false);
    assert.equal(choiceButtons(markup(current)).length, 0);
    assert.throws(() => directionSelectRequest(current, 'a', project, current.id, 'client-1'));
  }
});

test('answered history records exactly the chosen direction and has no actionable selection controls', () => {
  const html = markup(gate({ status: 'answered', selected_option_id: 'a' }));
  assert.ok(html.includes('已选择：创意a'));
  assert.equal((html.match(/class="direction-chosen"/g) ?? []).length, 1);
  assert.equal((html.match(/is-selected/g) ?? []).length, 1);
  assert.equal(choiceButtons(html).length, 0);
  assert.ok(html.includes('开场画面a；收束画面a'));
  for (const text of ['受众在意a', '产品呈现a', '主打什么', '创意展开']) assert.ok(!html.includes(text));
  assert.doesNotMatch(html, /<details[^>]*\bopen[ =]/);
  const old = markup(gate({ status: 'superseded' }));
  assert.ok(old.includes('这组创意方向已更新'));
  assert.equal(choiceButtons(old).length, 0);
});

test('refresh restores free text but never a local direction choice, even if named current', () => {
  const current = gate({ options: [direction('current', true), direction('b'), direction('c')] });
  const draft = initialDraft(current);
  assert.equal(draft.selectedOptionId, '');
  const stored = JSON.stringify({ ...draft, selectedOptionId: 'current', note: '改成办公室分享，不要人物脸' });
  const restored = restoreDraft(current, stored);
  assert.equal(restored.selectedOptionId, '');
  assert.equal(restored.note, '改成办公室分享，不要人物脸');
  assert.equal(restoreDraft({ ...current, context_version: 4 }, stored).note, '');
  assert.notEqual(signatureOf(current), signatureOf({ ...current, options: current.options.map(option => ({ ...option, reason: '另一商品理由' })) }));
  for (const field of ['audience_value', 'product_expression']) {
    const changed = { ...current, options: current.options.map(option => ({ ...option, [field]: '新的产品价值表达' })) };
    assert.notEqual(signatureOf(current), signatureOf(changed));
    assert.equal(restoreDraft(changed, stored).note, '', 'changed selling-point content must not inherit a draft for the previous offer');
  }
  assert.equal(restoreDraft({ ...current, revision: 1 }, stored).note, '');
});

test('selection sends the exact gate and product context including initial revision zero', () => {
  const current = gate();
  assert.deepEqual(directionSelectRequest(current, 'b', project, current.id, 'client-1'), {
    gate_id: current.id, direction_id: 'b', expected_context_version: 3, accepted_revision: 0, client_turn_id: 'client-1',
  });
  assert.throws(() => directionSelectRequest(current, 'missing', project, current.id, 'client-1'));
});

test('stale, resolved and cross-project direction cards cannot affect newer candidates', () => {
  const current = gate();
  for (const changed of [{ ...project, context_version: 4 }, { ...project, revision: 1 }, { ...project, id: 'another-project' }, undefined]) {
    assert.equal(isCurrentDirectionGate(current, changed), false);
    assert.throws(() => directionSelectRequest(current, 'b', changed, current.id, 'client-1'));
  }
  for (const status of ['answered', 'superseded']) assert.throws(() => directionSelectRequest(gate({ status }), 'b', project, current.id, 'client-1'));
  assert.throws(() => directionSelectRequest(current, 'b', project, 'new-gate', 'client-1'));
  const latest = gate({ id: 'new-gate', created_at: 20 });
  assert.equal(pendingConversationGate([current, latest], project)?.id, latest.id);
  assert.equal(pendingConversationGate([latest, current], project)?.id, latest.id);
  assert.equal(pendingConversationGate([gate({ revision: 1 })], project), undefined);
  assert.equal(pendingConversationGate([current, { ...latest, context_version: 2 }], project), undefined, 'a stale newest offer cannot revive an older pending card');
});

test('double clicks share a synchronous submission lock; success cannot be submitted twice', async () => {
  const submission = directionSubmission();
  let count = 0, finish;
  const first = submission.run(async () => { count += 1; await new Promise(resolve => { finish = resolve; }); });
  assert.equal(submission.state(), 'submitting');
  await assert.rejects(submission.run(async () => { count += 1; }), /等待/);
  finish(); await first;
  assert.equal(submission.state(), 'submitted');
  await assert.rejects(submission.run(async () => { count += 1; }), /等待/);
  assert.equal(count, 1);
});

test('failed submission releases the click lock so the same gate can be retried', async () => {
  const submission = directionSubmission();
  await assert.rejects(submission.run(async () => { throw new Error('离线'); }), /离线/);
  assert.equal(submission.state(), 'idle');
  await submission.run(async () => {});
  assert.equal(submission.state(), 'submitted');
});

test('legacy image confirmation still selects current and does not become a creative-direction gate', () => {
  const legacy = gate({ kind: 'creative', options: [{ id: 'current', label: '原方案', description: '商品与场景图', segment: { reference_asset_ids: [] } }] });
  assert.equal(initialDraft(legacy).selectedOptionId, 'current');
  const html = markup(legacy);
  assert.ok(html.includes('图片没问题，生成视频'));
  assert.ok(!html.includes('先选一个广告方向'));
  assert.equal(choiceButtons(html).length, 0);
});

test('direction activity and historical conversation remain visible without a premature full plan or images', () => {
  const current = gate();
  const job = { id: current.job_id, project_id: project.id, kind: 'plan', status: 'awaiting_input', phase: 'awaiting_direction', revision: 0, progress: {}, created_at: 1, updated_at: 10 };
  const message = { id: 'direction-message', role: 'assistant', content: current.message, job_id: job.id, created_at: 9 };
  const detail = { project, gates: [current], jobs: [job], messages: [message], assets: [], renders: [], events: [] };
  const items = conversationItems(detail);
  assert.equal(items.filter(item => item.type === 'gate').length, 1);
  assert.equal(items.filter(item => item.type === 'plan').length, 0);
  assert.deepEqual(messageDesignAssets(detail, message), []);
  const html = renderToStaticMarkup(React.createElement(RunTimeline, { job, events: [] }));
  assert.ok(html.includes('等待选择广告方向'));
  assert.ok(!html.includes('等待检查图片'));
});
