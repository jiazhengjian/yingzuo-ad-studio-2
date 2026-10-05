import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowRight, Check, CheckCircle2, ChevronDown, Circle, LoaderCircle, TriangleAlert } from 'lucide-react';
import type { DirectionGateData, DirectionOption } from './api';
import type { ConversationGateHandle, ConversationGateProps } from './ConversationGate';
import { draftKey, initialDraft, restoreDraft } from './gateDraft';
import { directionSubmission, validDirectionOptions } from './directionSelection';
import ProductionSummary from './ProductionSummary';

type Props = Omit<ConversationGateProps, 'gate'> & { gate: DirectionGateData };

const RULE_LABELS: Record<string, string> = {
  sensory_detail: '感官细节', everyday_context: '生活情境', appearance_reveal: '外观揭示',
  operation_walkthrough: '操作讲解', problem_solution: '问题与解决', evidence_comparison: '有依据的对比',
  feature_value: '特点与选择理由', exaggeration: '夸张演绎', contrast_humor: '反差幽默',
  identity_attitude: '身份与态度', ritual_sensory: '仪式与感官', situational_conflict: '情境冲突',
};
const HOOK_LABELS: Record<string, string> = {
  macro_detail: '微距开场', silhouette: '剪影开场', signature_sound: '声音开场', scale_shift: '尺度开场',
  awkward_moment: '尴尬开场', situational_conflict: '小冲突开场', arresting_line: '一句开场',
  direct_question: '提问开场', counterintuitive: '反常识开场', result_first: '结果开场',
};

function clip(value: string | undefined, limit: number) {
  const cleaned = value?.trim() || '';
  if (cleaned.length <= limit) return cleaned;
  const window = cleaned.slice(0, limit);
  const earliest = Math.max(8, Math.floor(limit / 3));
  for (const mark of ['。', '！', '？', '；', '，']) {
    const at = window.lastIndexOf(mark);
    if (at >= earliest) return window.slice(0, at + 1).trim();
  }
  return window.trim();
}

function techniqueTag(option: DirectionOption) {
  const rule = RULE_LABELS[option.rule_id] || '';
  const hook = option.hook_type ? HOOK_LABELS[option.hook_type] || '' : '';
  if (rule && hook) return `${rule} · ${hook}`;
  return rule || hook;
}

const SKELETON_LABELS: Record<string, string> = {
  product_visual: '拆解', world_intercut: '穿梭', lifestyle: '生活',
};

function pictureLabel(option: DirectionOption) {
  const concept = option.film_concept;
  if (!concept) return '';
  return SKELETON_LABELS[concept.visual_skeleton || ''] || SKELETON_LABELS[concept.structure] || '';
}

function PitchLines({ option }: { option: DirectionOption }) {
  const rows = [
    ['核心创意', clip(option.product_expression, 60)],
    ['洞察', clip(option.consumer_insight, 40)],
    ['怎么讲画面', pictureLabel(option)],
    ['侧面证明', clip(option.film_concept?.side_proof, 40)],
    ['贯穿母题', clip(option.film_concept?.visual_motif, 16)],
    ['0–3秒', clip(option.hook, 40)],
    ['中段', clip(option.film_concept?.key_visual, 40)],
    ['结尾', clip(option.payoff, 40)],
    ['为什么吸引人', clip(option.reason, 40)],
  ].filter(([, text]) => text);
  if (!rows.length) return null;
  return <dl className="direction-film-main">{rows.map(([label, text]) => <div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}</dl>;
}

function OptionContent({ option, selected, action }: { option: DirectionOption; selected?: boolean; action?: ReactNode }) {
  const technique = techniqueTag(option);
  return <>
    <div className="direction-copy">
    <div className="direction-card-heading"><div className="direction-card-title"><h4>方向：{option.label}</h4>{technique && <p className="direction-technique">{technique}</p>}</div><span className="direction-badges">
      {selected && <span className="direction-chosen"><Check size={12}/>已选择</span>}
      {option.recommended === true && <span className="direction-recommended">推荐</span>}
    </span></div>
    <PitchLines option={option}/>
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
