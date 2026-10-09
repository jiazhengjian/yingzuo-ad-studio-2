import { homepageMessageTag } from './adForm';

/** Chip above the first user message. Text comes only from the homepage type card. */
export default function HomeTypeTag({ projectId }: { projectId?: string | null }) {
  const tag = homepageMessageTag(projectId);
  if (!tag) return null;
  return <span className="message-tag">{tag}</span>;
}
