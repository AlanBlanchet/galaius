/** Theme values a webview cannot safely use exactly as VS Code hands them over.
 *
 *  --vscode-descriptionForeground is the one that matters here. VS Code's own Light theme ships
 *  it at #767676 on a #f8f8f8 canvas — **4.28:1, under the WCAG AA floor before any panel touches
 *  it** — and interact's webviews use it for label text that says who spoke, what a lane is, what
 *  a figure means. The dark themes clear it comfortably, which is why it went unnoticed until a
 *  panel's light theme was rendered for the first time and measured.
 *
 *  Pulled toward the editor foreground, it clears in both. One definition, because the same token
 *  was piped raw into seven places across five files and a fix applied to two of them is not a
 *  fix — it is a smaller version of the same bug.
 */
export const DIM_FOREGROUND =
  "color-mix(in srgb, var(--vscode-descriptionForeground, #9a9a9a) 70%, " +
  "var(--vscode-editor-foreground, #d4d4d4))";
