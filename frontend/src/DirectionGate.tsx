import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowRight, Check, CheckCircle2, ChevronDown, Circle, LoaderCircle, TriangleAlert } from 'lucide-react';
import type { DirectionGateData, DirectionOption, FilmConcept, FilmStructure } from './api';
import type { ConversationGateHandle, ConversationGateProps } from './ConversationGate';
import { draftKey, initialDraft, restoreDraft } from './gateDraft';
import { directionSubmission, validDirectionOptions } from './directionSelection';
import ProductionSummary from './ProductionSummary';

type Props = Omit<ConversationGateProps, 'gate'> & { gate: DirectionGateData };

const hookLabels: Record<string, string> = {
  macro_detail: '特写里的一个细节',
  silhouette: '先看到轮廓',
  signature_sound: '先听到一声',
  scale_shift: '大小突然变了',
  awkward_moment: '一个尴尬瞬间',
  situational_conflict: '当场卡住',
  arresting_line: '先说一句',
  direct_question: '先问一句',
  counterintuitive: '和想的不一样',
  result_first: '先给结果',
};

const filmStructureLabels: Record<FilmStructure, string> = {
  product_visual: '产品视觉呈现',
  world_intercut: '产品与场景交替',
  lifestyle: '生活方式',
  scene_action: '剧情／场景',
  presenter_explanation: '人物讲解',
};

function distinctRows(rows: [string, string | undefined][], seen = new Set<string>()) {
  return rows.flatMap(([label, value]) => {
    const text = value?.trim();
    const key = text?.replace(/\s+/g, ' ');
    if (!text || !key || seen.has(key)) return [];
    seen.add(key);
    return [{ label, text }];
  });
}

