# Layered YAML / Template Diff Script & Tooling
Why Python + shelling out (instead of pure Python): Python is the right orchestrator here, but do not try to evaluate `.yaml.gotmpl` files in Python. Helmfile relies on Go's `text/template`, Sprig functions, and Helmfile-specific context (`.Environment.Values`, `.Release`, `requiredEnv`, `readFile`, `tpl`). Re-implementing that in Python creates subtle drift from what actually deploys.

The script below handles both worlds cleanly:
- Plain YAML lists: Merged directly in Python using Helm's exact merge semantics (recursive dict merge, list replacement, and `key: null` deletion).
- `.yaml.gotmpl` lists (or an existing `helmfile.yaml.gotmpl`): Rendered using the real `helmfile` binary (`helmfile write-values` / `helmfile` template) via an offline temporary wrapper so Go templates evaluate identically to production.
- Optional chart rendering (`--chart`): Passes both merged values through `helm template` so you can diff either the merged values or the final K8s manifests.
- Colorized diff: Automatically uses `dyff` (semantic YAML diff that ignores key ordering) if installed, and falls back to sorted-key Python `difflib` with ANSI red/green output.

### yaml_layer_diff.py
Requires `pip install pyyaml` (plus `helmfile`/`helm` on `$PATH` if rendering `.gotmpl` files or charts, and optionally `dyff`).

# Usage Examples
1. Compare legacy `sc` values vs. new Helmfile layers directly (values-only diff):
    ```
    python3 yaml_layer_diff.py \
    --left-files default-values/svc1/values.yml sc/dev/svc1/values.yml \
    --right-files \
        helmfile/values/defaults/svc1.yaml.gotmpl \
        helmfile/values/sc/cluster-base.yaml.gotmpl \
        helmfile/values/sc/dev/base.yaml.gotmpl \
        helmfile/values/sc/dev/svc1.yaml.gotmpl \
    --right-vars env-vars-sc-dev.yaml \
    --release svc1 --namespace dev
    ```
1. Compare legacy `sc` vs. your real `helmfile.yaml.gotmpl`, rendering full K8s manifests:
    ```
    python3 yaml_layer_diff.py \
    --left-files default-values/svc1/values.yml sc/dev/svc1/values.yml \
    --right-helmfile helmfile/helmfile.yaml.gotmpl \
    --right-env sc-dev --right-selector name=svc1 \
    --chart ./charts/svc1 --release svc1 --namespace dev
    ```


### Existing Tools That Help Here

| Tool | Best Use Case | Fits Your Migration Workflow? |
|---|---|---|
| **`dyff`** | Semantic, colorized YAML diff (`dyff between a.yaml b.yaml`) that ignores key reordering and shows exact YAML paths. | **Yes — install this first.** It pairs with `helm template` and `helmfile template` out of the box, and the script above delegates to it automatically. |
| **`helm-unittest`** | Unit assertions and **snapshot testing** (`matchSnapshot: {}`) of chart output in CI. | **Partially.** Not built for ad-hoc side-by-side comparison of two value stacks, *but* great for locking down migrations: snapshot the legacy `sc` output, switch the test input to Helmfile's `write-values` output, and assert zero snapshot drift in GitLab CI. |
| **`helm-diff` / `helmfile diff`** | Diffs rendered manifests against a **live cluster** release. | **Post-migration check.** Run `helmfile -e sc-dev diff` against the live `sc` cluster before applying to confirm zero unexpected live changes. |
| **`git diff --no-index` + `delta`** | Syntax-highlighted side-by-side text diffs in the terminal. | Good lightweight alternative if you don't want to install `dyff`, though it is line-based rather than YAML-semantic. |

---

## 2. Auto-Generating the Layered Helmfile Structure

The script below scans `default-values/` plus an entire cluster directory (e.g., `sc/`), computes the effective merged config for every `(namespace, service)`, and factors it into your exact 4-layer hierarchy:

1. `helmfile/values/defaults/<svc>.yaml.gotmpl` — service defaults across namespaces, with **boundary-safe variablization** (e.g., turning `"svc1.dev.sc.internal"` / `"svc1.test.sc.internal"` into `"{{ .Release.Name }}.{{ .Environment.Values.namespace }}.{{ .Environment.Values.cluster }}.internal"` only when it round-trips 100% losslessly).
2. `helmfile/values/<cluster>/cluster-base.yaml.gotmpl` — values shared across **every** namespace and service in the cluster (e.g., image registry, cluster domain).
3. `helmfile/values/<cluster>/<ns>/base.yaml.gotmpl` — values shared across **every** service in a given namespace (e.g., `namespace`, env labels).
4. `helmfile/values/<cluster>/<ns>/<svc>.yaml.gotmpl` — minimal leaf overrides.

Two important safeguards are built in:
* **End-to-end self-verification:** After writing the 4 layers, the script re-renders and re-merges `L1 -> L2 -> L3 -> L4` for every `(namespace, service)` and asserts **100% exact equality** with the original legacy merge.
* **Non-destructive output by default:** Writes to `--out-dir` (e.g., `helmfile-migrated/values`) so you don't overwrite your existing `va` `defaults/<svc>.yaml.gotmpl` files before reviewing/merging them.

> **Watch out for two Helm realities:**
> 1. **Lists are atomic:** Helm replaces lists rather than merging elements, so lists are hoisted or overridden whole.
> 2. **Strict `cluster-base` / `ns-base` thresholds:** By default, a key is only hoisted to `cluster-base` or `ns-base` if **100%** of services in that scope define it identically. That prevents injecting unknown keys into charts that enforce a strict `values.schema.json`. For `defaults/<svc>`, a majority vote (`--default-threshold`, default `0.5`) is safe because every namespace deploys the same chart.

### Running It on `sc`

```bash
python3 migrate_to_helmfile.py \
  --defaults-dir default-values \
  --cluster-dir sc \
  --out-dir helmfile-staging/values
```

After running it:
1. Inspect `helmfile-staging/values/defaults/<svc>.yaml.gotmpl` against your existing `va` `helmfile/values/defaults/<svc>.yaml.gotmpl` using Script #1 (`yaml_layer_diff.py`) so you merge `sc` and `va` service defaults cleanly.
2. Copy `helmfile-staging/values/sc/` into `helmfile/values/sc/`.
3. Validate every service end-to-end with Script #1 (`--left-files default-values/<svc>/values.yml sc/<ns>/<svc>/values.yml` vs `--right-helmfile ... --chart ...`).

### Are There Online Tools for This Refactoring Process?

There is **no turnkey tool** that automatically reverse-engineers flat Helm `values.yaml` trees into variablized Helmfile `.yaml.gotmpl` layers — because deciding which keys belong at cluster scope vs. namespace scope vs. service defaults depends on your repository's own `.Environment.Values` contract. Custom Python (like the script above) is the standard way teams migrate legacy repos safely.

That said, a few tools complement the process:
* **`yq` (Mike Farah's Go CLI):** Useful for targeted bulk surgery during cleanup (e.g., deleting a field you just moved into `cluster-base.yaml.gotmpl` across 40 files in one command).
* **`helmfile write-values`:** Your primary sanity check after refactoring to inspect the exact resolved YAML each release receives.
* **Typed config languages (`CUE`, `ytt` from Carvel, `Pkl`, or `KCL`):** Worth knowing about if Go-templated YAML ever becomes too fragile at scale — they provide native overlays, schema validation, and variable interpolation without indentation/quoting hazards, though sticking with Helmfile + `.yaml.gotmpl` makes total sense since you already have `va` working on it.





    Existing Tools That Help HereToolBest Use CaseFits Your Migration Workflow?dyffSemantic, colorized YAML diff (dyff between a.yaml b.yaml) that ignores key reordering and shows exact YAML paths.Yes — install this first. It pairs with helm template and helmfile template out of the box, and the script above delegates to it automatically.helm-unittestUnit assertions and snapshot testing (matchSnapshot: {}) of chart output in CI.Partially. Not built for ad-hoc side-by-side comparison of two value stacks, but great for locking down migrations: snapshot the legacy sc output, switch the test input to Helmfile's write-values output, and assert zero snapshot drift in GitLab CI.helm-diff / helmfile diffDiffs rendered manifests against a live cluster release.Post-migration check. Run helmfile -e sc-dev diff against the live sc cluster before applying to confirm zero unexpected live changes.git diff --no-index + deltaSyntax-highlighted side-by-side text diffs in the terminal.Good lightweight alternative if you don't want to install dyff, though it is line-based rather than YAML-semantic.