// Run from the project root: node --test tests/frontend_strategy.mjs
// Uses existing frontend dependencies; no browser, API, or provider calls.
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
const strategy = loadSource('strategy.ts');
const { creativeSummary, planningNotes, planningMessageText } = loadSource('planningDisplay.ts');
const { PlanStrategySummary } = loadSource('App.tsx');
const ProductionSummary = loadSource('ProductionSummary.tsx').default;
const ConversationGate = loadSource('ConversationGate.tsx').default;

function fixture() {
  return {
    title: '商品广告', script: '看得见的真实细节。', language: 'zh-CN', aspect_ratio: '9:16', audio_strategy: 'native',
    facts: { name: '收纳袋', claims: ['可折叠'] },
    segments: [{ id: 'segment1', duration_seconds: 10, purpose: '展示', prompt: '展示商品', narration: '看得见的真实细节。', reference_asset_ids: ['product'], shots: [{ start: 0, end: 10, description: '展开商品', narration: '看得见的真实细节。', purpose: '展示商品细节', evidence_ids: ['fact1'] }] }],
    strategy: {
      version: '1.0', brief: { product_name: '收纳袋', category: '家居', audience: '租房人群', scenario: '整理衣物', facts: [{ id: 'fact1', text: '可折叠', source_kind: 'merchant', source_id: 'brief' }], unknowns: ['尺寸待确认'], assumptions: [] },
      directions: [
        { id: 'unused', rule_id: 'rule-b', label: '未选方向', audience: '', scenario: '', angle: '', hook: '从旅行打包开始', development: '', payoff: '', evidence_ids: [], reason: '不能默认展示这个方向', production_notes: [] },
        { id: 'chosen', rule_id: 'rule-a', label: '先看整理前后的变化', audience: '租房人群', scenario: '整理衣物', angle: '可视化收纳', hook: '一桌衣物', development: '展示展开和装入', payoff: '整齐收纳', evidence_ids: ['fact1'], reason: '从整理衣物的实际场景切入，让可折叠的特点更直观。请核对商品外观。', production_notes: ['保留原图'] },
      ], selected_direction_id: 'chosen', rules_used: [{ id: 'rule-a', version: '1', sources: ['https://example.com/official'] }],
    },
  };
}
const markup = plan => renderToStaticMarkup(React.createElement(PlanStrategySummary, { plan }));

test('legacy or null strategy adds no strategy UI and never invents a recommendation', () => {
  const plan = fixture();
  delete plan.strategy;
  assert.equal(strategy.selectedStrategyDirection(plan), null);
  assert.equal(markup(plan), '');
  plan.strategy = null;
  assert.equal(markup(plan), '');
  plan.strategy = { ...fixture().strategy, selected_direction_id: 'missing' };
  assert.equal(markup(plan), '');
});

test('the confirmed plan presents only its saved production decision and preserves it during edits', () => {
  const plan = fixture();
  const renderProduction = () => renderToStaticMarkup(React.createElement(ProductionSummary, { production: plan.production })).replace(/<!--.*?-->/g, '');
  assert.equal(renderProduction(), '', 'an old plan cannot infer people or voice from narrative prose');
  plan.production = { version: '1.0.0', product_constraints: [{ text: '商品可折叠', evidence_ids: ['fact1'] }],
    characters: [{ id: 'host', name: '整理者', description: '日常居家穿着' }], speech_mode: 'character_dialogue', speaker_id: 'host', voice_description: '自然放松' };
  assert.ok(renderProduction().includes('1 人出镜 · 整理者说话 · 1 张场景图＋1 张角色四视图'));
  const edited = strategy.clonePlanForEditing(plan);
  assert.deepEqual(edited.production, plan.production);
  edited.production.characters[0].description = '调整后的外观';
  assert.equal(plan.production.characters[0].description, '日常居家穿着');
});

test('selected product value survives a plan edit without mutating the saved direction', () => {
  const plan = fixture();
  const direction = strategy.selectedStrategyDirection(plan);
  direction.audience_value = '换季不用时，希望收纳用品也容易归置。';
  direction.product_expression = '物品取出后折起收纳袋，呈现已确认的可折叠特点。';
  const edited = strategy.clonePlanForEditing(plan);
  const selected = strategy.selectedStrategyDirection(edited);
  assert.equal(selected.audience_value, direction.audience_value);
  assert.equal(selected.product_expression, direction.product_expression);
  selected.product_expression = '后续修改要求';
  assert.equal(direction.product_expression, '物品取出后折起收纳袋，呈现已确认的可折叠特点。');
});

