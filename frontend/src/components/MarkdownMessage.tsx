"use client"

import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import type { Components } from "react-markdown"

/**
 * Renders assistant text as GitHub-flavoured Markdown.
 *
 * The feed used to print `msg.content` into a <p> with whitespace-pre-wrap, so every
 * **bold**, `---` and pipe-delimited table the model emits showed up as literal
 * punctuation. The model was already writing Markdown; nothing was reading it.
 *
 * Raw HTML is deliberately NOT enabled (no rehype-raw): this content originates from an
 * LLM summarising third-party spreadsheet data, so treating it as markup would be an
 * injection path. react-markdown escapes HTML by default — keep it that way.
 */

const components: Components = {
  // Tables are the whole point of remark-gfm here. The wrapper scrolls on its own so a
  // wide table never forces the chat column to scroll sideways.
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-white/10">
      <table className="w-full border-collapse text-[13px] tabular-nums">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-white/[0.04]">{children}</thead>,
  th: ({ children }) => (
    <th className="border-b border-white/10 px-3 py-2 text-left font-semibold text-zinc-200 whitespace-nowrap">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-b border-white/5 px-3 py-2 align-top text-zinc-300">{children}</td>
  ),
  tr: ({ children }) => <tr className="hover:bg-white/[0.02] transition-colors">{children}</tr>,

  h1: ({ children }) => <h1 className="mt-4 mb-2 text-base font-bold text-zinc-100">{children}</h1>,
  h2: ({ children }) => <h2 className="mt-4 mb-2 text-[15px] font-bold text-zinc-100">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-3 mb-1.5 text-sm font-semibold text-zinc-200">{children}</h3>,

  p: ({ children }) => <p className="my-2 leading-relaxed first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-2 space-y-1 pl-5 list-disc marker:text-indigo-400">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 space-y-1 pl-5 list-decimal marker:text-zinc-500">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed pl-1">{children}</li>,

  strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
  em: ({ children }) => <em className="italic text-zinc-300">{children}</em>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-indigo-400 underline underline-offset-2 hover:text-indigo-300"
    >
      {children}
    </a>
  ),

  code: ({ className, children }) => {
    // react-markdown v10 routes both inline and fenced code here; a fenced block is the
    // one carrying a language- class, or containing a newline.
    const isBlock = /language-/.test(className || "") || String(children).includes("\n")
    if (isBlock) {
      return (
        <code className="block overflow-x-auto rounded-lg border border-white/10 bg-black/40 p-3 font-mono text-[12.5px] text-zinc-200">
          {children}
        </code>
      )
    }
    return (
      <code className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[12.5px] text-indigo-200">
        {children}
      </code>
    )
  },
  pre: ({ children }) => <pre className="my-3">{children}</pre>,

  blockquote: ({ children }) => (
    <blockquote className="my-3 border-l-2 border-indigo-500/40 pl-3 text-zinc-400 italic">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-white/10" />,
}

export default function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="text-[15px] leading-relaxed text-zinc-100 select-text">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  )
}
