"""Render backlog.md into a scannable board: tools/backlog/backlog.html

Re-run after any backlog edit:  uv run python tools/backlog/build.py
Reads only; never writes to backlog.md.
"""
import html
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]   # tools/backlog/build.py -> repo root

# An explicit path lets --check run against any project's backlog (and against a fixture,
# which is how the rules below are tested). Without one it reads this project's.
_paths = [a for a in sys.argv[1:] if not a.startswith("-")]
SRC = Path(_paths[0]).resolve() if _paths else ROOT / "backlog.md"
DST = Path(__file__).resolve().parent / "backlog.html"

PRIORITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "RESEARCH", "—"]
PRI_CLASS = {"CRITICAL": "crit", "HIGH": "high", "MEDIUM": "med",
             "LOW": "low", "RESEARCH": "res", "—": "none"}

lines = SRC.read_text(encoding="utf-8-sig").split("\n")

section = None
entries = []
for i, line in enumerate(lines):
    m2 = re.match(r"^## (.+)", line)
    if m2:
        section = m2.group(1).strip()
        continue
    m3 = re.match(r"^### (.+)", line)
    if not (m3 and section):
        continue
    head = m3.group(1)

    name = re.split(r"[:—]", head)[0].strip()
    rest = head[len(name):].lstrip(" :—").strip()

    # The envelope is the LAST top-level (...) group, matched by walking back over
    # balanced parens. `\(([^()]*)\)` cannot nest, so on a heading whose origin quotes
    # someone verbatim and that quote carries its own parentheses,
    # it returned the inner aside and lost the envelope entirely, reporting a complete
    # entry as missing its priority and origin. Found by Warren on the first real entry
    # written to the new standard; the seven-entry fixture had no nested parens to catch it.
    env = ""
    s = head.rstrip()
    if s.endswith(")"):
        depth = 0
        for k in range(len(s) - 1, -1, -1):
            if s[k] == ")":
                depth += 1
            elif s[k] == "(":
                depth -= 1
                if depth == 0:
                    env = s[k + 1:-1]
                    break

    # Priority is read only from that envelope. Scanning the whole heading once picked
    # up prose like "P0 needs Mike's verb" and turned a MEDIUM entry into a CRITICAL one.
    pri = "—"
    pri_typo = ""
    m = re.match(r"\s*(CRIT|CRITICAL|HIGH|MEDIUM|LOW|RESEARCH|NORMAL)\b", env)
    if m:
        pri = {"CRIT": "CRITICAL", "NORMAL": "MEDIUM"}.get(m.group(1), m.group(1))
    else:
        # A misspelled priority and an absent one are different mistakes and deserve
        # different messages: "MED" is a typo to correct, an empty slot is a field to fill.
        first = re.match(r"\s*([A-Za-z]{3,12})\s*[,)]", env)
        if first:
            pri_typo = first.group(1)

    # STATUS rides in the same trailing block as the priority (docs/BACKLOG_FORMAT.md §2).
    # It duplicates the section on purpose: the duplication is what lets --check catch an
    # entry whose heading and section disagree, and what keeps a retrieval chunk legible
    # once it has been torn away from its section heading.
    # Status is read from the metadata part of the envelope only — everything before the
    # em dash. Searching the whole envelope read the word "closed" out of a quoted origin
    # ("Voting already closed (approved)") and reported a status the author never wrote.
    # Warren flagged it twice before it was fixed.
    meta = env.split("—", 1)[0]
    sm = re.search(r"\b(open|in-progress|done-awaiting-observation|closed)\b", meta)
    status = sm.group(1) if sm else ""

    # Origin is whatever follows the em dash inside the envelope: who raised it.
    om = re.search(r"—\s*(.+)$", env, re.DOTALL)
    origin = om.group(1).strip() if om else ""

    dm = re.search(r"(20\d\d-\d\d-\d\d)", head)
    when = dm.group(1) if dm else ""

    # Strip the trailing envelope so the description reads clean. Uses the balanced-paren
    # result above, so a quoted aside inside the origin no longer survives into the desc.
    desc = rest
    if env and desc.rstrip().endswith(f"({env})"):
        desc = desc.rstrip()[: -len(env) - 2]
    elif pri != "—":
        desc = re.sub(r"\s*\((?:CRIT|CRITICAL|HIGH|MEDIUM|LOW|RESEARCH|NORMAL)[^)]*\)\s*$", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    # First body bullet — the entry's own opening line, not a summary I invent.
    first = ""
    for j in range(i + 1, min(i + 8, len(lines))):
        s = lines[j].strip()
        if s.startswith("###") or s.startswith("##"):
            break
        if s.startswith("- "):
            first = re.sub(r"\s+", " ", s[2:]).strip()
            break

    entries.append({
        "section": section, "name": name, "desc": desc, "pri": pri, "pri_typo": pri_typo,
        "when": when, "first": first, "line": i + 1,
        "status": status, "origin": origin, "head": head, "env": env,
        "body": "\n".join(lines[i + 1:i + 40]).split("\n### ")[0],
        "done": "✅" in head, "part": "🟡" in head,
    })

if "--check" in sys.argv:
    # Validate the file against docs/BACKLOG_FORMAT.md. Reports, never rewrites:
    # every automated classifier in this repo's history has documented its own false
    # positives, so the script's job is to point, not to edit.
    CUTOFF = "2026-08-10"          # BACKLOG_FORMAT.md §6 — envelope required from here on
    RESOLUTIONS = {"fixed", "disproved", "moot", "superseded", "duplicate", "wontfix"}
    SECTION_STATUS = {
        "OPEN": "open",
        "IN PROGRESS": "in-progress",
        "DONE": "done-awaiting-observation",
        "BLOCKED": "open",
        "BACKLOG": "open",
        "IDEAS": "open",
    }

    # BACKLOG_FORMAT.md §5 promises that a file with its own section names declares the
    # mapping in front matter rather than renaming. Without this the checker only ever
    # worked for one project, which is not what the standard says.
    fm = re.match(r"^---\n(.*?)\n---\n", "\n".join(lines), re.DOTALL)
    if fm:
        in_sections = False
        for raw in fm.group(1).split("\n"):
            if re.match(r"^sections:\s*$", raw):
                in_sections = True
                continue
            if in_sections:
                pair = re.match(r'^\s+"?([^":]+)"?\s*:\s*(\S+)\s*$', raw)
                if pair:
                    SECTION_STATUS[pair.group(1).strip().upper()] = pair.group(2)
                    continue
                in_sections = False

    def canonical(sec: str) -> str | None:
        for key, st in SECTION_STATUS.items():
            if sec.upper().startswith(key):
                return st
        return None

    # The non-English rule cannot be proved from the markdown fixture: an entry
    # demonstrating it would have to carry the very thing these files must not carry.
    # Assert it here instead, against a string built from codepoint escapes, so the rule
    # is watched to fire without a single non-Latin character living in the repository.
    _NON_LATIN = "\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"   # "description", Cyrillic
    assert re.search(r"[\u0400-\u04ff]", _NON_LATIN), "non-English detector is broken"
    assert not re.search(r"[\u0400-\u04ff]", "plain ascii description"), \
        "non-English detector fires on Latin text"

    refusals, warnings = [], []

    def at(e, msg, bucket):
        bucket.append(f"{SRC.name}:{e['line']}  {e['name']}\n    {msg}")

    seen = {}
    for e in entries:
        sec_status = canonical(e["section"])
        # Migration is new-entries-only (§7), so a refusal on legacy content would leave
        # exit 1 permanently on and mean nothing. Post-cutoff entries are refused;
        # everything older warns, and the warning count is the migration debt meter.
        new = bool(e["when"]) and e["when"] >= CUTOFF
        hard = refusals if new else warnings

        if sec_status is None:
            at(e, f"section «{e['section']}» is not one of the recognised lifecycle "
                  f"sections and no front-matter mapping covers it (§2, §5)", hard)

        if e["name"] in seen:
            at(e, f"duplicate name — already used at {SRC.name}:{seen[e['name']]}", hard)
        else:
            seen[e["name"]] = e["line"]

        if e["status"] and sec_status and e["status"] != sec_status:
            at(e, f"heading says status «{e['status']}» but the section implies "
                  f"«{sec_status}» — one of the two is wrong", hard)

        # A closure line anywhere in the body must carry a known resolution (§3).
        for cl in re.findall(r"\*\*Closed:\*\*(.+)", e["body"]):
            tokens = {t.strip().lower() for t in cl.split("·")}
            if not tokens & RESOLUTIONS:
                at(e, f"closure line carries no known resolution "
                      f"({'/'.join(sorted(RESOLUTIONS))}): «{cl.strip()[:70]}»", hard)

        if new:
            missing = [f for f, v in (("priority", e["pri"] != "—" or e["pri_typo"]),
                                      ("status", e["status"]),
                                      ("origin", e["origin"])) if not v]
            if e["pri_typo"]:
                at(e, f"priority token «{e['pri_typo']}» is not in the vocabulary "
                      f"(CRITICAL / HIGH / MEDIUM / LOW / RESEARCH) — a misspelled "
                      f"priority reads as no priority at all", refusals)
            if missing:
                # Name the expected shape. "missing origin" on its own reads as a puzzle
                # when the real cause is a hyphen where the envelope wants an em dash.
                at(e, f"entry dated {e['when']} (on/after the {CUTOFF} cutoff) is missing: "
                      f"{', '.join(missing)}. Expected "
                      f"(PRIORITY, STATUS, YYYY-MM-DD — origin), with an em dash "
                      f"before the origin", refusals)
            # A refusal, not a warning: Mike, 2026-08-10 - "Cyrillic in new entries is a
            # refusal". Only the name and description are checked. The origin quotes whoever
            # raised the entry in their own words (§1), and those words are often Russian -
            # checking them would have the standard contradict itself.
            checked = e["head"][: len(e["head"]) - len(e["env"])] if e["env"] else e["head"]
            if re.search(r"[\u0400-\u04ff]", checked):
                at(e, "Cyrillic in the name or description of a post-cutoff entry — new "
                      "entries are written in English (§6); the origin quote is exempt",
                   refusals)
        else:
            missing = [f for f, v in (("priority", e["pri"] != "—"),
                                      ("date", e["when"]),
                                      ("origin", e["origin"])) if not v]
            if missing:
                at(e, f"missing {', '.join(missing)} (pre-cutoff entry, not required)", warnings)

    for line in refusals:
        print(f"REFUSE  {line}")
    for line in warnings:
        print(f"warn    {line}")
    print(f"\n{len(entries)} entries · {len(refusals)} refusals · {len(warnings)} warnings")
    print(f"Rules: docs/BACKLOG_FORMAT.md §8. Cutoff for the required envelope: {CUTOFF}.")
    sys.exit(1 if refusals else 0)

order = ["OPEN", "IN PROGRESS", "DONE", "BACKLOG", "IDEAS"]
def sec_rank(s):
    for k, key in enumerate(order):
        if s.startswith(key):
            return k
    return len(order)

sections = sorted({e["section"] for e in entries}, key=sec_rank)
by_pri = Counter(e["pri"] for e in entries)
shelf = sum(1 for e in entries if e["section"].startswith("DONE"))
open_n = sum(1 for e in entries if e["section"] == "OPEN")
crit_high = sum(1 for e in entries if e["pri"] in ("CRITICAL", "HIGH")
                and e["section"] in ("OPEN", "IN PROGRESS"))
dated = [e["when"] for e in entries if e["when"]]
oldest = min(dated) if dated else "—"

esc = html.escape


def md(text: str) -> str:
    """Escape, then honour the only two markdown marks the backlog uses in prose."""
    out = esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def chip(pri):
    return f'<span class="chip {PRI_CLASS.get(pri, "none")}">{esc(pri)}</span>'


rows = []
for s in sections:
    items = [e for e in entries if e["section"] == s]
    items.sort(key=lambda e: (PRIORITIES.index(e["pri"]) if e["pri"] in PRIORITIES else 9,
                              e["when"] or "0000"), reverse=False)
    rows.append(f'<section class="sec" data-sec="{esc(s)}">')
    rows.append(f'<h2>{esc(s)} <span class="cnt">{len(items)}</span></h2>')
    mix = Counter(e["pri"] for e in items)
    bar = "".join(
        f'<span class="seg {PRI_CLASS.get(p, "none")}" style="flex:{mix[p]}" '
        f'title="{esc(p)}: {mix[p]}"></span>'
        for p in PRIORITIES if mix.get(p)
    )
    legend = " · ".join(f"{esc(p)} {mix[p]}" for p in PRIORITIES if mix.get(p))
    rows.append(f'<div class="bar" role="img" aria-label="{esc(legend)}">{bar}</div>')
    rows.append(f'<p class="legend">{legend}</p>')
    rows.append('<ul class="list">')
    for e in items:
        mark = ' <span class="mark done">done</span>' if e["done"] else (
               ' <span class="mark part">partial</span>' if e["part"] else "")
        when = f'<time>{esc(e["when"])}</time>' if e["when"] else ""
        desc = f'<p class="d">{md(e["desc"])}</p>' if e["desc"] else ""
        cut = e["first"][:300] + ("…" if len(e["first"]) > 300 else "")
        first = f'<p class="f">{md(cut)}</p>' if e["first"] else ""
        rows.append(
            f'<li class="item" data-pri="{esc(e["pri"])}" '
            f'data-q="{esc((e["name"] + " " + e["desc"]).lower())}">'
            f'<div class="hd">{chip(e["pri"])}<code>{esc(e["name"])}</code>{mark}{when}</div>'
            f'{desc}{first}</li>'
        )
    rows.append("</ul></section>")

body = "\n".join(rows)
today = date.today().isoformat()

CSS = """
:root{--ground:#eff1f5;--surface:#fff;--sunk:#e6e9ef;--line:#ccd0da;--soft:#dce0e8;
--ink:#4c4f69;--ink-str:#2f3145;--ink-mut:#6c6f85;--ink-faint:#8c8fa1;
--accent:#1976d2;--accent-sunk:#e3edf9;
--crit:#c11135;--crit-sunk:#f8e2e7;--high:#b3730f;--high-sunk:#f6ecda;
--med:#1976d2;--med-sunk:#e3edf9;--low:#7c7f93;--low-sunk:#e6e7ee;
--res:#7847b0;--res-sunk:#eee6f6;--none:#9ca0b0;--none-sunk:#e9eaef;
--ok:#2f8f22;--ok-sunk:#e2f0de;
--sans:"Segoe UI Variable Text","Segoe UI",-apple-system,BlinkMacSystemFont,"Noto Sans",sans-serif;
--mono:ui-monospace,"Cascadia Mono","JetBrains Mono",Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#1e1e2e;--surface:#252537;--sunk:#181825;--line:#3b3b52;--soft:#2e2e42;
--ink:#cdd6f4;--ink-str:#e6ebf7;--ink-mut:#a6adc8;--ink-faint:#7f849c;
--accent:#89b4fa;--accent-sunk:#24304a;
--crit:#f38ba8;--crit-sunk:#3a2029;--high:#f9e2af;--high-sunk:#33301c;
--med:#89b4fa;--med-sunk:#24304a;--low:#9399b2;--low-sunk:#2a2a3c;
--res:#cba6f7;--res-sunk:#2e2540;--none:#7f849c;--none-sunk:#282838;
--ok:#a6e3a1;--ok-sunk:#22321f}}
:root[data-theme="dark"]{
--ground:#1e1e2e;--surface:#252537;--sunk:#181825;--line:#3b3b52;--soft:#2e2e42;
--ink:#cdd6f4;--ink-str:#e6ebf7;--ink-mut:#a6adc8;--ink-faint:#7f849c;
--accent:#89b4fa;--accent-sunk:#24304a;
--crit:#f38ba8;--crit-sunk:#3a2029;--high:#f9e2af;--high-sunk:#33301c;
--med:#89b4fa;--med-sunk:#24304a;--low:#9399b2;--low-sunk:#2a2a3c;
--res:#cba6f7;--res-sunk:#2e2540;--none:#7f849c;--none-sunk:#282838;
--ok:#a6e3a1;--ok-sunk:#22321f}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
font-size:1rem;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:clamp(1.4rem,4vw,3rem) clamp(1rem,4vw,2.2rem) 5rem}
header.mast{border-bottom:2px solid var(--ink-str);padding-bottom:1.1rem}
h1{margin:0;font-size:clamp(1.8rem,1.5rem+1.2vw,2.7rem);letter-spacing:-.02em;color:var(--ink-str)}
.meta{margin-top:.5rem;font-family:var(--mono);font-size:.8rem;color:var(--ink-mut);
font-variant-numeric:tabular-nums;display:flex;flex-wrap:wrap;gap:.3rem 1.3rem}
.meta b{color:var(--ink-str)}
.stats{display:grid;gap:.9rem;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:1.6rem 0 0}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:1rem 1.1rem}
.stat .n{display:block;font-size:clamp(2.1rem,1.8rem+1.2vw,2.9rem);line-height:1;font-weight:300;
letter-spacing:-.03em;color:var(--ink-str);font-variant-numeric:tabular-nums}
.stat .n.warnval{color:var(--high)}.stat .n.critval{color:var(--crit)}
.stat .lab{display:block;margin-top:.45rem;font-size:.85rem;color:var(--ink-mut)}
.controls{position:sticky;top:0;z-index:5;background:var(--ground);padding:1rem 0 .8rem;
margin-top:1.6rem;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
.controls input{flex:1 1 210px;min-width:170px;padding:.45rem .65rem;border:1px solid var(--line);
border-radius:5px;background:var(--surface);color:var(--ink);font:inherit;font-size:.9rem}
.fbtn{font-family:var(--mono);font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;
padding:.35rem .6rem;border-radius:4px;border:1px solid var(--line);background:var(--surface);
color:var(--ink-mut);cursor:pointer}
.fbtn[aria-pressed="true"]{background:var(--accent-sunk);border-color:var(--accent);color:var(--accent);font-weight:600}
.sec{margin-top:2.6rem}
.sec h2{font-size:1.35rem;margin:0 0 .5rem;color:var(--ink-str);letter-spacing:-.01em}
.sec h2 .cnt{font-family:var(--mono);font-size:.9rem;color:var(--ink-faint);font-variant-numeric:tabular-nums}
.bar{display:flex;gap:2px;height:8px;border-radius:2px;overflow:hidden;margin:.2rem 0 .4rem}
.seg{display:block;min-width:3px}
.seg.crit{background:var(--crit)}.seg.high{background:var(--high)}.seg.med{background:var(--med)}
.seg.low{background:var(--low)}.seg.res{background:var(--res)}.seg.none{background:var(--none)}
.legend{margin:0 0 .9rem;font-family:var(--mono);font-size:.72rem;color:var(--ink-faint);
font-variant-numeric:tabular-nums}
.list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.55rem}
.item{background:var(--surface);border:1px solid var(--line);border-radius:5px;padding:.7rem .9rem}
.item .hd{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem}
.item code{font-family:var(--mono);font-size:.84rem;color:var(--ink-str);font-weight:600;
background:none;padding:0;word-break:break-word}
.item time{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--ink-faint);
font-variant-numeric:tabular-nums}
.item .d{margin:.35rem 0 0;font-size:.9rem;color:var(--ink)}
.item .f{margin:.3rem 0 0;font-size:.82rem;color:var(--ink-mut)}
.chip{font-family:var(--mono);font-size:.66rem;letter-spacing:.07em;font-weight:700;
padding:.16rem .42rem;border-radius:3px;border:1px solid;white-space:nowrap}
.chip.crit{color:var(--crit);background:var(--crit-sunk);border-color:var(--crit)}
.chip.high{color:var(--high);background:var(--high-sunk);border-color:var(--high)}
.chip.med{color:var(--med);background:var(--med-sunk);border-color:var(--med)}
.chip.low{color:var(--low);background:var(--low-sunk);border-color:var(--low)}
.chip.res{color:var(--res);background:var(--res-sunk);border-color:var(--res)}
.chip.none{color:var(--none);background:var(--none-sunk);border-color:var(--none)}
.mark{font-family:var(--mono);font-size:.66rem;letter-spacing:.06em;text-transform:uppercase;
padding:.16rem .42rem;border-radius:3px}
.mark.done{color:var(--ok);background:var(--ok-sunk)}
.mark.part{color:var(--high);background:var(--high-sunk)}
.note{border-left:3px solid var(--accent);background:var(--accent-sunk);padding:.9rem 1.1rem;
border-radius:0 5px 5px 0;margin-top:1.4rem;font-size:.92rem}
.note p{margin:0}.note p+p{margin-top:.5rem}
footer{margin-top:3.5rem;padding-top:1.2rem;border-top:1px solid var(--line);
font-size:.82rem;color:var(--ink-faint)}
.hidden{display:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

JS = """
const q=document.getElementById('q'),btns=[...document.querySelectorAll('.fbtn')];
let pri=new Set();
function apply(){
  const t=q.value.trim().toLowerCase();
  document.querySelectorAll('.sec').forEach(sec=>{
    let shown=0;
    sec.querySelectorAll('.item').forEach(it=>{
      const okP=pri.size===0||pri.has(it.dataset.pri);
      const okQ=!t||it.dataset.q.includes(t);
      const ok=okP&&okQ; it.classList.toggle('hidden',!ok); if(ok)shown++;
    });
    sec.classList.toggle('hidden',shown===0);
  });
}
q.addEventListener('input',apply);
btns.forEach(b=>b.addEventListener('click',()=>{
  const p=b.dataset.pri;
  if(pri.has(p)){pri.delete(p);b.setAttribute('aria-pressed','false');}
  else{pri.add(p);b.setAttribute('aria-pressed','true');}
  apply();
}));
"""

filters = "".join(
    f'<button class="fbtn" type="button" data-pri="{esc(p)}" aria-pressed="false">'
    f'{esc(p)} {by_pri.get(p, 0)}</button>'
    for p in PRIORITIES if by_pri.get(p)
)

doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>D-PC Messenger — Backlog board</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Ctext y=%2714%27 font-size=%2714%27%3E%F0%9F%93%8B%3C/text%3E%3C/svg%3E">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="mast">
  <h1>Backlog board</h1>
  <div class="meta">
    <span>built <b>{today}</b> from <b>backlog.md</b></span>
    <span><b>{len(entries)}</b> entries</span>
    <span>oldest <b>{esc(oldest)}</b></span>
  </div>
</header>

<div class="stats">
  <div class="stat"><span class="n critval">{crit_high}</span>
    <span class="lab"><b>CRITICAL and HIGH</b> entries open or in progress — what is burning</span></div>
  <div class="stat"><span class="n warnval">{shelf}</span>
    <span class="lab">on the <b>done, awaiting observation</b> shelf — code ran ahead of proof</span></div>
  <div class="stat"><span class="n">{open_n}</span>
    <span class="lab">open and not started</span></div>
</div>

<div class="note">
  <p><strong>The observation shelf is this board's headline number.</strong> It holds work that
  is committed and covered by tests but has never once been watched running on a live system.
  An entry moves to <code>backlog_closed.md</code> only after it has been observed in production —
  "the code is written" is not grounds.</p>
</div>

<div class="controls">
  <input id="q" type="search" placeholder="search name and description" aria-label="Search entries">
  {filters}
</div>

{body}

<footer>
  <p>Built by <code>tools/backlog/build.py</code> from <code>backlog.md</code>; priority, date
  and the done / partial marks are read from entry headings, nothing is inferred.
  Rebuild after editing the backlog: <code>uv run python tools/backlog/build.py</code>.
  Check it against the standard: <code>build.py --check</code> (docs/BACKLOG_FORMAT.md).</p>
  <p>Priority was recognised on {sum(v for k, v in by_pri.items() if k != '—')} of {len(entries)}
  entries; the rest carry none in their heading and are marked "—" rather than guessed into a level.</p>
</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""

DST.write_text(doc, encoding="utf-8")
print(f"entries: {len(entries)}  ->  {DST}  ({len(doc)} chars)")
print("sections:", {s: sum(1 for e in entries if e['section'] == s) for s in sections})
print("priorities:", dict(by_pri))