function FilmConceptContent({ option, concept }: { option: DirectionOption; concept: FilmConcept }) {
  const seen = new Set<string>();
  const mainRows = distinctRows([
    ['一句话', option.big_idea],
    ['观众处境', option.consumer_insight],
    ['开头', option.hook_type ? hookLabels[option.hook_type] : undefined],
    ['主打什么', option.angle],
    ['核心画面', concept.key_visual],
    ['创意展开', option.story_summary],
    ['怎样表达卖点', option.product_expression],
  ], seen);
  const details = distinctRows([
    ['受众为什么在意', option.audience_value],
    ['观看理由', option.attraction_mechanism],
    ['留下的印象', concept.memory_anchor],
    ['声音怎样配合', concept.sound_role],
  ], seen);
  return <div className="direction-film-concept">
    <span className="direction-structure"><span className="gate-sr-only">影片结构：</span>{filmStructureLabels[concept.structure]}</span>
    <dl className="direction-film-main">{mainRows.map(({ label, text }) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}</dl>
    {details.length > 0 && <details className="direction-film-details">
      <summary><span>受众、记忆点与声音</span><ChevronDown size={13}/></summary>
      <dl>{details.map(({ label, text }) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}</dl>
    </details>}
  </div>;
}

function OptionContent({ option, selected, action }: { option: DirectionOption; selected?: boolean; action?: ReactNode }) {
  const story = option.story_summary?.trim();
  const attraction = option.attraction_mechanism?.trim();
  const audienceValue = option.audience_value?.trim();
  const productExpression = option.product_expression?.trim();
  const hasProductValue = !!(audienceValue || productExpression);
  const filmConcept = option.film_concept;
  const hasFilmConcept = filmConcept && filmStructureLabels[filmConcept.structure];
  const legacyStory = [option.hook, option.development, option.payoff].filter(Boolean).join(' ');
  return <>
    <div className="direction-copy">
    <div className="direction-card-heading"><h4>方向：{option.label}</h4><span className="direction-badges">
      {selected && <span className="direction-chosen"><Check size={12}/>已选择</span>}
      {option.recommended === true && <span className="direction-recommended">推荐</span>}
    </span></div>
    {hasFilmConcept ? <FilmConceptContent option={option} concept={filmConcept}/> : <>
    {hasProductValue && <dl className="direction-product-value">
      {option.angle.trim() && <div><dt>主打什么</dt><dd>{option.angle}</dd></div>}
      {audienceValue && <div><dt>为什么在意</dt><dd>{audienceValue}</dd></div>}
      {productExpression && <div><dt>如何体现</dt><dd>{productExpression}</dd></div>}
    </dl>}
    {(story || legacyStory) && <p className="direction-story"><span>{story ? (hasProductValue ? '创意展开' : '故事概要') : '原画面设想'}</span>{story || legacyStory}</p>}
    {(attraction || !hasProductValue) && <p className="direction-attraction"><span>{attraction ? '怎样吸引观众' : '原方向说明'}</span>{attraction || option.angle}</p>}
    </>}
    <ProductionSummary production={option.production}/>
    </div>
    {action && <div className="direction-card-actions">{action}</div>}
  </>;
}

const DirectionGate = forwardRef<ConversationGateHandle, Props>(function DirectionGate(props, ref) {
  const { gate } = props;
  if (gate.status !== 'pending') {
    const superseded = gate.status === 'superseded';
    const chosen = gate.options.find(option => option.id === gate.selected_option_id);
    return <details className={`conversation-gate gate-resolved direction-history ${superseded ? 'gate-superseded' : ''}`}>
      <summary><span>{superseded ? <Circle size={15}/> : <CheckCircle2 size={15}/>}{superseded ? '这组创意方向已更新' : chosen ? `已选择：${chosen.label}` : '创意方向已确认'}</span><ChevronDown size={15}/></summary>
      <div className="gate-resolved-body"><p>{superseded ? '请从最新方向卡选择，这组仅作记录。' : '后续脚本与分镜将围绕这个方向展开。'}</p>
        <div className="direction-options">{gate.options.map(option => <article key={option.id} className={`direction-card ${chosen?.id === option.id ? 'is-selected' : ''}`}><OptionContent option={option} selected={chosen?.id === option.id}/></article>)}</div>
      </div>
    </details>;
  }
  return <PendingDirectionGate {...props} ref={ref}/>;
});
export default DirectionGate;

const PendingDirectionGate = forwardRef<ConversationGateHandle, Props>(function PendingDirectionGate({ gate, busy, error, composerText = '', onComposerStateChange, onDirectionSelect, onDirectionModify }, ref) {
  const [draft, setDraft] = useState(() => {
    try { return restoreDraft(gate, sessionStorage.getItem(draftKey(gate.id))); }
    catch { return initialDraft(gate); }
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submittingId, setSubmittingId] = useState('');
  const [localError, setLocalError] = useState('');
  const [storageUnavailable, setStorageUnavailable] = useState(false);
  const lock = useRef(directionSubmission());
  const locked = busy || submitting || submitted;
  const hasDraft = !!composerText.trim();
  const valid = validDirectionOptions(gate);

  useEffect(() => {
    if (submitted) return;
    try { sessionStorage.setItem(draftKey(gate.id), JSON.stringify(draft)); setStorageUnavailable(false); }
    catch { setStorageUnavailable(true); }
  }, [draft, gate.id, submitted]);
  useEffect(() => {
    onComposerStateChange?.({ gateId: gate.id, mode: 'direction', prompt: '想换个思路？在这里补充或修改…', canInput: !submitted, maxLength: 1000, initialText: draft.note });
  }, [gate.id, submitted, draft.note, onComposerStateChange]);

  async function perform(action: () => Promise<void>, optionId = '') {
    if (locked || lock.current.state() !== 'idle') throw new Error('请等待当前操作完成。');
    setSubmitting(true); setSubmittingId(optionId); setLocalError('');
    try {
      await lock.current.run(action);
      setSubmitted(true);
      try { sessionStorage.removeItem(draftKey(gate.id)); } catch { /* Draft storage is optional. */ }
    } catch (caught) {
      setLocalError(caught instanceof Error ? caught.message : '提交未完成，请重试。');
      throw caught;
    } finally { setSubmitting(false); }
  }
  async function select(optionId: string) {
    if (locked || hasDraft || !valid || !onDirectionSelect) return;
    await perform(() => onDirectionSelect(optionId), optionId);
  }
  useImperativeHandle(ref, () => ({
    cacheText(text) { if (!locked) setDraft(previous => ({ ...previous, note: text.slice(0, 1000) })); },
    async submitText(input) {
      const text = input.trim();
      if (!text || text.length > 1000) throw new Error('修改意见需要1至1000字。');
      if (!onDirectionModify) throw new Error('当前无法提交修改，请刷新后重试。');
      setDraft(previous => ({ ...previous, note: text }));
      await perform(() => onDirectionModify(text));
    },
  }));

  return <section className="conversation-gate gate-pending direction-gate" aria-label="选择广告创意方向">
    <div className="gate-question-card" aria-busy={submitting}>
      <div className="gate-card-heading"><h3>先选一个广告方向</h3><span className="direction-count">3 个方向</span></div>
      <p className="gate-description">选好后，再展开完整脚本与分镜。也可以在下方告诉我想怎么改。</p>
      {valid ? <div className="direction-options" role="group" aria-label="三个创意方向">{gate.options.map(option => <article key={option.id} className={`direction-card ${submittingId === option.id ? 'is-submitting' : ''}`}>
        <OptionContent option={option} action={<button type="button" className="direction-select" aria-label={`选择方向：${option.label}`} disabled={locked || hasDraft || !onDirectionSelect} onClick={() => void select(option.id).catch(() => {})}>
          {submittingId === option.id && (submitting || submitted) ? <>{submitted ? <Check size={13}/> : <LoaderCircle className="spin" size={13}/>} {submitted ? '已提交' : '正在选择…'}</> : <>选这个方向<ArrowRight size={13}/></>}
        </button>}/>
      </article>)}</div> : <p className="gate-inline-error" role="alert"><TriangleAlert size={15}/>方向内容不完整，请刷新项目后重试，或在下方补充需求。</p>}
      <p className="direction-status" role="status">{submitted ? '已提交，正在准备下一步…' : submitting ? '正在提交，请稍候…' : hasDraft ? '请先发送修改意见，或清空后选择方向。' : '点击一个方向继续，推荐项也需要你选择。'}</p>
      {(localError || error) && <div className="gate-inline-error" role="alert"><TriangleAlert size={15}/><span>{localError || error}</span></div>}
      {storageUnavailable && <p className="gate-storage-warning">修改意见暂时无法保存，刷新前请先发送。</p>}
    </div>
  </section>;
});
