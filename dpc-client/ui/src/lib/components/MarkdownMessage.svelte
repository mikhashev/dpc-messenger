<!-- MarkdownMessage.svelte -->
<!-- Secure markdown renderer for AI-generated content using @humanspeak/svelte-markdown -->
<script lang="ts">
  import SvelteMarkdown from '@humanspeak/svelte-markdown';

  interface Props {
    content: string;
  }

  let { content }: Props = $props();
</script>

<div class="markdown-content">
  <SvelteMarkdown source={content} />
</div>

<style>
  .markdown-content {
    line-height: 1.6;
    color: #333;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
    max-width: 100%; /* Constrain to parent width */
  }

  /* Typography */
  .markdown-content :global(h1) {
    font-size: 1.5rem;
    font-weight: bold;
    margin: 1rem 0 0.5rem 0;
    border-bottom: 2px solid #e0e0e0;
    padding-bottom: 0.3rem;
  }

  .markdown-content :global(h2) {
    font-size: 1.3rem;
    font-weight: bold;
    margin: 0.875rem 0 0.5rem 0;
    border-bottom: 1px solid #e0e0e0;
    padding-bottom: 0.25rem;
  }

  .markdown-content :global(h3) {
    font-size: 1.1rem;
    font-weight: bold;
    margin: 0.75rem 0 0.5rem 0;
  }

  .markdown-content :global(h4),
  .markdown-content :global(h5),
  .markdown-content :global(h6) {
    font-size: 1rem;
    font-weight: bold;
    margin: 0.5rem 0;
  }

  .markdown-content :global(p) {
    margin: 0.75rem 0;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
  }

  .markdown-content :global(p:first-child) {
    margin-top: 0;
  }

  .markdown-content :global(p:last-child) {
    margin-bottom: 0;
  }

  /* Inline code */
  .markdown-content :global(code) {
    background: #f5f5f5;
    padding: 0.2em 0.4em;
    border-radius: 3px;
    font-family: 'Courier New', Consolas, Monaco, monospace;
    font-size: 0.9em;
    color: #d73a49;
  }

  /* Code blocks */
  .markdown-content :global(pre) {
    background: #f6f8fa;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 1rem;
    overflow-x: auto;
    margin: 1rem 0;
  }

  .markdown-content :global(pre code) {
    background: transparent;
    padding: 0;
    color: #24292e;
    font-size: 0.85em;
    line-height: 1.45;
  }

  /* Lists */
  .markdown-content :global(ul),
  .markdown-content :global(ol) {
    margin: 0.75rem 0;
    padding-left: 2rem;
  }

  .markdown-content :global(li) {
    margin: 0.25rem 0;
  }

  .markdown-content :global(ul) {
    list-style-type: disc;
  }

  .markdown-content :global(ol) {
    list-style-type: decimal;
  }

  .markdown-content :global(li > ul),
  .markdown-content :global(li > ol) {
    margin: 0.25rem 0;
  }

  /* Links */
  .markdown-content :global(a) {
    color: #0366d6;
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-bottom 0.2s;
  }

  .markdown-content :global(a:hover) {
    border-bottom: 1px solid #0366d6;
  }

  /* Blockquotes */
  .markdown-content :global(blockquote) {
    border-left: 4px solid #dfe2e5;
    padding: 0.5rem 1rem;
    margin: 1rem 0;
    background: #f8f9fa;
    color: #6a737d;
  }

  .markdown-content :global(blockquote p) {
    margin: 0.5rem 0;
  }

  /* Tables */
  .markdown-content :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 1rem 0;
  }

  .markdown-content :global(th),
  .markdown-content :global(td) {
    border: 1px solid #dfe2e5;
    padding: 0.5rem 0.75rem;
    text-align: left;
  }

  .markdown-content :global(th) {
    background: #f6f8fa;
    font-weight: 600;
  }

  .markdown-content :global(tr:nth-child(even)) {
    background: #f8f9fa;
  }

  /* Horizontal rule */
  .markdown-content :global(hr) {
    border: none;
    border-top: 2px solid #e1e4e8;
    margin: 1.5rem 0;
  }

  /* Bold and italic */
  .markdown-content :global(strong) {
    font-weight: bold;
  }

  .markdown-content :global(em) {
    font-style: italic;
  }

  /* Images */
  .markdown-content :global(img) {
    max-width: 100%;
    height: auto;
    border-radius: 4px;
    margin: 0.5rem 0;
  }
</style>
