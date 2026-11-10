export const POINTER_START = "<!-- pensieve-docs:start -->";
export const POINTER_END = "<!-- pensieve-docs:end -->";

export const pointerText = (out) =>
  `Pensieve's docs for this repository are pulled into \`${out}/\` (start at \`index.md\`). ` +
  "They may lag the code: check the revision in `index.md` and verify against source.";

/**
 * Insert or refresh a marked block (the docs pointer unless other markers are given). Running it again with the same
 * text returns the content unchanged, so repeated pulls never duplicate it.
 */
export function upsertPointer(content, text, [open, close] = [POINTER_START, POINTER_END]) {
  const block = `${open}\n${text}\n${close}`;
  const start = content.indexOf(open);
  const end = content.indexOf(close, start);
  if (start >= 0 && end > start) {
    return content.slice(0, start) + block + content.slice(end + close.length);
  }
  const separator = !content ? "" : content.endsWith("\n\n") ? "" : content.endsWith("\n") ? "\n" : "\n\n";
  return `${content}${separator}${block}\n`;
}
