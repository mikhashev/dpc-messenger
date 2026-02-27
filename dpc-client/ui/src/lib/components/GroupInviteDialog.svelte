<!-- GroupInviteDialog.svelte - Accept/decline group chat invitations -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let open: boolean = false;
  export let invite: {
    group_id: string;
    name: string;
    topic?: string;
    created_by: string;
    creator_name: string;
    members: string[];
  } | null = null;

  const dispatch = createEventDispatcher();

  function handleAccept() {
    if (!invite) return;
    dispatch('accept', { group_id: invite.group_id });
    open = false;
  }

  function handleDecline() {
    if (!invite) return;
    dispatch('decline', { group_id: invite.group_id });
    open = false;
  }
</script>

{#if open && invite}
  <div class="modal-overlay" role="presentation">
    <div class="modal" role="dialog" aria-labelledby="invite-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="invite-title">Group Invitation</h2>
      </div>

      <div class="modal-body">
        <p class="invite-message">
          <strong>{invite.creator_name}</strong> invited you to join a group:
        </p>

        <div class="group-info">
          <div class="info-row">
            <span class="label">Group</span>
            <span class="value">{invite.name}</span>
          </div>
          {#if invite.topic}
            <div class="info-row">
              <span class="label">Topic</span>
              <span class="value">{invite.topic}</span>
            </div>
          {/if}
          <div class="info-row">
            <span class="label">Members</span>
            <span class="value">{invite.members?.length || 0} participants</span>
          </div>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-decline" on:click={handleDecline}>Decline</button>
        <button class="btn-accept" on:click={handleAccept}>Join Group</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: #1e1e2e;
    border: 1px solid #45475a;
    border-radius: 12px;
    width: 380px;
    max-width: 90vw;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  }

  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid #45475a;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.1rem;
    color: #cdd6f4;
  }

  .modal-body {
    padding: 16px 20px;
  }

  .invite-message {
    margin: 0 0 12px;
    font-size: 0.9rem;
    color: #bac2de;
  }

  .invite-message strong {
    color: #89b4fa;
  }

  .group-info {
    background: #313244;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .info-row {
    display: flex;
    gap: 8px;
    align-items: baseline;
  }

  .label {
    font-size: 0.75rem;
    color: #6c7086;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    min-width: 60px;
    flex-shrink: 0;
  }

  .value {
    font-size: 0.9rem;
    color: #cdd6f4;
    font-weight: 500;
  }

  .modal-footer {
    padding: 12px 20px;
    border-top: 1px solid #45475a;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }

  .btn-decline {
    padding: 8px 16px;
    border: 1px solid #45475a;
    border-radius: 6px;
    background: transparent;
    color: #a6adc8;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .btn-decline:hover {
    background: #313244;
    border-color: #f38ba8;
    color: #f38ba8;
  }

  .btn-accept {
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    background: #a6e3a1;
    color: #1e1e2e;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.85rem;
  }

  .btn-accept:hover {
    background: #94e2d5;
  }
</style>
