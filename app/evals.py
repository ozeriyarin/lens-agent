"""Evals: measure the agent on a labelled photo set (constitution V, feature 006).

Why this module exists: unit tests prove the *code* behaves; they say nothing about
whether the *model* finds the mug. This is the other half. A set of photos with the
objects the owner expects, a scorer, and a runner that pushes every photo through the
same path `/scan` uses, then writes a report. Change the instructions or the model,
run it, compare the numbers.

    uv run python -m app.evals run                    # real models, writes eval/runs/<ts>.json
    uv run python -m app.evals run --model test       # offline, fake model
    uv run python -m app.evals bootstrap              # draft labels for new photos in EVAL_PHOTOS_DIR
    uv run python -m app.evals compare A.json B.json  # metric deltas

Design (plan.md): labels live in the repo (eval/set.jsonl), photos outside it
(EVAL_PHOTOS_DIR); matching is alias-based and visible; unlabelled extras are reported,
not penalised; the runner uses its own ledger file under the same monthly cap.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model

from app.agent import INSTRUCTIONS, LIVE_HINT, SCAN_MODEL_SETTINGS, AgentFailed, build_agent
from app.budget import Budget, BudgetExceeded
from app.config import Settings
from app.embeddings import Embeddings, hashing_embed
from app.agent import lookup_toolset
from app.gate import BadImage, prepare
from app.kb import KnowledgeBase
from app.memory import Memory
from app.router import Router
from app.schema import BBox, DetectedObject, ScanResult

DEFAULT_SET = Path("eval/set.jsonl")
DEFAULT_RUNS = Path("eval/runs")
DEFAULT_LEDGER = Path("eval/ledger.db")
DEFAULT_KB = Path("eval/kb")  # sample notes committed with the set (005)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def photos_dir() -> Path:
    return Path(os.getenv("EVAL_PHOTOS_DIR", "C:/ai-projects/lens-agent-eval-photos"))


# --- the set ---------------------------------------------------------------------------


@dataclass
class Expected:
    label: str
    aliases: list[str] = field(default_factory=list)
    category: str | None = None
    box: list[int] | None = None  # [ymin, xmin, ymax, xmax] on 0..1000, like the model's box_2d
    entity: str | None = None  # feature 004: group id shared by the *same physical object* across photos

    def names(self) -> list[str]:
        return [self.label, *self.aliases]


@dataclass
class EvalItem:
    id: str
    file: str
    expect: list[Expected] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    question: str | None = None
    expect_answer_contains: str | None = None
    source_expected: str | None = None  # 005: a note file the answer must cite
    notes: str | None = None
    draft: bool = False

    @classmethod
    def from_json(cls, d: dict) -> "EvalItem":
        return cls(
            id=d["id"],
            file=d["file"],
            expect=[Expected(**e) for e in d.get("expect", [])],
            forbid=list(d.get("forbid", [])),
            question=d.get("question"),
            expect_answer_contains=d.get("expect_answer_contains"),
            source_expected=d.get("source_expected"),
            notes=d.get("notes"),
            draft=bool(d.get("draft", False)),
        )

    def to_json(self) -> dict:
        d = {"id": self.id, "file": self.file, "expect": [
            {k: v for k, v in asdict(e).items() if v not in (None, [])} for e in self.expect]}
        if self.forbid:
            d["forbid"] = self.forbid
        for k in ("question", "expect_answer_contains", "source_expected", "notes"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.draft:
            d["draft"] = True
        return d


def load_set(path: Path = DEFAULT_SET) -> list[EvalItem]:
    """One JSON object per line; blank lines and `#` comments allowed. Drafts are kept
    (so bootstrap can see them) but `scorable()` leaves them out."""
    items: list[EvalItem] = []
    if not path.exists():
        return items
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(EvalItem.from_json(json.loads(line)))
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"{path}:{n}: {e}") from e
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate ids")
    return items


def scorable(items: list[EvalItem]) -> list[EvalItem]:
    return [i for i in items if not i.draft]


# --- matching ----------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def label_matches(predicted: str, name: str) -> bool:
    """`name` equals the predicted label, or appears in it as whole words
    ("coffee mug" matches "mug"; "mugshot" does not)."""
    p, n = _norm(predicted), _norm(name)
    if not n:
        return False
    return p == n or re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", p) is not None


def match(predictions: list[DetectedObject], item: EvalItem) -> tuple[list[tuple[int, int]], list[str], list[int]]:
    """Greedy one-to-one matching. Returns (pairs of (expected idx, predicted idx),
    forbidden labels that were predicted, indices of unmatched predictions)."""
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    # Exact label equality first, then word-boundary containment: "mug" should take the
    # prediction literally called "mug" before it steals "coffee mug" from another entry.
    for strict in (True, False):
        for ei, exp in enumerate(item.expect):
            if any(ei == p[0] for p in pairs):
                continue
            for pi, pred in enumerate(predictions):
                if pi in used:
                    continue
                if exp.category and pred.category != exp.category:
                    continue
                ok = any(_norm(pred.label) == _norm(n) for n in exp.names()) if strict else any(
                    label_matches(pred.label, n) for n in exp.names())
                if ok:
                    pairs.append((ei, pi))
                    used.add(pi)
                    break
    forbid_hits = [f for f in item.forbid if any(label_matches(p.label, f) for p in predictions)]
    extras = [pi for pi in range(len(predictions)) if pi not in used]
    return pairs, forbid_hits, extras


def box_to_bbox(box: list[int]) -> BBox:
    ymin, xmin, ymax, xmax = box
    return BBox(x=xmin / 1000, y=ymin / 1000, w=(xmax - xmin) / 1000, h=(ymax - ymin) / 1000)


def iou(a: BBox, b: BBox) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


# --- scoring -----------------------------------------------------------------------------


@dataclass
class PhotoScore:
    id: str
    file: str
    expected: int
    matched: int
    missed: list[str]
    forbid_hits: list[str]
    extras: list[str]
    recall: float
    iou_mean: float | None
    answer_hit: bool | None
    latency_ms: int
    cost_usd: float
    escalated: bool
    model: str
    error: str | None = None
    identity_rate: float | None = None  # 004: share of non-person predictions that carry an identity
    source_hit: bool | None = None  # 005: the expected note file was cited
    sources: list[str] = field(default_factory=list)
    entity_links: dict[str, str] = field(default_factory=dict)  # 004: expected entity group -> memory entity id


def score_photo(item: EvalItem, result: ScanResult | None, *, latency_ms: int = 0, cost_usd: float = 0.0,
                escalated: bool = False, model: str = "", error: str | None = None,
                entity_ids: list[str | None] | None = None) -> PhotoScore:
    n = len(item.expect)
    if result is None:
        return PhotoScore(item.id, item.file, n, 0, [e.label for e in item.expect], [], [], 0.0, None, None,
                          latency_ms, cost_usd, escalated, model, error or "no result")
    pairs, forbid_hits, extras = match(result.objects, item)
    non_people = [o for o in result.objects if o.category != "person"]
    identity_rate = (sum(1 for o in non_people if o.identity) / len(non_people)) if non_people else None
    links: dict[str, str] = {}
    if entity_ids:
        for ei, pi in pairs:
            group = item.expect[ei].entity
            if group and pi < len(entity_ids) and entity_ids[pi]:
                links[group] = entity_ids[pi]
    matched_e = {ei for ei, _ in pairs}
    ious = [iou(result.objects[pi].bbox, box_to_bbox(item.expect[ei].box))
            for ei, pi in pairs if item.expect[ei].box and result.objects[pi].bbox]
    answer_hit = None
    if item.expect_answer_contains:
        text = " ".join([result.answer or "", *(o.answer or "" for o in result.objects)]).lower()
        answer_hit = item.expect_answer_contains.lower() in text
    return PhotoScore(
        id=item.id, file=item.file, expected=n, matched=len(pairs),
        missed=[e.label for i, e in enumerate(item.expect) if i not in matched_e],
        forbid_hits=forbid_hits, extras=[result.objects[pi].label for pi in extras],
        recall=(len(pairs) / n) if n else 1.0,
        iou_mean=(sum(ious) / len(ious)) if ious else None,
        answer_hit=answer_hit, latency_ms=latency_ms, cost_usd=cost_usd, escalated=escalated, model=model,
        identity_rate=identity_rate, entity_links=links,
        source_hit=(any(src.file == item.source_expected for src in result.sources) if item.source_expected else None),
        sources=[f"{src.file} › {src.heading}" for src in result.sources],
    )


def identity_metrics(scores: list[PhotoScore]) -> dict:
    """004 FR-9. identity_recall: for each entity group seen in >= 2 photos, did memory give
    the same entity id each time (fraction of photo pairs that agree)? identity_precision:
    are different groups kept apart (fraction of cross-group pairs with different ids)?"""
    by_group: dict[str, list[str]] = {}
    for s in scores:
        for group, eid in s.entity_links.items():
            by_group.setdefault(group, []).append(eid)
    same_pairs = agree = 0
    for ids in by_group.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                same_pairs += 1
                agree += ids[i] == ids[j]
    groups = list(by_group)
    cross_pairs = apart = 0
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            for a in by_group[groups[i]]:
                for b in by_group[groups[j]]:
                    cross_pairs += 1
                    apart += a != b
    return {
        "entity_groups": len(groups),
        "identity_recall": round(agree / same_pairs, 4) if same_pairs else None,
        "identity_precision": round(apart / cross_pairs, 4) if cross_pairs else None,
    }


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return float(s[k])


def summarize(scores: list[PhotoScore]) -> dict:
    ok = [s for s in scores if s.error is None]
    ious = [s.iou_mean for s in ok if s.iou_mean is not None]
    answers = [s.answer_hit for s in ok if s.answer_hit is not None]
    src_hits = [s.source_hit for s in ok if s.source_hit is not None]
    lat = [float(s.latency_ms) for s in ok]
    id_rates = [s.identity_rate for s in ok if s.identity_rate is not None]
    return {
        "photos": len(scores),
        "errors": len(scores) - len(ok),
        "mean_recall": round(statistics.fmean(s.recall for s in scores), 4) if scores else 0.0,
        "identity_rate": round(statistics.fmean(id_rates), 4) if id_rates else None,
        **identity_metrics(scores),
        "forbid_hit_rate": round(sum(1 for s in ok if s.forbid_hits) / len(ok), 4) if ok else 0.0,
        "iou_mean": round(statistics.fmean(ious), 4) if ious else None,
        "iou_photos": len(ious),
        "answer_accuracy": round(sum(answers) / len(answers), 4) if answers else None,
        "source_accuracy": round(sum(src_hits) / len(src_hits), 4) if src_hits else None,
        "lookups": sum(1 for s in ok if s.sources),
        "escalation_rate": round(sum(1 for s in ok if s.escalated) / len(ok), 4) if ok else 0.0,
        "mean_cost_usd": round(statistics.fmean(s.cost_usd for s in ok), 6) if ok else 0.0,
        "total_cost_usd": round(sum(s.cost_usd for s in ok), 5),
        "latency_p50_ms": int(_pct(lat, 0.5)),
        "latency_p95_ms": int(_pct(lat, 0.95)),
        "failures": [{"id": s.id, "missed": s.missed, "forbid_hits": s.forbid_hits, "error": s.error}
                     for s in scores if s.missed or s.forbid_hits or s.error],
    }


# --- running -----------------------------------------------------------------------------


def _demo_model() -> Model:
    from pydantic_ai.models.test import TestModel

    return TestModel(custom_output_args={"scene": "demo desk", "objects": [
        {"label": "laptop", "category": "electronics", "confidence": 0.95, "box_2d": [200, 100, 600, 600]},
        {"label": "coffee mug", "category": "other", "confidence": 0.8}]})


def make_router(settings: Settings, model: str | Model | None, ledger: Path, toolset=None) -> Router:
    """Same composition as the app: primary + optional escalation, under the monthly cap."""
    budget = Budget(ledger, settings.budget_usd)
    chosen = model if model is not None else settings.model
    if chosen == "test":
        chosen = _demo_model()
    primary_name = chosen if isinstance(chosen, str) else getattr(chosen, "model_name", "model")
    escalation, esc_name = None, ""
    if isinstance(chosen, str) and settings.escalation_model:
        try:
            escalation, esc_name = build_agent(settings.escalation_model), settings.escalation_model
        except UserError:
            pass  # no key for the second tier: run without it, the report says which model ran
    return Router(budget, build_agent(chosen), primary_name, escalation, esc_name, settings.escalate_below, toolset=toolset)


def settings_fingerprint() -> str:
    """So two reports can be compared: same instructions + settings => same fingerprint."""
    blob = json.dumps({"instructions": INSTRUCTIONS, "live_hint": LIVE_HINT, "settings": SCAN_MODEL_SETTINGS}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


async def run(*, set_path: Path = DEFAULT_SET, photos: Path | None = None, model: str | Model | None = None,
              limit: int | None = None, tag: str = "", settings: Settings | None = None,
              ledger: Path = DEFAULT_LEDGER, runs_dir: Path = DEFAULT_RUNS, write: bool = True,
              kb_dir: Path | None = DEFAULT_KB) -> dict:
    settings = settings or Settings.from_env()
    photos = photos or photos_dir()
    items = scorable(load_set(set_path))
    if limit:
        items = items[:limit]
    if not items:
        raise SystemExit(f"no scorable items in {set_path} (drafts are skipped)")
    router = make_router(settings, model, ledger)
    # 005: the sample notes, indexed into the eval ledger's db, offered as the lookup tool.
    real_model = model is None or (isinstance(model, str) and model != "test")
    kb_emb = (Embeddings(settings.embedding_model, router.budget) if real_model
              else Embeddings("hashing", router.budget, embed_fn=hashing_embed))
    kb = KnowledgeBase(ledger, kb_emb, kb_dir, floor=settings.kb_floor) if kb_dir else None
    if kb:
        await kb.index()
        router.toolset = lookup_toolset(kb) if kb.count() else None
    estimate = router.budget.estimate_for(router.primary_model) * len(items)
    if router.budget.remaining() < 1.5 * estimate:
        raise SystemExit(f"not enough budget: need ~{estimate:.3f} USD x1.5, remaining {router.budget.remaining():.3f} USD")
    # 004: a fresh memory per run (temp file), so identity metrics measure this run only.
    import tempfile

    mem_file = Path(tempfile.gettempdir()) / f"lens-eval-memory-{os.getpid()}.db"
    if mem_file.exists():
        mem_file.unlink()
    emb = (Embeddings(settings.embedding_model, router.budget) if real_model
           else Embeddings("hashing", router.budget, embed_fn=hashing_embed))
    memory = Memory(mem_file, emb if settings.memory else None, threshold=settings.memory_threshold, min_gap_s=0)

    scores: list[PhotoScore] = []
    for item in items:
        path = photos / item.file
        if not path.exists():
            scores.append(score_photo(item, None, error=f"missing file {path}"))
            continue
        try:
            frame = prepare(path.read_bytes())
            routed = await router.run(frame, item.question, source="tap")
        except (BadImage, AgentFailed, BudgetExceeded) as e:
            scores.append(score_photo(item, None, error=f"{type(e).__name__}: {e}"))
            continue
        refs = await memory.link(routed.result, routed.request_id, source="tap")
        scores.append(score_photo(item, routed.result, latency_ms=routed.latency_ms, cost_usd=routed.cost_usd,
                                  escalated=routed.escalated, model=routed.model,
                                  entity_ids=[r.id if r else None for r in refs]))
        print(f"  {item.id:<24} recall {scores[-1].recall:.2f}  missed {scores[-1].missed}  "
              f"{routed.latency_ms} ms  ${routed.cost_usd:.4f}{'  escalated' if routed.escalated else ''}"
              f"{'  links ' + str(scores[-1].entity_links) if scores[-1].entity_links else ''}"
              f"{'  sources ' + str(scores[-1].sources) if scores[-1].sources else ''}", file=sys.stderr)
    try:
        mem_file.unlink(missing_ok=True)
    except OSError:
        pass  # Windows may still hold the file briefly; it is in the temp dir anyway

    report = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tag": tag,
        "set": str(set_path),
        "set_size": len(items),
        "model": router.primary_model,
        "escalation_model": router.escalation_model or None,
        "fingerprint": settings_fingerprint(),
        "summary": summarize(scores),
        "photos": [asdict(s) for s in scores],
    }
    if write:
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = runs_dir / f"{stamp}{'-' + tag if tag else ''}.json"
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        report["path"] = str(out)
    return report


async def bootstrap(*, photos: Path | None = None, set_path: Path = DEFAULT_SET, model: str | Model | None = None,
                    settings: Settings | None = None, ledger: Path = DEFAULT_LEDGER) -> list[EvalItem]:
    """Scan every image in the photos folder that is not in the set yet and append a draft
    entry with the model's labels. The owner edits the drafts and removes `"draft": true`."""
    settings = settings or Settings.from_env()
    photos = photos or photos_dir()
    existing = load_set(set_path)
    known = {i.file for i in existing}
    files = sorted(p for p in photos.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES and p.relative_to(photos).as_posix() not in known)
    if not files:
        return []
    router = make_router(settings, model, ledger)
    new: list[EvalItem] = []
    for path in files:
        rel = path.relative_to(photos).as_posix()
        item_id = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-") or f"photo-{len(existing) + len(new) + 1}"
        try:
            routed = await router.run(prepare(path.read_bytes()), None, source="tap")
        except (BadImage, AgentFailed, BudgetExceeded) as e:
            print(f"  {rel}: skipped ({type(e).__name__})", file=sys.stderr)
            continue
        expect = [Expected(label=o.label, category=o.category,
                           box=[round(o.bbox.y * 1000), round(o.bbox.x * 1000), round((o.bbox.y + o.bbox.h) * 1000),
                                round((o.bbox.x + o.bbox.w) * 1000)] if o.bbox else None)
                  for o in routed.result.objects]
        new.append(EvalItem(id=item_id, file=rel, expect=expect, notes=routed.result.scene, draft=True))
        print(f"  {rel}: {len(expect)} draft labels", file=sys.stderr)
    set_path.parent.mkdir(parents=True, exist_ok=True)
    with set_path.open("a", encoding="utf-8") as f:
        for item in new:
            f.write(json.dumps(item.to_json(), ensure_ascii=False) + "\n")
    return new


