"""Quick corpus analysis for Task 003 (pre-tokenizer).

Word-level analysis (whitespace + lowercase) since tokenizer is not trained yet.
Reports:
  - Total lines, unique texts, exact-duplicate count
  - Length distribution (chars, words): min/max/mean/median + percentiles
  - Vocabulary size + top tokens
  - Per-category breakdown
  - Per-triple paraphrase quality (text-distinct within same source_triple_hash)
"""
import json
import sys
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median, quantiles

CORPUS = Path(r"C:/Users/mike/.dpc/agents/agent_001/training_data/corpus.jsonl")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORD_RE = re.compile(r"\w+", re.UNICODE)


def percentiles(values, ps=(5, 25, 50, 75, 95, 99)):
    if not values:
        return {p: 0 for p in ps}
    q = quantiles(values, n=100)
    return {p: q[p - 1] for p in ps}


def main():
    total = 0
    char_lens = []
    word_lens = []
    text_counter = Counter()
    token_counter = Counter()
    cat_count = Counter()
    cat_char_sum = defaultdict(int)
    cat_word_sum = defaultdict(int)
    triple_texts = defaultdict(list)
    triple_cat = {}
    template_idx_counter = Counter()

    with CORPUS.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            text = rec["text"]
            cat = rec.get("source_category", "?")
            triple_hash = rec.get("source_triple_hash", "")
            tidx = rec.get("template_idx", -1)

            total += 1
            char_lens.append(len(text))
            tokens = WORD_RE.findall(text.lower())
            word_lens.append(len(tokens))
            text_counter[text] += 1
            token_counter.update(tokens)
            cat_count[cat] += 1
            cat_char_sum[cat] += len(text)
            cat_word_sum[cat] += len(tokens)
            triple_texts[triple_hash].append(text)
            triple_cat[triple_hash] = cat
            template_idx_counter[tidx] += 1

    print(f"# Corpus Analysis — corpus.jsonl\n")
    print(f"**File:** `{CORPUS}` ({CORPUS.stat().st_size:,} bytes)")
    print(f"**Total lines:** {total:,}\n")

    # 1. Duplicates
    unique_texts = len(text_counter)
    dup_count = total - unique_texts
    dup_texts = [t for t, c in text_counter.items() if c > 1]
    print(f"## 1. Duplicates (exact text match)\n")
    print(f"- Unique texts: **{unique_texts:,}** / {total:,}")
    print(f"- Exact duplicates (extra copies): **{dup_count:,}** ({dup_count / total * 100:.2f}%)")
    print(f"- Distinct duplicated texts: **{len(dup_texts):,}**\n")
    if dup_texts:
        print("Top 5 most-duplicated texts:")
        for t, c in text_counter.most_common(5):
            if c > 1:
                preview = t[:80].replace("\n", " ")
                print(f"- `{c}x` — {preview}")
        print()

    # 2. Length distribution
    print("## 2. Length Distribution\n")
    print("| Metric | Chars | Words |")
    print("|---|---|---|")
    print(f"| min | {min(char_lens)} | {min(word_lens)} |")
    print(f"| max | {max(char_lens)} | {max(word_lens)} |")
    print(f"| mean | {mean(char_lens):.1f} | {mean(word_lens):.1f} |")
    print(f"| median | {median(char_lens):.0f} | {median(word_lens):.0f} |")
    cp = percentiles(char_lens)
    wp = percentiles(word_lens)
    for p in (5, 25, 75, 95, 99):
        print(f"| p{p} | {cp[p]:.0f} | {wp[p]:.0f} |")
    print()

    # 3. Vocabulary
    print("## 3. Vocabulary (word-level, lowercase, \\w+ regex)\n")
    print(f"- Vocab size: **{len(token_counter):,}**")
    print(f"- Total tokens: **{sum(token_counter.values()):,}**")
    print(f"- Avg tokens/line: **{sum(token_counter.values()) / total:.1f}**\n")
    hapax = sum(1 for c in token_counter.values() if c == 1)
    print(f"- Hapax legomena (count=1): **{hapax:,}** ({hapax / len(token_counter) * 100:.1f}% of vocab)")
    print(f"- Tokens appearing ≥10x: **{sum(1 for c in token_counter.values() if c >= 10):,}**")
    print(f"- Tokens appearing ≥100x: **{sum(1 for c in token_counter.values() if c >= 100):,}**\n")
    print("Top 20 most frequent:")
    for tok, c in token_counter.most_common(20):
        print(f"- `{tok}` — {c:,}")
    print()

    # 4. Per-category breakdown
    print("## 4. Per-Category Breakdown\n")
    print("| Category | Lines | % | Mean chars | Mean words |")
    print("|---|---|---|---|---|")
    for cat in sorted(cat_count, key=lambda x: -cat_count[x]):
        c = cat_count[cat]
        print(f"| {cat} | {c:,} | {c/total*100:.1f}% | {cat_char_sum[cat]/c:.1f} | {cat_word_sum[cat]/c:.1f} |")
    print()

    # 5. Paraphrase quality (per-triple distinctness)
    print("## 5. Paraphrase Quality (within-triple distinctness)\n")
    triple_count = len(triple_texts)
    paraphrases_per_triple = Counter()
    distinct_per_triple = []
    triples_with_internal_dup = 0
    for h, texts in triple_texts.items():
        paraphrases_per_triple[len(texts)] += 1
        d = len(set(texts))
        distinct_per_triple.append(d)
        if d < len(texts):
            triples_with_internal_dup += 1
    print(f"- Total distinct triples: **{triple_count:,}**")
    print(f"- Paraphrases per triple distribution:")
    for k in sorted(paraphrases_per_triple):
        print(f"  - {k} paraphrase(s): {paraphrases_per_triple[k]:,} triples")
    print(f"- Triples with internal duplicate paraphrases: **{triples_with_internal_dup:,}** ({triples_with_internal_dup / triple_count * 100:.2f}%)")
    print(f"- Avg distinct paraphrases per triple: **{mean(distinct_per_triple):.2f}**\n")

    # 6. Template index
    print("## 6. Template Index Distribution\n")
    for tidx in sorted(template_idx_counter):
        print(f"- template_idx={tidx}: {template_idx_counter[tidx]:,}")
    print()


if __name__ == "__main__":
    main()
