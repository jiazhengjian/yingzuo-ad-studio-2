import type { AdForm } from './api';
import { adFormDetails } from './adForm';

export default function AdFormSummary({ adForm, onChange }: { adForm?: AdForm | null; onChange?: () => void }) {
  const details = adFormDetails(adForm);
  if (!details) return null;
  return <div className="ad-form-summary" aria-label="已保存的广告倾向">
    <div><span>广告倾向</span><strong>{details.label}</strong>{onChange && <button type="button" onClick={onChange} aria-label="修改广告倾向">修改</button>}</div>
    {onChange && <small>可在下方输入，例如“改成产品展示广告”。</small>}
  </div>;
}
