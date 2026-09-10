#!/usr/bin/env bash
# Prepare an authenticated source cache on an F1 login node. This script never submits jobs.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/f1/prepare.sh [--root PATH] [--case-archive PATH]

Download and verify every source needed for the offline compute-node build. The optional
case archive must be the custodian-supplied fixed public Xmon bundle; no public URL exists.
EOF
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(cd -- "$script_dir/../.." && pwd -P)
root=${PALACE_F1_ROOT:-"$HOME/opt/palace"}
case_archive=

while (($#)); do
  case "$1" in
    --root)
      (($# >= 2)) || { echo "prepare.sh: --root requires a path" >&2; exit 2; }
      root=$2
      shift 2
      ;;
    --case-archive)
      (($# >= 2)) || { echo "prepare.sh: --case-archive requires a path" >&2; exit 2; }
      case_archive=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "prepare.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "prepare.sh: python3 is required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "prepare.sh: curl is required" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "prepare.sh: git is required" >&2; exit 1; }

arguments=(prepare --repo "$repo" --root "$root")
if [[ -n $case_archive ]]; then
  [[ -f $case_archive ]] || { echo "prepare.sh: case archive not found: $case_archive" >&2; exit 1; }
  arguments+=(--case-archive "$case_archive")
fi

python3 "$script_dir/f1ctl.py" "${arguments[@]}"

cat <<EOF

Preparation completed without submitting a job.
Root: $root
Submit the build from this checkout as documented in scripts/f1/README.md.
EOF
