// The consent dialog's disk-space verdict, the same rule the backend's
// download_other applies: (size + installed) x 1.1 must be free. An absent
// installed size adds nothing (not stated, never a known 0). For a size that is
// a range (size_source 'package_max') the refusal takes the smallest variant and
// the largest only warns: which one is fetched depends on the card.

export const FREE_SPACE_MARGIN = 1.1;

export type SpaceVerdict = 'enough' | 'may_not_fit' | 'short' | 'unknown';

export function neededBytes(size: number | null, installed: number | null): number | null {
    return !size ? null : (size + (installed ?? 0)) * FREE_SPACE_MARGIN;
}

export function spaceVerdict(
    free: number | null,
    size: number | null,
    installed: number | null,
    sizeMin: number | null = null,
    installedMin: number | null = null
): SpaceVerdict {
    const most = neededBytes(size, installed);
    const least = sizeMin ? neededBytes(sizeMin, installedMin) : most;
    if (free == null || most == null || least == null) return 'unknown';
    if (free < least) return 'short';
    if (free < most) return 'may_not_fit';
    return 'enough';
}
