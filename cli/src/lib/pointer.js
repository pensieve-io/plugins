export const POINTER_START = "<!-- pensieve-docs:start -->";
export const POINTER_END = "<!-- pensieve-docs:end -->";

export const pointerText = (out) =>
  `Pensieve's docs for this repository are pulled into \`${out}/\` (start at \`index.md\`). ` +
  "They may lag the code: check the revision in `index.md` and verify against source.";

/**
 * Insert or refresh the marked pointer block. Running it again with the same
 * text returns the content unchanged, so repeated pulls never duplicate it.
 */
export function upsertPointer(content, text) {
  const block = `${POINTER_START}\n${text}\n${POINTER_END}`;
  const start = content.indexOf(POINTER_START);
  const end = content.indexOf(POINTER_END, start);
  if (start >= 0 && end > start) {
    return content.slice(0, start) + block + content.slice(end + POINTER_END.length);
  }
  const separator = !content ? "" : content.endsWith("\n\n") ? "" : content.endsWith("\n") ? "\n" : "\n\n";
  return `${content}${separator}${block}\n`;
}
