#!/usr/bin/env bash
#SBATCH --job-name=palace-f1-xmon
#SBATCH --partition=ct112
#SBATCH --nodes=1
#SBATCH --ntasks=56
#SBATCH --cpus-per-task=1
#SBATCH --time=04:00:00

# Run the fixed public Xmon input with the direct Palace ELF executable.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sbatch [resource options] scripts/f1/run.sbatch [--root PATH] [--case-dir PATH]

MPI ranks and threads are taken only from Slurm's --ntasks and --cpus-per-task values.
EOF
}

if [[ -n ${PALACE_F1_REPO:-} ]]; then
  repo_candidate=$PALACE_F1_REPO
elif [[ -n ${SLURM_SUBMIT_DIR:-} && -f $SLURM_SUBMIT_DIR/scripts/f1/common.sh ]]; then
  repo_candidate=$SLURM_SUBMIT_DIR
elif [[ -f "$(dirname -- "${BASH_SOURCE[0]}")/common.sh" ]]; then
  repo_candidate=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
else
  echo "palace-f1: set PALACE_F1_REPO or submit from the Palace checkout root" >&2
  exit 1
fi
repo_candidate=$(cd -- "$repo_candidate" && pwd -P)
export PALACE_F1_REPO=$repo_candidate
# shellcheck source=common.sh
source "$repo_candidate/scripts/f1/common.sh"

repo=$(f1_resolve_repo)
root=${PALACE_F1_ROOT:-"$HOME/opt/palace"}
case_dir=
while (($#)); do
  case "$1" in
    --root)
      (($# >= 2)) || f1_die "--root requires a path"
      root=$2
      shift 2
      ;;
    --case-dir)
      (($# >= 2)) || f1_die "--case-dir requires a path"
      case_dir=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      f1_die "unknown argument: $1"
      ;;
  esac
done

case "$root" in
  *$'\n'*|*';'*) f1_die "PALACE_F1_ROOT may not contain newlines or semicolons" ;;
esac
root_requested=$root
root=$(cd -- "$root_requested" && pwd -P) || f1_die "prepared root does not exist: $root_requested"
control=$repo/scripts/f1/f1ctl.py
lock=$repo/scripts/f1/sources.lock.json
prefix=$root/versions/$PALACE_F1_BUILD_ID
ranks=${SLURM_NTASKS:-}
threads=${SLURM_CPUS_PER_TASK:-}

f1_require_job
[[ $ranks =~ ^[1-9][0-9]*$ ]] || f1_die "SLURM_NTASKS must be a positive integer"
[[ $threads =~ ^[1-9][0-9]*$ ]] || f1_die "SLURM_CPUS_PER_TASK must be a positive integer"
((ranks * threads <= 112)) || f1_die "one F1 node supports at most 112 allocated CPUs"
unset ARMPL_DIR ARMPLROOT ARMPL_ROOT AOCL_DIR AOCLROOT AOCL_ROOT MKL_DIR MKLROOT MKL_ROOT
unset OPENBLAS_DIR OPENBLASROOT OPENBLAS_ROOT
unset LD_LIBRARY_PATH LD_PRELOAD
f1_load_modules
if [[ -z $case_dir ]]; then
  case_dir=$(python3 "$control" verify-prepared \
    --repo "$repo" --root "$root" --field public_case.case_dir)
else
  python3 "$control" verify-prepared --repo "$repo" --root "$root"
fi
executable=$(python3 "$control" verify-install \
  --prefix "$prefix" --prepared "$root/prepared.json" --field executable)
case_requested=$case_dir
case_dir=$(cd -- "$case_requested" && pwd -P) || \
  f1_die "public case directory does not exist: $case_requested"
case_receipt=$case_dir.receipt.json
[[ -f $case_receipt ]] || f1_die "public case receipt missing: $case_receipt"
python3 "$control" verify-case --lock "$lock" --case-dir "$case_dir"
config_relative=$(python3 "$control" verify-case \
  --lock "$lock" --case-dir "$case_dir" --field config_relative_path)
mesh_relative=$(python3 "$control" verify-case \
  --lock "$lock" --case-dir "$case_dir" --field mesh_relative_path)
archive_sha=$(python3 "$control" verify-case \
  --lock "$lock" --case-dir "$case_dir" --field archive_sha256)

restart=${SLURM_RESTART_COUNT:-0}
run_dir=$root/runs/xmon-${SLURM_JOB_ID}-r${restart}
[[ ! -e $run_dir ]] || f1_die "run directory already exists: $run_dir"
mkdir -p "$run_dir/work" "$root/logs"
cp -a "$case_dir"/. "$run_dir/work"/
find "$run_dir/work" -type d -exec chmod u+w {} +

config=$run_dir/work/$config_relative
mesh=$run_dir/work/$mesh_relative
[[ $(f1_sha256 "$config") == 7bd0e7dc97854c8bac7829dd71e16f2c8c91cfe3caec456d24455f8d68caeed2 ]] || \
  f1_die "copied config checksum mismatch"
[[ $(f1_sha256 "$mesh") == 942365447b123cebe2f1f3f70c7284b5bc284602d8ed137f968637a703177cf5 ]] || \
  f1_die "copied mesh checksum mismatch"

# shellcheck disable=SC1090
source "$prefix/run-env.sh"
export OMP_NUM_THREADS=$threads
export OPENBLAS_NUM_THREADS=1
export OMP_PROC_BIND=close
export OMP_PLACES=cores

log=$run_dir/palace.log
resource_usage=$run_dir/resource-usage.txt
[[ -x /usr/bin/time ]] || f1_die "/usr/bin/time is required for the run receipt"
python3 "$control" run-start \
  --run-dir "$run_dir" \
  --case-dir "$case_dir" \
  --case-archive-sha256 "$archive_sha" \
  --config "$config" \
  --mesh "$mesh" \
  --executable "$executable" \
  --log "$log" \
  --resource-usage "$resource_usage" \
  --ranks "$ranks" \
  --threads "$threads"

cd -- "$(dirname -- "$config")"
set +e
/usr/bin/time -v -o "$resource_usage" \
  srun --mpi=pmix --cpu-bind=cores "$executable" "$(basename -- "$config")" \
  >"$log" 2>&1
exit_code=$?
set -e

[[ $(f1_sha256 "$case_dir/$config_relative") == 7bd0e7dc97854c8bac7829dd71e16f2c8c91cfe3caec456d24455f8d68caeed2 ]] || \
  f1_die "canonical config changed during run"
[[ $(f1_sha256 "$case_dir/$mesh_relative") == 942365447b123cebe2f1f3f70c7284b5bc284602d8ed137f968637a703177cf5 ]] || \
  f1_die "canonical mesh changed during run"

python3 "$control" run-finish \
  --run-dir "$run_dir" \
  --log "$log" \
  --resource-usage "$resource_usage" \
  --exit-code "$exit_code" \
  --case-archive-sha256 "$archive_sha" \
  --ranks "$ranks" \
  --threads "$threads"

echo "Palace run receipt: $run_dir/RUN-RECEIPT.json"
exit "$exit_code"