test('saved partial appearance and one speaker continuation are displayed and retained in edit copies', () => {
  const plan = fixture();
  plan.production = { version: '1.0.0', product_constraints: [],
    characters: [{ id: 'char_model', name: '模特', description: '只露后脑及头发', appearance: 'partial_only' }],
    speech_mode: 'voiceover', speaker_id: '', voice_description: '自然普通话' };
  const render = () => renderToStaticMarkup(React.createElement(ProductionSummary, { production: plan.production })).replace(/<!--.*?-->/g, '');
  assert.ok(render().includes('1 人局部出镜'));
  assert.ok(!render().includes('无人出镜'));
  plan.production.characters[0].appearance = 'face_visible';
  Object.assign(plan.production, { speech_mode: 'character_dialogue', speaker_id: 'char_model', speech_delivery: 'on_and_off_screen' });
  assert.ok(render().includes('模特说话（含画外延续）'));
  assert.deepEqual(strategy.clonePlanForEditing(plan).production, plan.production);
  delete plan.production.speech_delivery;
  assert.ok(!render().includes('画外延续'), 'legacy choice must not acquire a capability it never selected');
});

test('creative summary shows audience, setting and the chosen story without rationale, full dialogue or duplicate actions', () => {
  const plan = fixture();
  assert.equal(strategy.selectedStrategyDirection(plan).id, 'chosen');
  const html = markup(plan);
  assert.ok(!html.includes('<details'));
  assert.ok(!html.includes('<button'));
  for (const visible of ['创意摘要', '面向租房人群', '场景设定为整理衣物', '一桌衣物', '展示展开和装入', '整齐收纳']) assert.ok(html.includes(visible));
  assert.ok(!html.includes('从整理衣物的实际场景切入，让可折叠的特点更直观。'));
  assert.ok(!html.includes('换个思路'));
  for (const hidden of ['未选方向', '从旅行打包开始', 'rule-a', 'fact1', '尺寸待确认', 'production_notes', plan.script]) assert.ok(!html.includes(hidden));
  assert.equal(Array.from(strategy.shortDirectionReason('字'.repeat(120))).length, 72);
});

test('changing projects reads that project’s selection and does not retain a previous recommendation', () => {
  const first = fixture(), next = fixture();
  next.strategy.selected_direction_id = 'unused';
  assert.ok(markup(first).includes('一桌衣物'));
  assert.ok(!markup(next).includes('一桌衣物'));
  assert.ok(markup(next).includes('从旅行打包开始'));
  assert.equal(first.strategy.selected_direction_id, 'chosen');
});

test('reviewed content summary supersedes preliminary direction without exposing full speech or review notes', () => {
  const plan = fixture();
  plan.ad_script = {
    skill_version: '1.0.0', core_message: '换季整理，用完就折起来。',
    audience_takeaway: '观众记住折叠收纳的用途', product_role: '收纳袋承担整理任务', evidence_ids: ['fact1'],
    beats: [{ id: 'b1', purpose: '说明用途', message: '闲置时可折起', visual_role: '衣物归位后，把收纳袋折好。', narration: '用完一折，收起来。', evidence_ids: ['fact1'] }],
  };
  plan.script_review = { script: structuredClone(plan.ad_script), ready: true, checks: [{ note: '内部短结论不出现在卡片上' }] };
  const html = markup(plan);
  assert.ok(html.includes('换季整理，用完就折起来。'));
  assert.ok(html.includes('衣物归位后，把收纳袋折好。'));
  assert.ok(!html.includes('一桌衣物'));
  assert.ok(!html.includes('用完一折，收起来。'));
  assert.ok(!html.includes('内部短结论'));
});

test('direct merchant narration edit invalidates prior content review while retaining facts and shot evidence', () => {
  const editing = loadSource('planEditing.ts');
  const plan = fixture();
  plan.segments[0].shots[0].beat_id = 'b1';
  plan.ad_script = { skill_version: '1.0.0', beats: [{ id: 'b1', narration: plan.script }] };
  plan.script_review = { script: structuredClone(plan.ad_script), ready: true, checks: [] };
  const before = structuredClone(plan);
  const edited = editing.updateShotNarration(plan, 0, 0, '用完折好，收进柜子。');
  assert.equal(edited.ad_script, null);
  assert.equal(edited.script_review, null);
  assert.equal(edited.script, '用完折好，收进柜子。');
  assert.deepEqual(edited.strategy, plan.strategy);
  assert.deepEqual(edited.facts, plan.facts);
  assert.deepEqual(edited.segments[0].shots[0].evidence_ids, ['fact1']);
  assert.deepEqual(plan, before);
});

