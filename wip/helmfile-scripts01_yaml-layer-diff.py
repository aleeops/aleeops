#!/usr/bin/env python3

# Requires pip install pyyaml (plus helmfile/helm on $PATH if rendering .gotmpl files or charts, and optionally dyff).

"""
Render and diff two stacks of layered YAML or Helmfile .yaml.gotmpl files,
optionally passing both through `helm template` to compare final K8s manifests.
"""

import argparse
import copy
import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

# ANSI colors for fallback diff
RED = "\033[31m"
GREEN = "\033[32m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"


def helm_merge(base: Any, override: Any) -> Any:
    """Merge two YAML structures using Helm's exact merge semantics."""
    if override is None:
        return copy.deepcopy(base)
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)

    merged = copy.deepcopy(base)
    for k, v in override.items():
        if v is None:
            # In Helm, setting a key to null removes it from lower layers
            merged.pop(k, None)
        elif k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = helm_merge(merged[k], v)
        else:
            # Lists and scalars replace lower layers completely
            merged[k] = copy.deepcopy(v)
    return merged


def run_cmd(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n")
        sys.stderr.write(proc.stderr + "\n")
        sys.exit(2)
    return proc.stdout


def render_standalone_gotmpl(
    files: list[Path],
    release: str,
    namespace: str,
    env_vars_file: Path | None,
) -> dict:
    """Render an arbitrary ordered list of YAML/.gotmpl files via a temp helmfile."""
    if not shutil.which("helmfile"):
        sys.exit("Error: `helmfile` binary is required to render .gotmpl files.")

    with tempfile.TemporaryDirectory(prefix="hf-diff-") as tmp:
        tmp_path = Path(tmp)
        chart_dir = tmp_path / "dummy-chart"
        chart_dir.mkdir()
        (chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: dummy\nversion: 0.1.0\n"
        )

        env_block: dict[str, Any] = {"default": {}}
        if env_vars_file:
            env_block["default"]["values"] = [str(env_vars_file.resolve())]

        hf_doc_1 = yaml.safe_dump({"environments": env_block}, sort_keys=False)
        hf_doc_2 = yaml.safe_dump(
            {
                "releases": [
                    {
                        "name": release,
                        "namespace": namespace,
                        "chart": str(chart_dir),
                        "values": [str(f.resolve()) for f in files],
                    }
                ]
            },
            sort_keys=False,
        )

        hf_file = tmp_path / "helmfile.yaml.gotmpl"
        hf_file.write_text(f"{hf_doc_1}---\n{hf_doc_2}")

        out_file = tmp_path / "merged-values.yaml"
        run_cmd(
            [
                "helmfile",
                "-f",
                str(hf_file),
                "-e",
                "default",
                "write-values",
                "--output-file-template",
                str(out_file),
            ]
        )
        return yaml.safe_load(out_file.read_text()) or {}


def render_from_helmfile(
    helmfile_path: Path, env: str, selector: str | None, render_manifests: bool
) -> tuple[dict | None, str | None]:
    """Pull merged values or rendered manifests directly from a real helmfile."""
    if not shutil.which("helmfile"):
        sys.exit("Error: `helmfile` binary not found in PATH.")

    base_cmd = ["helmfile", "-f", str(helmfile_path), "-e", env]
    if selector:
        base_cmd += ["-l", selector]

    if render_manifests:
        return None, run_cmd(base_cmd + ["template"])

    with tempfile.TemporaryDirectory(prefix="hf-write-") as tmp:
        out_file = Path(tmp) / "values.yaml"
        run_cmd(
            base_cmd
            + ["write-values", "--output-file-template", str(out_file)]
        )
        return yaml.safe_load(out_file.read_text()) or {}, None


def resolve_side(
    files: list[str] | None,
    helmfile_path: str | None,
    env: str | None,
    selector: str | None,
    vars_file: str | None,
    chart: str | None,
    release: str,
    namespace: str,
    helmfile_manifests: bool,
) -> str:
    """Resolve one side (left or right) into a YAML string (values or manifests)."""
    if helmfile_path:
        if not env:
            sys.exit("Error: --*-env is required when using --*-helmfile.")
        values, manifests = render_from_helmfile(
            Path(helmfile_path), env, selector, render_manifests=helmfile_manifests
        )
        if manifests is not None:
            return manifests
    elif files:
        paths = [Path(f) for f in files]
        for p in paths:
            if not p.exists():
                sys.exit(f"Error: file not found: {p}")

        if any(p.suffix == ".gotmpl" for p in paths):
            values = render_standalone_gotmpl(
                paths, release, namespace, Path(vars_file) if vars_file else None
            )
        else:
            values = {}
            for p in paths:
                doc = yaml.safe_load(p.read_text()) or {}
                values = helm_merge(values, doc)
    else:
        sys.exit("Error: provide either --*-files or --*-helmfile for both sides.")

    if chart:
        if not shutil.which("helm"):
            sys.exit("Error: `helm` binary is required when using --chart.")
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=True) as tf:
            yaml.safe_dump(values, tf, sort_keys=True)
            tf.flush()
            return run_cmd(
                [
                    "helm",
                    "template",
                    release,
                    chart,
                    "--namespace",
                    namespace,
                    "-f",
                    tf.name,
                ]
            )

    return yaml.safe_dump(values, sort_keys=True, default_flow_style=False)


