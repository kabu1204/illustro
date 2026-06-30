"""Download WD14 (with embeddings) model and tag files directly into the project's data/models.

Uses hf_hub_download's local_dir parameter to write files into the project directory
instead of the global ~/.cache/huggingface, avoiding duplicate copies. The downloaded
path will be data/models/<onnx_file>.

Different sub-models may have different subdirectory/filename layouts, so this script
lists repo files first. If a configured file is not found, available options are printed
to help you update config.yaml.
"""
from __future__ import annotations

import os

from .config import Config


def download_models(cfg: Config) -> None:
    from huggingface_hub import hf_hub_download, list_repo_files

    # Disable symlink warning; place real files directly in the local directory
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    repo = cfg.tagger.hf_repo
    dest = cfg.model_dir  # Project-local: <data_dir>/models
    print(f"[*] Repo: {repo}")
    print(f"[*] Download target (project-local): {dest}")
    try:
        files = list_repo_files(repo)
    except Exception as e:
        print(f"[!] Failed to list repo files: {e}")
        print("    Check network / whether HuggingFace login is required (private repo).")
        return

    onnx = [f for f in files if f.endswith(".onnx")]
    csvs = [f for f in files if f.endswith(".csv")]
    print(f"[*] .onnx files in repo: {onnx}")
    print(f"[*] .csv files in repo:  {csvs}")

    want = [(cfg.tagger.onnx_file, "ONNX model"), (cfg.tagger.tags_csv, "tag file")]
    ok = True
    for rel, label in want:
        if rel not in files:
            ok = False
            print(f"[!] Configured {label} '{rel}' not found in repo.")
            print(f"    Update the corresponding entry in config.yaml to one of the paths listed above, then re-run.")
            continue
        print(f"[*] Downloading {label}: {rel}")
        try:
            # Old versions (<1.0): explicitly disable symlinks so real files land in the project directory
            path = hf_hub_download(
                repo_id=repo, filename=rel,
                local_dir=str(dest), local_dir_use_symlinks=False,
            )
        except TypeError:
            # New versions removed this parameter; local_dir defaults to real files
            path = hf_hub_download(repo_id=repo, filename=rel, local_dir=str(dest))
        print(f"    -> {path}")

    if ok:
        print(f"[done] Model ready at {dest}. You can now run: python -m illustro.cli build")
    else:
        print("[done] See messages above. Adjust config.yaml and re-run download-models.")