test('creative summary uses confirmed context instead of alternative audience assumptions and does not truncate the story', () => {
  const plan = fixture();
  plan.strategy.directions[1].audience = '未经确认的受众假设';
  plan.strategy.directions[1].scenario = '未经确认的场景假设';
  plan.strategy.directions[1].development = '展示展开和装入，'.repeat(40) + '最后展示完整容量';
  const saved = structuredClone(plan), summary = creativeSummary(plan);
  assert.ok(summary.includes('面向租房人群，场景设定为整理衣物。'));
  assert.ok(!summary.includes('未经确认'));
  assert.ok(summary.includes('最后展示完整容量。整齐收纳。'));
  assert.deepEqual(plan, saved);
});

function planningFixture() {
  const job = { id: 'plan1', project_id: 'project1', kind: 'plan', status: 'ready', phase: 'plan_ready', revision: 1, updated_at: 1 };
  const message = { id: 'message1', role: 'assistant', job_id: job.id, content: '已保存的公开策划说明。'.repeat(15) };
  return { project: { id: 'project1', revision: 2, plan: fixture() }, jobs: [job], messages: [message] };
}

test('long public planning prose moves into its own run, with a concise completion message and no data mutation', () => {
  const detail = planningFixture(), saved = structuredClone(detail);
  assert.equal(planningNotes(detail, detail.jobs[0])[0].content, detail.messages[0].content);
  assert.equal(planningMessageText(detail, detail.messages[0]), '广告方案已整理好，创意摘要和分镜见下方。');
  assert.deepEqual(detail, saved);
  detail.messages[0].content = '简短的策划反馈。';
  assert.deepEqual(planningNotes(detail, detail.jobs[0]), []);
  assert.equal(planningMessageText(detail, detail.messages[0]), detail.messages[0].content);
});

test('clarification, failures, generation and user messages are never shortened as planning prose', () => {
  for (const patch of [{ kind: 'generate' }, { phase: 'clarification' }, { status: 'failed' }, { project_id: 'other-project' }]) {
    const detail = planningFixture();
    Object.assign(detail.jobs[0], patch);
    assert.deepEqual(planningNotes(detail, detail.jobs[0]), []);
    assert.equal(planningMessageText(detail, detail.messages[0]), detail.messages[0].content);
  }
  const detail = planningFixture();
  detail.messages[0].role = 'user';
  assert.deepEqual(planningNotes(detail, detail.jobs[0]), []);
  assert.equal(planningMessageText(detail, detail.messages[0]), detail.messages[0].content);
});

test('historical explanation stays with its own run and is never reconstructed from the mutable current plan', () => {
  const detail = planningFixture(), oldJob = detail.jobs[0];
  const nextJob = { ...oldJob, id: 'plan2', revision: 2, updated_at: 2 };
  detail.jobs.push(nextJob);
  assert.deepEqual(planningNotes(detail, nextJob), []);
  assert.equal(planningNotes(detail, oldJob)[0].content, detail.messages[0].content);
  assert.ok(!planningMessageText(detail, detail.messages[0]).includes('分镜见下方'));
  detail.project.plan.strategy.directions[1].reason = '后来改过的理由';
  assert.ok(!JSON.stringify(planningNotes(detail, oldJob)).includes('后来改过的理由'));
});

test('change-direction draft preserves existing preferences and is idempotent without a submission', () => {
  const direction = strategy.selectedStrategyDirection(fixture());
  const draft = strategy.directionChangeDraft('希望改成旅行场景', direction);
  assert.ok(draft.startsWith('希望改成旅行场景\n\n'));
  assert.ok(draft.includes('避开当前「先看整理前后的变化」'));
  assert.ok(draft.includes('保留已确认的商品事实和时长'));
  assert.ok(draft.includes('先更新广告方案供我确认'));
  assert.equal(strategy.directionChangeDraft(draft, direction), draft);
});

test('editing narration preserves strategy and shot evidence with an independent deep copy', () => {
  const plan = fixture(), saved = structuredClone(plan);
  const edited = strategy.clonePlanForEditing(plan);
  assert.deepEqual(edited, saved);
  edited.script = '修改后的台词。';
  edited.segments[0].shots[0].narration = edited.script;
  assert.deepEqual(edited.strategy, saved.strategy);
  assert.equal(edited.segments[0].shots[0].purpose, '展示商品细节');
  assert.deepEqual(edited.segments[0].shots[0].evidence_ids, ['fact1']);
  edited.strategy.brief.facts[0].text = '编辑副本';
  edited.segments[0].shots[0].evidence_ids.push('new');
  assert.deepEqual(plan, saved);
  delete plan.strategy;
  const legacy = strategy.clonePlanForEditing(plan);
  assert.ok(!Object.hasOwn(legacy, 'strategy'), 'legacy edit must not synthesize strategy fields');
});