def normalize_yaml_docs(raw_yaml: str) -> str:
    """Sort keys across multi-document YAML so text diffs ignore key order."""
    docs = [d for d in yaml.safe_load_all(raw_yaml) if d is not None]
    return yaml.safe_dump_all(docs, sort_keys=True, default_flow_style=False)


def diff_yaml(
    left_yaml: str,
    right_yaml: str,
    left_label: str,
    right_label: str,
    use_dyff: bool = True,
) -> int:
    """Display colorized diff; returns 0 if identical, 1 if different."""
    if use_dyff and shutil.which("dyff"):
        with tempfile.TemporaryDirectory(prefix="yaml-diff-") as tmp:
            l_path = Path(tmp) / "left.yaml"
            r_path = Path(tmp) / "right.yaml"
            l_path.write_text(left_yaml)
            r_path.write_text(right_yaml)
            proc = subprocess.run(
                [
                    "dyff",
                    "between",
                    "--omit-header",
                    "--color",
                    "on",
                    "--set-exit-code",
                    str(l_path),
                    str(r_path),
                ]
            )
            if proc.returncode == 0:
                print(f"{GREEN}No differences found.{RESET}")
            return proc.returncode

    left_norm = normalize_yaml_docs(left_yaml).splitlines(keepends=True)
    right_norm = normalize_yaml_docs(right_yaml).splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            left_norm, right_norm, fromfile=left_label, tofile=right_label, n=3
        )
    )

    if not diff_lines:
        print(f"{GREEN}No differences found.{RESET}")
        return 0

    for line in diff_lines:
        if line.startswith(("+++", "---")):
            sys.stdout.write(f"{BOLD}{line}{RESET}")
        elif line.startswith("+"):
            sys.stdout.write(f"{GREEN}{line}{RESET}")
        elif line.startswith("-"):
            sys.stdout.write(f"{RED}{line}{RESET}")
        elif line.startswith("@@"):
            sys.stdout.write(f"{CYAN}{line}{RESET}")
        else:
            sys.stdout.write(line)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Render and color-diff two layered YAML / Helmfile stacks."
    )
    for side in ("left", "right"):
        g = p.add_argument_group(f"{side.capitalize()} input")
        g.add_argument(
            f"--{side}-files",
            nargs="+",
            help="Ordered list of YAML or .yaml.gotmpl files (lowest to highest priority).",
        )
        g.add_argument(
            f"--{side}-vars",
            help="Optional YAML file of .Environment.Values for standalone .gotmpl files.",
        )
        g.add_argument(
            f"--{side}-helmfile",
            help="Use an existing helmfile.yaml.gotmpl instead of a raw file list.",
        )
        g.add_argument(f"--{side}-env", help="Helmfile environment name (-e).")
        g.add_argument(
            f"--{side}-selector", help="Helmfile label selector (-l), e.g. name=svc1."
        )

    p.add_argument(
        "--chart",
        help="Optional Helm chart path/ref. If set, renders full K8s manifests via `helm template` before diffing.",
    )
    p.add_argument("--release", default="svc1", help="Release name (default: svc1).")
    p.add_argument("--namespace", default="dev", help="Namespace (default: dev).")
    p.add_argument(
        "--helmfile-manifests",
        action="store_true",
        help="When using --*-helmfile, run `helmfile template` directly instead of `write-values`.",
    )
    p.add_argument(
        "--no-dyff",
        action="store_true",
        help="Force Python unified diff even if `dyff` is installed.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    left = resolve_side(
        args.left_files,
        args.left_helmfile,
        args.left_env,
        args.left_selector,
        args.left_vars,
        args.chart,
        args.release,
        args.namespace,
        args.helmfile_manifests,
    )
    right = resolve_side(
        args.right_files,
        args.right_helmfile,
        args.right_env,
        args.right_selector,
        args.right_vars,
        args.chart,
        args.release,
        args.namespace,
        args.helmfile_manifests,
    )
    rc = diff_yaml(left, right, "left", "right", use_dyff=not args.no_dyff)
    sys.exit(rc)


if __name__ == "__main__":
    main()