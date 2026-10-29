/**
 * Who a transfer in the Active Transfers row is from or to, and in which group
 * (wording Mike's, 2026-10-29). Nodes are named the way the permission dialog
 * names them (`labelOf`), and a relayed group file reads "from <author> via
 * <node>", the author being the offer's unsigned claim.
 */

import { authorLabel, cameThroughAnotherNode, labelOf } from '../fileTransferPermission';

export interface TransferRow {
    node_id?: string;
    direction?: string;       // absent on an offer: the row is a download
    group_id?: string;
    sender_name?: string;     // the offer's name for node_id
    sender_node_id?: string;  // a group file's claimed author
}

type NameOf = (id: string) => string | undefined;

function nodeLabel(id: string, ownName: string | undefined, nameOf?: NameOf): string {
    const name = ownName && ownName !== id ? ownName : nameOf?.(id);
    return labelOf(id, name);
}

/** The line under the file name: `in <group> · from <sender>`, `to <peer>`, … */
export function transferRowLabel(transfer: TransferRow, nameOf?: NameOf, groupNameOf?: NameOf): string {
    const id = transfer.node_id || '';
    const group = transfer.group_id ? (groupNameOf?.(transfer.group_id) || transfer.group_id) : '';
    const where = group ? `in ${group} · ` : '';
    if (transfer.direction === 'upload') {
        return `${where}to ${nodeLabel(id, undefined, nameOf)}`;
    }
    const hop = nodeLabel(id, transfer.sender_name, nameOf);
    const offer = { ...transfer, node_id: id };
    const who = cameThroughAnotherNode(offer) ? `${authorLabel(offer, nameOf)} via ${hop}` : hop;
    return `${where}from ${who}`;
}