test('a direction card lets a merchant picture the ad, including why people stop', () => {
  const production = { version: '1.0.0', product_constraints: [],
    characters: [{ id: 'birthday', name: '寿星', description: '成年' }, { id: 'friend', name: '朋友', description: '成年' }],
    speech_mode: 'character_dialogue', speaker_id: 'birthday', voice_description: '自然' };
  const cake = {
    id: 'cake', label: '先别切，等这勺巧克力落下来', rule_id: 'contrast_humor', hook_type: 'situational_conflict', recommended: true,
    angle: '不应该出现的主打说明', audience_value: '不应该出现的受众价值', story_summary: '不应该出现的故事概要',
    attraction_mechanism: '不应该出现的吸引方式', big_idea: '不应该出现的一句话创意', development: '不应该出现的展开',
    product_expression: '把丝滑巧克力和停住的叉子绑在一起：酱还在滴，人先不动。',
    consumer_insight: '切下去之前，总想再多看它一会儿。',
    hook: '寿星按住朋友的手，樱桃还对着镜头。',
    payoff: '第一块推到寿星面前，樱桃留在最上面。',
    reason: '停一下的动作，让人想看酱到底落不落。',
    film_concept: { structure: 'scene_action', key_visual: '叉子停在盘边，巧克力酱慢慢滴落。',
      memory_anchor: '不应该出现的记忆点', sound_role: '不应该出现的声音任务' },
    production, audience: '', scenario: '', evidence_ids: ['fact1'], production_notes: ['保持蛋糕原图外观。'],
  };
  const other = (id) => ({ ...cake, id, label: `备用${id}`, recommended: false, rule_id: 'not_a_rule', hook_type: 'not_a_hook',
    product_expression: `备用核心${id}`, consumer_insight: '', hook: `备用开头${id}`, payoff: `备用结尾${id}`, reason: `备用理由${id}`,
    film_concept: { structure: 'scene_action', key_visual: `备用中段${id}` } });
  const current = { id: 'gate-cake', project_id: 'project-cake', job_id: 'job-cake', kind: 'direction', status: 'pending',
    message: '请选一个方向', questions: [], answers: [], options: [cake, other('b'), other('c')],
    revision: 0, context_version: 3, created_at: 10 };
  const html = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate: current, busy: false, onSubmit: async () => {}, onCreativeSubmit: async () => {},
    onDirectionSelect: async () => {}, onDirectionModify: async () => {},
  }));
  const start = html.indexOf('方向：先别切，等这勺巧克力落下来');
  const card = html.slice(start, html.indexOf('方向：备用b'));
  assert.ok(card.includes('反差幽默 · 小冲突开场'));
  assert.ok(card.includes('class="direction-recommended"'));
  const labels = ['核心创意', '洞察', '0–3秒', '中段', '结尾', '为什么吸引人'];
  let cursor = 0;
  for (const label of labels) {
    const at = card.indexOf(`<dt>${label}</dt>`, cursor);
    assert.ok(at > cursor, label);
    cursor = at;
  }
  for (const text of [cake.product_expression, cake.consumer_insight, cake.hook, cake.film_concept.key_visual, cake.payoff, cake.reason,
    '2 人出镜 · 寿星说话']) {
    assert.ok(card.includes(text), text);
  }
  for (const hidden of ['一句话创意', '为什么能打', '核心画面', '怎么开头', cake.big_idea, cake.angle, cake.audience_value,
    cake.story_summary, cake.attraction_mechanism, cake.development, cake.film_concept.memory_anchor, cake.film_concept.sound_role,
    'scene_action', 'contrast_humor', 'situational_conflict', 'not_a_rule', 'not_a_hook', '保持蛋糕原图外观。']) {
    assert.ok(!card.includes(hidden), hidden);
  }
  assert.ok(!html.includes('not_a_rule'));
  assert.ok(!html.includes('not_a_hook'));
  assert.ok(!html.includes('direction-film-details'));
  const long = { ...cake, id: 'long', reason: '停一下，就想看这勺酱到底落不落。'.repeat(4),
    product_expression: '把丝滑巧克力和停住的叉子绑在一起：酱还在滴，人先不动。这句后面是多出来的解释，商家卡片上不需要看完这一整段，屏幕上也放不下。' };
  const longHtml = renderToStaticMarkup(React.createElement(ConversationGate, {
    gate: { ...current, options: [long, other('b'), other('c')] }, busy: false,
    onSubmit: async () => {}, onCreativeSubmit: async () => {}, onDirectionSelect: async () => {}, onDirectionModify: async () => {},
  }));
  assert.ok(!longHtml.includes(long.reason));
  assert.ok(!longHtml.includes(long.product_expression));
  assert.ok(longHtml.includes('停一下，就想看这勺酱到底落不落。停一下，就想看这勺酱到底落不落。'));
  assert.ok(longHtml.includes('把丝滑巧克力和停住的叉子绑在一起：酱还在滴，人先不动。'));
});
