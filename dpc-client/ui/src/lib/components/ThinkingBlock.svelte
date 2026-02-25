<script lang="ts">
  /**
   * ThinkingBlock.svelte
   *
   * Collapsible component for displaying AI thinking/reasoning content.
   * Used for models like DeepSeek R1, Claude Extended Thinking, OpenAI o1/o3.
   *
   * v1.4 (February 2026) - Thinking Mode Support
   */

  interface Props {
    thinking: string;
    tokenCount?: number;
    collapsed?: boolean;
  }

  let { thinking, tokenCount, collapsed = true }: Props = $props();
  let isExpanded = $state(!collapsed);
</script>

{#if thinking}
  <div class="thinking-block" class:expanded={isExpanded}>
    <button
      class="thinking-toggle"
      onclick={() => isExpanded = !isExpanded}
      aria-expanded={isExpanded}
      aria-controls="thinking-content"
    >
      <span class="toggle-icon" aria-hidden="true">{isExpanded ? '▼' : '▶'}</span>
      <span class="thinking-label">Thinking</span>
      {#if tokenCount}
        <span class="token-count">({tokenCount} tokens)</span>
      {/if}
    </button>

    {#if isExpanded}
      <div class="thinking-content" id="thinking-content">
        <pre>{thinking}</pre>
      </div>
    {/if}
  </div>
{/if}

<style>
  .thinking-block {
    background: linear-gradient(135deg, #f8f9ff 0%, #f0f4ff 100%);
    border: 1px solid #d0d8f0;
    border-radius: 8px;
    margin: 0.5rem 0;
    overflow: hidden;
    transition: all 0.2s ease;
  }

  .thinking-block:hover {
    border-color: #b8c4e0;
  }

  .thinking-block.expanded {
    background: linear-gradient(135deg, #f5f7ff 0%, #e8ecff 100%);
  }

  .thinking-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 12px;
    background: transparent;
    border: none;
    cursor: pointer;
    font-size: 0.85rem;
    color: #556;
    transition: background 0.15s ease;
  }

  .thinking-toggle:hover {
    background: rgba(0, 0, 0, 0.03);
  }

  .thinking-toggle:focus {
    outline: 2px solid #4a5a8a;
    outline-offset: -2px;
    border-radius: 8px;
  }

  .toggle-icon {
    font-size: 0.7rem;
    color: #88a;
    transition: transform 0.2s ease;
  }

  .thinking-block.expanded .toggle-icon {
    color: #4a5a8a;
  }

  .thinking-label {
    font-weight: 500;
    color: #4a5a8a;
  }

  .token-count {
    color: #88a;
    font-size: 0.8rem;
    margin-left: auto;
  }

  .thinking-content {
    padding: 12px;
    border-top: 1px solid #e0e8f0;
    background: rgba(255, 255, 255, 0.5);
    max-height: 400px;
    overflow-y: auto;
  }

  .thinking-content pre {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-family: 'SF Mono', 'Consolas', 'Monaco', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
    color: #445;
  }

  /* Dark mode support */
  @media (prefers-color-scheme: dark) {
    .thinking-block {
      background: linear-gradient(135deg, #1a1f2e 0%, #242a3d 100%);
      border-color: #3a4055;
    }

    .thinking-block:hover {
      border-color: #4a5065;
    }

    .thinking-block.expanded {
      background: linear-gradient(135deg, #1e2435 0%, #282f42 100%);
    }

    .thinking-toggle {
      color: #99a;
    }

    .thinking-toggle:hover {
      background: rgba(255, 255, 255, 0.05);
    }

    .toggle-icon {
      color: #778;
    }

    .thinking-block.expanded .toggle-icon {
      color: #99aaff;
    }

    .thinking-label {
      color: #99aaff;
    }

    .token-count {
      color: #778;
    }

    .thinking-content {
      border-top-color: #3a4055;
      background: rgba(0, 0, 0, 0.2);
    }

    .thinking-content pre {
      color: #bbc;
    }
  }
</style>
