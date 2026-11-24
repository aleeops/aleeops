#!/usr/bin/env python3
"""
Factor legacy Helm values (default-values/ + <cluster>/<ns>/<svc>/values.yml)
into a minimal 4-layer Helmfile .yaml.gotmpl hierarchy with automatic
variablization and 100% round-trip verification.
"""

import argparse
import copy
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

# Customize these to match your helmfile.yaml.gotmpl variable conventions
CLUSTER_EXPR = "{{ .Environment.Values.cluster }}"
NS_EXPR = "{{ .Environment.Values.namespace }}"
SVC_EXPR = "{{ .Release.Name }}"


def canon(val: Any) -> str:
    """Type-safe canonical key (keeps True != 1 and supports lists/dicts)."""
    return json.dumps(val, sort_keys=True, default=str)


def helm_merge(base: Any, override: Any) -> Any:
    if override is None:
        return copy.deepcopy(base)
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    merged = copy.deepcopy(base)
    for k, v in override.items():
        if v is None:
            merged.pop(k, None)
        elif k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = helm_merge(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v)
    return merged


def flatten(obj: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Flatten nested dicts into tuple-keyed leaf paths (lists stay atomic)."""
    out: dict[tuple[str, ...], Any] = {}
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            out.update(flatten(v, prefix + (str(k),)))
    elif prefix:
        out[prefix] = obj
    return out


def unflatten(flat: dict[tuple[str, ...], Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path, val in flat.items():
        cur = root
        for part in path[:-1]:
            cur = cur.setdefault(part, {})
        cur[path[-1]] = copy.deepcopy(val)
    return root


def boundary_replace(text: str, token: str, replacement: str) -> str:
    """Replace token only on non-alphanumeric boundaries (won't turn 'scale' into '{{cluster}}ale')."""
    if not token:
        return text
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")
    return pattern.sub(lambda _: replacement, text)


def templatize_val(val: Any, pairs: list[tuple[str, str]]) -> Any:
    """Variablize strings and flat string lists using (literal, go_template_expr) pairs."""
    # Sort longest tokens first to avoid partial shadowing
    ordered = sorted(pairs, key=lambda x: len(x[0]), reverse=True)
    if isinstance(val, str):
        out = val
        for tok, expr in ordered:
            out = boundary_replace(out, tok, expr)
        return out
    if isinstance(val, list) and all(isinstance(x, str) for x in val):
        return [templatize_val(x, ordered) for x in val]
    return copy.deepcopy(val)


def render_val(val: Any, ctx: dict[str, str]) -> Any:
    """Substitute our generated Go template expressions back to concrete values."""
    if isinstance(val, str):
        out = val
        for expr, concrete in ctx.items():
            out = out.replace(expr, concrete)
        return out
    if isinstance(val, list):
        return [render_val(x, ctx) for x in val]
    if isinstance(val, dict):
        return {k: render_val(v, ctx) for k, v in val.items()}
    return copy.deepcopy(val)


def find_unified_template(
    samples: list[tuple[Any, dict[str, str]]],
    var_keys: list[str],
) -> tuple[bool, Any]:
    """
    Check if all samples share an identical literal value OR collapse to a single
    variablized template that round-trips 100% losslessly for every sample.
    """
    if not samples:
        return False, None

    # 1. Exact literal match (prefer concrete over templates when identical)
    first_val = samples[0][0]
    if all(canon(v) == canon(first_val) for v, _ in samples):
        return True, copy.deepcopy(first_val)

    # 2. Try variablizing the requested scope tokens
    candidates = []
    for val, ctx in samples:
        pairs = [(ctx[expr], expr) for expr in var_keys if expr in ctx]
        candidates.append(templatize_val(val, pairs))

    tmpl = candidates[0]
    if not all(canon(c) == canon(tmpl) for c in candidates):
        return False, None

    # 3. Verify lossless round-trip on every single sample
    for orig_val, ctx in samples:
        if canon(render_val(tmpl, ctx)) != canon(orig_val):
            return False, None

    return True, tmpl


def minimal_delta(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Return the smallest YAML override so that helm_merge(base, delta) == target."""
    delta: dict[str, Any] = {}
    for k, target_val in target.items():
        if k not in base:
            delta[k] = copy.deepcopy(target_val)
        elif isinstance(base[k], dict) and isinstance(target_val, dict):
            sub = minimal_delta(base[k], target_val)
            if sub:
                delta[k] = sub
        elif canon(base[k]) != canon(target_val):
            delta[k] = copy.deepcopy(target_val)

    for k in base:
        if k not in target:
            delta[k] = None  # Explicit null unsets inherited key in Helm
    return delta


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def find_values_file(directory: Path) -> Path | None:
    for name in ("values.yml", "values.yaml"):
        p = directory / name
        if p.exists():
            return p
    return None


def dump_yaml(path: Path, data: dict[str, Any], header: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=True, default_flow_style=False) if data else "{}\n"
    content = f"# {header}\n{body}"
    path.write_text(content)
    return len(content.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert legacy default-values + cluster dir into layered Helmfile .yaml.gotmpl structure."
    )
    ap.add_argument("--defaults-dir", required=True, help="Path to default-values/ directory.")
    ap.add_argument("--cluster-dir", required=True, help="Path to cluster directory (e.g. sc).")
    ap.add_argument("--cluster-name", help="Cluster name (defaults to --cluster-dir basename).")
    ap.add_argument(
        "--out-dir",
        default="helmfile-migrated/values",
        help="Output values root (default: helmfile-migrated/values).",
    )
    ap.add_argument(
        "--default-threshold",
        type=float,
        default=0.5,
        help="Min fraction of namespaces sharing a value to hoist into defaults/<svc> (default: 0.5).",
    )
    args = ap.parse_args()

    defaults_root = Path(args.defaults_dir)
    cluster_root = Path(args.cluster_dir)
    cluster = args.cluster_name or cluster_root.name
    out_root = Path(args.out_dir)

    # 1. Discover all (ns, svc) pairs and compute effective merged configs
    effective: dict[str, dict[str, dict[str, Any]]] = {}
    flat_eff: dict[str, dict[str, dict[tuple[str, ...], Any]]] = {}
    orig_lines = 0
    seen_default_files: set[Path] = set()

    namespaces = sorted(p.name for p in cluster_root.iterdir() if p.is_dir())
    all_services: set[str] = set()

    for ns in namespaces:
        effective[ns] = {}
        flat_eff[ns] = {}
        for svc_dir in sorted((cluster_root / ns).iterdir()):
            if not svc_dir.is_dir():
                continue
            svc = svc_dir.name
            cluster_val_file = find_values_file(svc_dir)
            if not cluster_val_file:
                continue

            all_services.add(svc)
            orig_lines += len(cluster_val_file.read_text().splitlines())

            def_val_file = find_values_file(defaults_root / svc)
            base_cfg = load_yaml_file(def_val_file) if def_val_file else {}
            if def_val_file and def_val_file not in seen_default_files:
                seen_default_files.add(def_val_file)
                orig_lines += len(def_val_file.read_text().splitlines())

            override_cfg = load_yaml_file(cluster_val_file)
            merged = helm_merge(base_cfg, override_cfg)
            effective[ns][svc] = merged
            flat_eff[ns][svc] = flatten(merged)

    pairs_all = [(ns, svc) for ns in namespaces for svc in effective[ns]]
    if not pairs_all:
        sys.exit("No values.yml / values.yaml files found under cluster-dir.")

    def ctx_for(ns: str, svc: str) -> dict[str, str]:
        return {CLUSTER_EXPR: cluster, NS_EXPR: ns, SVC_EXPR: svc}

    # 2. Extract Layer 2: <cluster>/cluster-base.yaml.gotmpl (100% presence across cluster)
    all_paths = set.intersection(*(set(flat_eff[ns][svc].keys()) for ns, svc in pairs_all))
    cluster_base_flat: dict[tuple[str, ...], Any] = {}
    for p in sorted(all_paths):
        samples = [(flat_eff[ns][svc][p], ctx_for(ns, svc)) for ns, svc in pairs_all]
        ok, val = find_unified_template(samples, var_keys=[NS_EXPR, SVC_EXPR])
        if ok:
            cluster_base_flat[p] = val
    cluster_base = unflatten(cluster_base_flat)

    # 3. Extract Layer 3: <cluster>/<ns>/base.yaml.gotmpl (100% presence across services in ns)
    ns_base: dict[str, dict[str, Any]] = {}
    ns_base_flat: dict[str, dict[tuple[str, ...], Any]] = {}
    for ns in namespaces:
        svcs_in_ns = sorted(effective[ns].keys())
        if not svcs_in_ns:
            ns_base[ns] = {}
            ns_base_flat[ns] = {}
            continue
        ns_common_paths = set.intersection(*(set(flat_eff[ns][s].keys()) for s in svcs_in_ns))
        extracted: dict[tuple[str, ...], Any] = {}
        for p in sorted(ns_common_paths):
            # Skip if cluster-base already produces the exact right value for every service here
            if p in cluster_base_flat and all(
                canon(render_val(cluster_base_flat[p], ctx_for(ns, s)))
                == canon(flat_eff[ns][s][p])
                for s in svcs_in_ns
            ):
                continue
            samples = [(flat_eff[ns][s][p], ctx_for(ns, s)) for s in svcs_in_ns]
            ok, val = find_unified_template(samples, var_keys=[SVC_EXPR])
            if ok:
                extracted[p] = val
        ns_base_flat[ns] = extracted
        ns_base[ns] = unflatten(extracted)

    # 4. Extract Layer 1: defaults/<svc>.yaml.gotmpl (service defaults across namespaces)
    svc_defaults: dict[str, dict[str, Any]] = {}
    for svc in sorted(all_services):
        svc_namespaces = [ns for ns in namespaces if svc in effective[ns]]
        if not svc_namespaces:
            continue
        # Only consider paths present across all namespaces of this service (avoids emitting nulls)
        svc_paths = set.intersection(*(set(flat_eff[ns][svc].keys()) for ns in svc_namespaces))
        extracted: dict[tuple[str, ...], Any] = {}
        for p in sorted(svc_paths):
            # Skip paths already handled cleanly by cluster-base or every ns-base
            handled_above = all(
                (p in ns_base_flat[ns])
                or (
                    p in cluster_base_flat
                    and canon(render_val(cluster_base_flat[p], ctx_for(ns, svc)))
                    == canon(flat_eff[ns][svc][p])
                )
                for ns in svc_namespaces
            )
            if handled_above:
                continue

            samples = [(flat_eff[ns][svc][p], ctx_for(ns, svc)) for ns in svc_namespaces]
            # Prefer full-match literal or variablized (cluster + ns) template
            ok, val = find_unified_template(samples, var_keys=[CLUSTER_EXPR, NS_EXPR])
            if ok:
                extracted[p] = val
                continue

            # Otherwise, check if a variablized or literal value meets majority threshold
            counts: Counter[str] = Counter()
            rep_val: dict[str, Any] = {}
            for orig_val, ctx in samples:
                cand = templatize_val(
                    orig_val,
                    [(ctx[CLUSTER_EXPR], CLUSTER_EXPR), (ctx[NS_EXPR], NS_EXPR)],
                )
                # Only keep variablized candidate if it round-trips for this sample
                chosen = cand if canon(render_val(cand, ctx)) == canon(orig_val) else orig_val
                key = canon(chosen)
                counts[key] += 1
                rep_val[key] = chosen

            best_key, best_count = counts.most_common(1)[0]
            if best_count / len(svc_namespaces) >= args.default_threshold and (
                best_count >= 2 or len(svc_namespaces) == 1
            ):
                extracted[p] = rep_val[best_key]

        svc_defaults[svc] = unflatten(extracted)

    # 5. Extract Layer 4: <cluster>/<ns>/<svc>.yaml.gotmpl & write all files
    new_lines = 0
    new_lines += dump_yaml(
        out_root / cluster / "cluster-base.yaml.gotmpl",
        cluster_base,
        f"Cluster-level defaults for {cluster}",
    )

    for ns in namespaces:
        new_lines += dump_yaml(
            out_root / cluster / ns / "base.yaml.gotmpl",
            ns_base[ns],
            f"Namespace-level defaults for {cluster}/{ns}",
        )

    for svc, data in svc_defaults.items():
        new_lines += dump_yaml(
            out_root / "defaults" / f"{svc}.yaml.gotmpl",
            data,
            f"Cross-cluster defaults for {svc} (review before merging with existing defaults)",
        )

    # 6. Compute minimal leaf overrides + 100% round-trip verification
    passed = 0
    failed = 0
    for ns, svc in pairs_all:
        ctx = ctx_for(ns, svc)
        l1 = render_val(svc_defaults.get(svc, {}), ctx)
        l2 = render_val(cluster_base, ctx)
        l3 = render_val(ns_base.get(ns, {}), ctx)

        base_3 = helm_merge(helm_merge(l1, l2), l3)
        leaf = minimal_delta(base_3, effective[ns][svc])

        new_lines += dump_yaml(
            out_root / cluster / ns / f"{svc}.yaml.gotmpl",
            leaf,
            f"Service-specific overrides for {cluster}/{ns}/{svc}",
        )

        # Verify full 4-layer merge matches original effective config identically
        reconstructed = helm_merge(base_3, render_val(leaf, ctx))
        if canon(reconstructed) == canon(effective[ns][svc]):
            passed += 1
        else:
            failed += 1
            sys.stderr.write(f"MISMATCH in {cluster}/{ns}/{svc}!\n")

    reduction = ((orig_lines - new_lines) / orig_lines * 100) if orig_lines else 0.0
    print(f"Generated layered Helmfile structure in: {out_root}")
    print(f"Lines: {orig_lines} -> {new_lines} ({reduction:.1f}% reduction)")
    print(f"Round-trip verification: {passed}/{passed + failed} services matched 100%.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()