def compare(a: dict, b: dict) -> str:
    """Side-by-side summary metrics with deltas, as text."""
    keys = ["mean_recall", "forbid_hit_rate", "iou_mean", "answer_accuracy", "source_accuracy", "identity_rate", "identity_recall",
            "identity_precision", "escalation_rate", "mean_cost_usd", "latency_p50_ms", "latency_p95_ms"]
    sa, sb = a["summary"], b["summary"]
    lines = [f"{'metric':<18}{'A':>12}{'B':>12}{'delta':>12}",
             f"{'photos':<18}{sa['photos']:>12}{sb['photos']:>12}{'':>12}",
             f"{'model':<18}{a['model'].split(':')[-1]:>12}{b['model'].split(':')[-1]:>12}",
             f"{'fingerprint':<18}{a['fingerprint']:>12}{b['fingerprint']:>12}"]
    for k in keys:
        va, vb = sa.get(k), sb.get(k)
        fa = "-" if va is None else (f"{va:.4f}" if isinstance(va, float) else str(va))
        fb = "-" if vb is None else (f"{vb:.4f}" if isinstance(vb, float) else str(vb))
        d = "" if va is None or vb is None else f"{vb - va:+.4f}" if isinstance(vb, float) else f"{vb - va:+d}"
        lines.append(f"{k:<18}{fa:>12}{fb:>12}{d:>12}")
    return "\n".join(lines)


