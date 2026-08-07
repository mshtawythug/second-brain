import remarkMath from "remark-math"
import rehypeKatex from "rehype-katex"
import rehypeMathjax from "rehype-mathjax/svg"
//@ts-ignore
import rehypeTypst from "@myriaddreamin/rehype-typst"
import { QuartzTransformerPlugin } from "../types"
import { KatexOptions } from "katex"
import { Options as MathjaxOptions } from "rehype-mathjax/svg"
//@ts-ignore
import { Options as TypstOptions } from "@myriaddreamin/rehype-typst"

interface Options {
  renderEngine: "katex" | "mathjax" | "typst"
  customMacros: MacroType
  katexOptions: Omit<KatexOptions, "macros" | "output">
  mathJaxOptions: Omit<MathjaxOptions, "macros">
  typstOptions: TypstOptions
}

// mathjax macros
export type Args = boolean | number | string | null
interface MacroType {
  [key: string]: string | Args[]
}

// brain-extension: single-dollar inline math is DISABLED.
//
// Upstream calls `remarkMath` with no options, and
// micromark-extension-math defaults `singleDollarTextMath` to true — so a
// single `$` opens an inline math span, and (like a code span) it may run
// across newlines until the next `$` in the same paragraph.
//
// This vault is a personal knowledge base with essentially no mathematics but
// a great deal of prose about money. 383 of ~1400 notes contain a `$`; only 2
// contain `$$`. Every ordinary "costs $5 … saves $12" sentence therefore had
// the text BETWEEN the two amounts swallowed into a math span and rendered in
// KaTeX math-italic with inter-word spacing collapsed — measured at 551
// `<span class="katex">` spans across 171 published pages, the longest 278
// characters of running prose.
//
// The build-log symptom was `No character metrics for '»' / '¢' in style
// 'Main-Regular'` (katex.mjs `makeSymbol`), emitted whenever such a swallowed
// span happened to contain a character absent from KaTeX's Main-Regular font.
// That warning is cosmetic on its own — KaTeX keeps the glyph and only zeroes
// its metrics — but it was the visible tip of the real rendering defect.
//
// NOTE for anyone tempted to "fix" this with katexOptions.strict: that does
// NOT suppress these warnings. `strict` gates `Settings.reportNonstrict`
// (a different message, `unicodeTextInMathMode`); the `makeSymbol` warning is
// unconditional. Silencing the strict report is in fact what lets the Latin-1
// character fall through to the zero-metrics branch in the first place.
//
// `$$…$$` block math is UNAFFECTED and still renders.
const remarkMathOptions = { singleDollarTextMath: false }

export const Latex: QuartzTransformerPlugin<Partial<Options>> = (opts) => {
  const engine = opts?.renderEngine ?? "katex"
  const macros = opts?.customMacros ?? {}
  return {
    name: "Latex",
    markdownPlugins() {
      return [[remarkMath, remarkMathOptions]]
    },
    htmlPlugins() {
      switch (engine) {
        case "katex": {
          return [[rehypeKatex, { output: "html", macros, ...(opts?.katexOptions ?? {}) }]]
        }
        case "typst": {
          return [[rehypeTypst, opts?.typstOptions ?? {}]]
        }
        default:
        case "mathjax": {
          return [
            [
              rehypeMathjax,
              {
                ...(opts?.mathJaxOptions ?? {}),
                tex: {
                  ...(opts?.mathJaxOptions?.tex ?? {}),
                  macros,
                },
              },
            ],
          ]
        }
      }
    },
    externalResources() {
      switch (engine) {
        case "katex":
          return {
            css: [{ content: "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" }],
            js: [
              {
                // fix copy behaviour: https://github.com/KaTeX/KaTeX/blob/main/contrib/copy-tex/README.md
                src: "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/copy-tex.min.js",
                loadTime: "afterDOMReady",
                contentType: "external",
              },
            ],
          }
      }
    },
  }
}
