'use strict';

/* md.js — minimal Markdown renderer for LLM output.

   Replaces the `marked` CDN dependency. Critically, this escapes HTML
   *before* parsing: the input is model-generated text from Perplexity, so
   passing it through a renderer that permits raw HTML is an injection path.
   Nothing here ever emits a tag it did not construct itself.

   Supports what the research prompts actually produce: ## headings, bold,
   italic, inline code, links, bullet and numbered lists, blockquotes,
   horizontal rules, and paragraphs. */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, ch => ESCAPES[ch]);
}

/* Inline formatting. Runs on already-escaped text, so any < > in the source
   is inert by this point and only our own markup survives. */
function inline(text) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    // Only http(s) links — blocks javascript: and data: URLs by construction.
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    )
    .replace(
      /(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>',
    );
}

export function renderMarkdown(src) {
  if (!src) return '';

  const lines = escapeHtml(src).split('\n');
  const out = [];
  let listType = null;      // 'ul' | 'ol' | null
  let paragraph = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${inline(paragraph.join(' '))}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => {
    if (listType) { out.push(`</${listType}>`); listType = null; }
  };
  const openList = (type) => {
    if (listType !== type) { closeList(); out.push(`<${type}>`); listType = type; }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (!line.trim()) { flushParagraph(); closeList(); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph(); closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,}|━{3,})$/.test(line.trim())) {
      flushParagraph(); closeList();
      out.push('<hr/>');
      continue;
    }

    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    if (bullet) {
      flushParagraph(); openList('ul');
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }

    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      flushParagraph(); openList('ol');
      out.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }

    const quote = line.match(/^\s*&gt;\s?(.*)$/);   // '>' is escaped by now
    if (quote) {
      flushParagraph(); closeList();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }

    closeList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  closeList();
  return out.join('\n');
}
