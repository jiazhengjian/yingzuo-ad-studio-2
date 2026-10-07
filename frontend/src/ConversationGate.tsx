import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Check, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, Circle, Images, MessageSquare, Send, TriangleAlert } from 'lucide-react';
import type { Answer, ExistingGate, Gate } from './api';
import DirectionGate from './DirectionGate';
import { answerCurrentQuestion, answerCurrentWithText, answerLabel, draftKey, initialDraft, questionOptions, restoreDraft, signatureOf, submitAnswers, validAnswer, visibleQuestions } from './gateDraft';
import type { GateDraft } from './gateDraft';
import { adFormOptionDescription } from './adForm';
import './conversation.css';

export interface GateComposerState {
  gateId: string;
  mode: 'question' | 'review' | 'creative' | 'direction';
  questionId?: string;
  prompt: string;
  canInput: boolean;
  maxLength: number;
  initialText?: string;
}
export interface ConversationGateHandle { submitText: (text: string) => Promise<void>; cacheText: (text: string) => void }
export interface ConversationGateProps {
  gate: Gate;
  onSubmit: (answers: Answer[]) => Promise<void>;
  onCreativeSubmit: (optionId: string, note: string) => Promise<void>;
  onDirectionSelect?: (optionId: string) => Promise<void>;
  onDirectionModify?: (text: string) => Promise<void>;
  busy: boolean;
  error?: string;
  composerText?: string;
  onComposerStateChange?: (state: GateComposerState) => void;
  onReviewImages?: () => void;
}
function clearDraft(gateId: string) { try { sessionStorage.removeItem(draftKey(gateId)); } catch { /* Storage may be disabled. */ } }
function loadDraft(gate: Gate) { try { return restoreDraft(gate, sessionStorage.getItem(draftKey(gate.id))); } catch { return initialDraft(gate); } }

const ConversationGate = forwardRef<ConversationGateHandle, ConversationGateProps>(function ConversationGate(props, ref) {
  const { gate } = props;
  useEffect(() => { if (gate.status !== 'pending') clearDraft(gate.id); }, [gate.id, gate.status]);
  if (gate.kind === 'direction') return <DirectionGate key={`${gate.id}:${gate.context_version}:${signatureOf(gate)}`} {...props} gate={gate} ref={ref}/>;
  if (gate.status !== 'pending') return <ResolvedGate gate={gate} />;
  return <PendingGate key={`${gate.id}:${signatureOf(gate)}`} {...props} gate={gate} ref={ref} />;
});
export default ConversationGate;

function ResolvedGate({ gate }: { gate: ExistingGate }) {
  const superseded = gate.status === 'superseded';
  const creative = gate.kind === 'creative';
  const selection = gate.options.find(option => option.id === gate.selected_option_id);
  return <details className={`conversation-gate gate-resolved ${superseded ? 'gate-superseded' : ''}`}>
    <summary><span>{superseded ? <Circle size={15} /> : <CheckCircle2 size={15} />}{superseded ? '此确认已由后续要求替代' : creative ? gate.note?.trim() ? '图片修改意见已提交' : '图片已确认' : '选项已确认'}</span><ChevronDown size={15} /></summary>
    <div className="gate-resolved-body">
      {superseded && <p>这张卡片已不再等待回答，请继续查看最新对话。</p>}
      {!creative && gate.questions.map(question => <div className="gate-saved-answer" key={question.id}><span>{question.prompt}</span><strong>{answerLabel(question, gate.answers.find(answer => answer.question_id === question.id))}</strong></div>)}
      {creative && selection && <div className="gate-saved-answer"><span>确认的开场</span><strong>{selection.label}</strong><p>{selection.description}</p></div>}
      {creative && gate.note?.trim() && <div className="gate-saved-answer"><span>修改意见</span><strong>{gate.note}</strong><p>先更新方案，图片确认前不会生成视频。</p></div>}
    </div>
  </details>;
}

