"""Calibration harness for pattern_analysis.

Reads a labeled corpus and, per sample, dumps the RAW measurements behind each
marker plus several dispersion candidates for burstiness, then aggregates by
class so we can see which measure separates human vs AI and where the reference
pivots (RCV_REF/OPENER_REF/PUNCT_REF/LEX_REF) should sit. No changes to signals.py.

Usage:  PYTHONPATH=<project> python eval_harness.py <corpus.json>
Corpus: [{"id","label":"human"|"ai","group","source","text"}, ...]
"""
import json
import statistics
import sys

import signals as S


def dispersions(wps):
    n = len(wps)
    mean = statistics.mean(wps)
    out = {"n": n, "mean": round(mean, 2)}
    if n >= 2 and mean:
        stdev = statistics.stdev(wps)
        out["cv"] = round(stdev / mean, 3)                       # current measure
        med = statistics.median(wps)
        mad = statistics.median([abs(x - med) for x in wps])
        out["rcv"] = round(mad / med, 3) if med else 0.0         # robust CV = MAD/median
        q1, _, q3 = statistics.quantiles(wps, n=4, method="inclusive")
        out["qcd"] = round((q3 - q1) / (q3 + q1), 3) if (q3 + q1) else 0.0  # quartile coef.
    else:
        out["cv"] = out["rcv"] = out["qcd"] = None
    return out


def raw_markers(text):
    sents = [s for s in (p.strip() for p in S._SENTENCE_SPLIT.split(text)) if s]
    words = S._WORD.findall(text.lower())
    wps = [len(S._WORD.findall(s)) for s in sents]
    sent_words = [S._WORD.findall(s.lower()) for s in sents]
    openers = [tuple(w[:2]) for w in sent_words if len(w) >= 2]
    opener_rep = 1 - len(set(openers)) / len(openers) if openers else 0.0
    bigrams = list(zip(words, words[1:]))
    bigram_rep = 1 - len(set(bigrams)) / len(bigrams) if bigrams else 0.0
    marks = text.count("—") + text.count("--") + text.count("–") + text.count(";")
    punct_rate = marks / len(sents) if sents else 0.0
    low = text.lower()
    lex_hits = sum(1 for w in words if w in S.AI_LEXICON)
    phrase_hits = sum(low.count(p) for p in S.AI_PHRASES)
    opener_hits = len(S._ING_OPENER_RE.findall(text))
    lex_rate = 1000.0 * (lex_hits + phrase_hits + opener_hits) / len(words) if words else 0.0
    return sents, words, wps, opener_rep, bigram_rep, marks, punct_rate, lex_rate


def main(path):
    corpus = json.load(open(path))
    rows = []
    print(f"{'id':16} {'lbl':5} {'sent':4} {'word':4} {'gate':4} "
          f"{'CV':>6} {'rCV':>6} {'QCD':>6} {'oprep':>6} {'birep':>6} {'pmark':>6} {'lexr':>6} "
          f"{'patt':>6}  pattern_markers")
    for s in corpus:
        sents, words, wps, oprep, birep, marks, prate, lexr = raw_markers(s["text"])
        gated = len(words) < S.MIN_STABLE_WORDS or len(sents) < S.MIN_STABLE_SENTENCES
        disp = dispersions(wps) if wps else {"cv": None, "rcv": None, "qcd": None}
        r = S.run_pattern_analysis(s["text"])
        rows.append({"label": s["label"], "gated": gated, "sents": len(sents), **disp,
                     "oprep": oprep, "birep": birep, "prate": prate, "lexr": lexr,
                     "pattern": r["pattern_score"]})
        def f(x):
            return f"{x:6.3f}" if isinstance(x, (int, float)) else f"{'--':>6}"
        print(f"{s['id'][:16]:16} {s['label'][:5]:5} {len(sents):4d} {len(words):4d} "
              f"{'Y' if gated else 'n':>4} {f(disp['cv'])} {f(disp['rcv'])} {f(disp['qcd'])} "
              f"{oprep:6.3f} {birep:6.3f} {prate:6.3f} {lexr:6.3f} {r['pattern_score']:6.3f}  "
              f"{r['pattern_markers']}")

    print("\n--- class separation by length bucket (calibrate on the long bucket) ---")
    buckets = [("long >=8 sents", lambda r: r["sents"] >= 8),
               ("mid 3-7 sents", lambda r: 3 <= r["sents"] <= 7)]
    for bname, pred in buckets:
        print(f"\n[{bname}]")
        for measure in ("cv", "rcv", "qcd", "oprep", "birep", "prate", "lexr", "pattern"):
            line = f"  {measure:7}"
            for lbl in ("human", "ai"):
                vals = [r[measure] for r in rows
                        if r["label"] == lbl and pred(r) and not r["gated"]
                        and r.get(measure) is not None]
                if vals:
                    line += (f"  {lbl:5} n={len(vals):2d} med={statistics.median(vals):.3f} "
                             f"[{min(vals):.3f},{max(vals):.3f}]")
                else:
                    line += f"  {lbl:5} --"
            print(line)

    gated = [r for r in rows if r["gated"]]
    print(f"\n[short/gated: {len(gated)} samples -> pattern forced to 0.5 neutral by design; "
          f"validate these scale down neutrally, not confidently wrong]")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "corpus.json")