def _print_summary(report: dict) -> None:
    s = report["summary"]
    print(f"\n{report['set_size']} photos on {report['model']}"
          f"{' (+ ' + report['escalation_model'] + ')' if report.get('escalation_model') else ''}"
          f"  fingerprint {report['fingerprint']}")
    print(f"recall {s['mean_recall']:.3f}  forbid-hits {s['forbid_hit_rate']:.3f}  "
          f"iou {s['iou_mean'] if s['iou_mean'] is not None else '-'} ({s['iou_photos']} boxed)  "
          f"answers {s['answer_accuracy'] if s['answer_accuracy'] is not None else '-'}  "
          f"escalated {s['escalation_rate']:.3f}  cost ${s['mean_cost_usd']:.4f}/photo (${s['total_cost_usd']:.4f})  "
          f"latency p50 {s['latency_p50_ms']} ms p95 {s['latency_p95_ms']} ms  errors {s['errors']}")
    print(f"kb: {s['lookups']} answers cited notes, source accuracy {s['source_accuracy'] if s['source_accuracy'] is not None else '-'}")
    print(f"identity rate {s['identity_rate'] if s['identity_rate'] is not None else '-'}  "
          f"identity recall {s['identity_recall'] if s['identity_recall'] is not None else '-'}  "
          f"identity precision {s['identity_precision'] if s['identity_precision'] is not None else '-'}  "
          f"({s['entity_groups']} entity groups)")
    for f in s["failures"]:
        print(f"  - {f['id']}: missed {f['missed']}{' forbid ' + str(f['forbid_hits']) if f['forbid_hits'] else ''}"
              f"{' error ' + f['error'] if f['error'] else ''}")
    if report.get("path"):
        print(f"report: {report['path']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evals", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="score the set")
    r.add_argument("--set", type=Path, default=DEFAULT_SET)
    r.add_argument("--photos", type=Path, default=None)
    r.add_argument("--model", default=None, help="model string, or 'test' for the offline fake")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--tag", default="")
    r.add_argument("--kb", type=Path, default=DEFAULT_KB, help="notes folder for the lookup tool ('' = none)")
    b = sub.add_parser("bootstrap", help="draft labels for photos not in the set")
    b.add_argument("--photos", type=Path, default=None)
    b.add_argument("--set", type=Path, default=DEFAULT_SET)
    b.add_argument("--model", default=None)
    c = sub.add_parser("compare", help="metric deltas between two reports")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "run":
        report = asyncio.run(run(set_path=args.set, photos=args.photos, model=args.model, limit=args.limit, tag=args.tag,
                                 kb_dir=args.kb if str(args.kb) else None))
        _print_summary(report)
    elif args.cmd == "bootstrap":
        new = asyncio.run(bootstrap(photos=args.photos, set_path=args.set, model=args.model))
        print(f"{len(new)} draft entries appended to {args.set}; edit them and remove \"draft\": true")
    else:
        print(compare(json.loads(args.a.read_text(encoding="utf-8")), json.loads(args.b.read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