const PendingGate = forwardRef<ConversationGateHandle, Omit<ConversationGateProps, 'gate'> & { gate: ExistingGate }>(function PendingGate({ gate, onSubmit, onCreativeSubmit, busy, error, composerText = '', onComposerStateChange, onReviewImages }, ref) {
  const [draft, setDraft] = useState<GateDraft>(() => loadDraft(gate));
  const draftRef = useRef(draft);
  const [localError, setLocalError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [storageUnavailable, setStorageUnavailable] = useState(false);
  const submissionLock = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const previousStep = useRef(draft.step);
  const locked = busy || submitting || submitted;
  const creative = gate.kind === 'creative';
  const questions = visibleQuestions(gate, draft.answers);
  const review = draft.step >= questions.length;
  const question = questions[draft.step];
  const answer = question ? draft.answers[question.id] : undefined;
  const cachedText = question ? draft.composerDrafts[question.id] : undefined;
  const allAnswered = questions.length > 0 && questions.every(item => validAnswer(item, draft.answers[item.id]));
  const selection = gate.options.find(option => option.id === draft.selectedOptionId);
  const visibleError = localError || error;

  function updateDraft(value: GateDraft) { draftRef.current = value; setDraft(value); }
  useEffect(() => {
    if (submitted) return;
    try { sessionStorage.setItem(draftKey(gate.id), JSON.stringify(draft)); setStorageUnavailable(false); }
    catch { setStorageUnavailable(true); }
  }, [draft, gate.id, submitted]);
  useEffect(() => { if (previousStep.current !== draft.step) { heading.current?.focus(); previousStep.current = draft.step; } }, [draft.step]);
  useEffect(() => {
    onComposerStateChange?.({ gateId: gate.id, mode: creative ? 'creative' : review ? 'review' : 'question', questionId: question?.id,
      prompt: creative ? '图片哪里需要调整？在这里告诉我…' : review ? '答案已填好，请在上方提交全部或返回修改' : question?.prompt ?? '请回答当前问题',
      canInput: creative || (!review && question?.allow_custom !== false), maxLength: creative ? 1000 : 500,
      initialText: creative ? draft.note : cachedText ?? answer?.text,
    });
  }, [gate.id, creative, review, question?.id, question?.prompt, question?.allow_custom, answer?.text, cachedText, draft.note, onComposerStateChange]);

  function choose(nextAnswer: Answer) {
    if (locked || !question) return;
    setLocalError(''); updateDraft(answerCurrentQuestion(gate, draftRef.current, nextAnswer));
  }
  function moveTo(step: number) { if (!locked) { setLocalError(''); updateDraft({ ...draftRef.current, step }); } }
  async function performSubmission(action: () => Promise<void>) {
    if (locked || submissionLock.current) throw new Error('请等待当前操作完成。');
    submissionLock.current = true; setSubmitting(true); setLocalError('');
    try { await action(); setSubmitted(true); clearDraft(gate.id); }
    catch (caught) { setLocalError(caught instanceof Error ? caught.message : '提交没有完成，答案已保留，请重试。'); submissionLock.current = false; throw caught; }
    finally { setSubmitting(false); }
  }
  async function submit() {
    if (creative) {
      if (!selection || composerText.trim()) return;
      await performSubmission(() => onCreativeSubmit(selection.id, ''));
    } else if (allAnswered) {
      await performSubmission(() => onSubmit(submitAnswers(gate, draftRef.current)));
    }
  }
  useImperativeHandle(ref, () => ({
    cacheText(text) {
      if (locked) return;
      if (creative) updateDraft({ ...draftRef.current, note: text.slice(0, 1000) });
      else if (question) updateDraft({ ...draftRef.current, composerDrafts: { ...draftRef.current.composerDrafts, [question.id]: text.slice(0, 500) } });
    },
    async submitText(input) {
      if (locked || submissionLock.current) throw new Error('请等待当前操作完成。');
      if (creative) {
        const note = input.trim();
        if (!note || note.length > 1000) throw new Error('修改意见需要1至1000字。');
        if (!selection) throw new Error('请先选择一个开场方向。');
        updateDraft({ ...draftRef.current, note });
        await performSubmission(() => onCreativeSubmit(selection.id, note));
      } else {
        try { updateDraft(answerCurrentWithText(gate, draftRef.current, input)); setLocalError(''); }
        catch (caught) { setLocalError(caught instanceof Error ? caught.message : '请回答当前问题。'); throw caught; }
      }
    },
  }));

  if (!creative && gate.questions.length === 0) return <div className="gate-inline-error" role="alert"><TriangleAlert size={16} />这张澄清卡暂时没有问题，请刷新项目状态后再试。</div>;
  return <section className="conversation-gate gate-pending" aria-label={creative ? '检查并确认图片' : '补充广告需求'}>
    <div className={`gate-waiting-strip ${submitted ? 'gate-submitted-strip' : ''}`} role="status"><MessageSquare size={16} /><span>{submitted ? '回答已提交，等待同步最新状态' : submitting ? '正在提交…' : creative ? '等待检查图片' : '等待你的回答'}</span></div>
    <div className="gate-question-card" aria-busy={submitting}>
      {creative ? <>
        <div className="gate-card-heading"><h3 ref={heading} tabIndex={-1}>检查生成图片</h3>{onReviewImages && <button type="button" className="gate-link-button" onClick={onReviewImages}><Images size={14}/>查看图片</button>}</div>
        <p className="gate-description">视频将使用这些参考图片。请检查图片是否符合已确认的方案。</p>
        {gate.options.length > 1 && <details className="gate-opening-details"><summary>开场：{selection?.label || '请选择'}<span>可选调整</span><ChevronDown size={13}/></summary><div className="gate-creative-options" role="group" aria-label="开场方向">{gate.options.map(option => <button key={option.id} type="button" className={`gate-creative-option ${draft.selectedOptionId === option.id ? 'selected' : ''}`} aria-pressed={draft.selectedOptionId === option.id} disabled={locked} onClick={() => { setLocalError(''); updateDraft({ ...draftRef.current, selectedOptionId: option.id }); }}><span className="gate-option-title"><strong>{option.label}</strong>{draft.selectedOptionId === option.id && <Check size={15}/>}</span><span className="gate-option-description">{option.description}</span></button>)}</div></details>}
        {!selection && <p className="gate-inline-error" role="alert">请选择开场方向，或刷新获取当前方案。</p>}
        <p className="gate-submit-help">需要修改图片或开场时，直接在下方输入。发送修改意见会先更新方案，不会生成视频。</p>
        <div className="gate-footer"><span className="gate-draft-label">{composerText.trim() ? '请先发送修改意见，或清空后确认图片' : storageUnavailable ? '本地草稿暂无法保存' : '确认后才会开始视频生成'}</span><button type="button" className="gate-submit-button" disabled={locked || !selection || !!composerText.trim()} onClick={() => void submit().catch(() => {})}>{submitted ? <><Check size={14}/>已提交</> : submitting ? '正在提交…' : <><Check size={14}/>图片没问题，生成视频</>}</button></div>
      </> : review ? <>
        <div className="gate-card-heading"><h3 ref={heading} tabIndex={-1}>确认你的选择</h3><CheckCircle2 size={16}/></div>
        <p className="gate-description">检查全部答案，然后统一提交。</p>
        <div className="gate-review-list">{questions.map((item, index) => <div className="gate-review-answer" key={item.id}><div><span>{index + 1}. {item.prompt}</span><button className="gate-link-button" type="button" disabled={locked} onClick={() => moveTo(index)} aria-label={`修改第${index + 1}题答案`}>修改</button></div><strong>{answerLabel(item, draft.answers[item.id])}</strong></div>)}</div>
        <div className="gate-footer"><button className="gate-back-button" type="button" disabled={locked} onClick={() => moveTo(0)}><ChevronLeft size={14}/>返回修改</button><button type="button" className="gate-submit-button" disabled={locked || !allAnswered} onClick={() => void submit().catch(() => {})}>{submitted ? <><Check size={14}/>已提交</> : submitting ? '正在提交…' : <>提交全部<Send size={14}/></>}</button></div>
      </> : question ? <>
        <div className="gate-question-navigation"><span className="gate-question-label">补充一点信息</span><div>{question.allow_skip !== false && <button type="button" className="gate-skip-button" disabled={locked} onClick={() => choose({ question_id: question.id, skipped: true })}>跳过</button>}<span className="gate-counter"><button type="button" aria-label="返回上一题" disabled={locked || draft.step === 0} onClick={() => moveTo(draft.step - 1)}><ChevronLeft size={15}/></button>{draft.step + 1} / {questions.length}</span></div></div>
        <h3 className="gate-question-title" ref={heading} tabIndex={-1}>{question.prompt}</h3>
        <div className="gate-answer-options" role="group" aria-label={question.prompt}>{questionOptions(question).map(option => <button type="button" key={option.id} aria-pressed={answer?.option_id === option.id} className={`gate-answer-option ${answer?.option_id === option.id ? 'selected' : ''}`} disabled={locked} onClick={() => choose({ question_id: question.id, option_id: option.id })}><span>{option.label}{adFormOptionDescription(question.id, option.id) && <small className="ad-form-option-description">{adFormOptionDescription(question.id, option.id)}</small>}</span>{answer?.option_id === option.id ? <Check size={16}/> : <ChevronRight size={15}/>}</button>)}</div>
        {answer?.text && <p className="gate-current-answer">已填写：{answer.text}</p>}
        <div className="gate-footer"><span className="gate-draft-label">{question.allow_custom !== false ? '也可以在下方输入答案，发送后进入下一题' : '选择后进入下一题，最终统一提交'}</span><button type="button" className="gate-next-button" disabled={locked || !validAnswer(question, answer)} onClick={() => moveTo(Math.min(draft.step + 1, questions.length))}>{draft.step + 1 === questions.length ? '检查选择' : '下一题'}<ChevronRight size={14}/></button></div>
      </> : null}
      {visibleError && <div className="gate-inline-error" role="alert"><TriangleAlert size={15}/><span>{visibleError}</span></div>}
      {storageUnavailable && <p className="gate-storage-warning">当前浏览器无法保存草稿，刷新前请先提交。</p>}
    </div>
  </section>;
});
