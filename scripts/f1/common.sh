#!/usr/bin/env bash
# Shared constants and fail-fast checks for F1 Slurm entry points.

PALACE_F1_SOURCE_COMMIT=1ea14a84f60f672ffe4ce12dad404bc82896ef11
PALACE_F1_SOURCE_TREE=246e6de4418c00488da225b99c3e9c3461e63beb
PALACE_F1_BUILD_ID=palace-1ea14a84-gcc11.2.0-openmpi4.1.6-openblas0.3.20-zlib1.3.2-cpu
PALACE_F1_GCC_MODULE=gcc/11.2.0
PALACE_F1_MPI_MODULE=openmpi/4.1.6

f1_die() {
  echo "palace-f1: $*" >&2
  exit 1
}

f1_resolve_repo() {
  local candidate
  if [[ -n ${PALACE_F1_REPO:-} ]]; then
    candidate=$PALACE_F1_REPO
  elif [[ -n ${SLURM_SUBMIT_DIR:-} && -f $SLURM_SUBMIT_DIR/scripts/f1/sources.lock.json ]]; then
    candidate=$SLURM_SUBMIT_DIR
  else
    f1_die "set PALACE_F1_REPO to the shared checkout path, or submit from its root"
  fi
  candidate=$(cd -- "$candidate" && pwd -P) || f1_die "cannot resolve checkout: $candidate"
  [[ -f $candidate/scripts/f1/sources.lock.json ]] || f1_die "not a Palace F1 checkout: $candidate"
  printf '%s\n' "$candidate"
}

f1_require_job() {
  [[ -n ${SLURM_JOB_ID:-} ]] || f1_die "this entry point must run inside an sbatch allocation"
  [[ ${SLURM_JOB_NUM_NODES:-0} == 1 ]] || f1_die "exactly one node is supported"
  [[ $(uname -m) == x86_64 ]] || f1_die "F1 CPU build requires x86_64"
}

f1_clean_build_environment() {
  unset CC CXX FC F77 F90 CFLAGS CXXFLAGS FFLAGS FCFLAGS CPPFLAGS LDFLAGS
  unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_LIBRARY_PATH LD_PRELOAD
  unset PKG_CONFIG_PATH CMAKE_PREFIX_PATH
  unset ARMPL_DIR ARMPLROOT ARMPL_ROOT AOCL_DIR AOCLROOT AOCL_ROOT MKL_DIR MKLROOT MKL_ROOT
  unset OPENBLAS_DIR OPENBLASROOT OPENBLAS_ROOT
}

f1_load_modules() {
  if ! type module >/dev/null 2>&1; then
    if [[ -r /etc/profile.d/modules.sh ]]; then
      # shellcheck disable=SC1091
      source /etc/profile.d/modules.sh
    else
      f1_die "environment modules are unavailable"
    fi
  fi
  module purge
  module load "$PALACE_F1_GCC_MODULE"
  module load "$PALACE_F1_MPI_MODULE"

  local command_name
  for command_name in gcc g++ gfortran mpicc mpicxx mpifort ompi_info make git python3 srun readelf ldd sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || f1_die "required command unavailable: $command_name"
  done
  local gcc_version mpi_version
  gcc_version=$(gcc -dumpfullversion -dumpversion)
  [[ $gcc_version == 11.2.0 ]] || f1_die "expected GCC 11.2.0, found $gcc_version"
  [[ $(g++ -dumpfullversion -dumpversion) == 11.2.0 ]] || f1_die "expected G++ 11.2.0"
  [[ $(gfortran -dumpfullversion -dumpversion) == 11.2.0 ]] || f1_die "expected GFortran 11.2.0"
  make --version | sed -n '1p' | grep -q '^GNU Make ' || f1_die "GNU Make is required"
  mpi_version=$(ompi_info --version 2>/dev/null | sed -n '1{s/.*v\([0-9][0-9.]*\).*/\1/p;q;}')
  [[ $mpi_version == 4.1.6 ]] || f1_die "expected Open MPI 4.1.6, found ${mpi_version:-unknown}"
  [[ $(mpicc --showme:command | awk '{print $1}') == *gcc ]] || f1_die "mpicc is not backed by GCC"
  [[ $(mpicxx --showme:command | awk '{print $1}') == *g++ ]] || f1_die "mpicxx is not backed by G++"
  [[ $(mpifort --showme:command | awk '{print $1}') == *gfortran ]] || f1_die "mpifort is not backed by GFortran"
  srun --mpi=list 2>&1 | grep -q 'pmix' || f1_die "Slurm does not report PMIx support"
}

f1_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

f1_find_one() {
  local root=$1 pattern=$2
  local -a matches=()
  mapfile -t matches < <(find -L "$root" -type f -name "$pattern" -print)
  ((${#matches[@]} == 1)) || f1_die "expected one $pattern below $root, found ${#matches[@]}"
  printf '%s\n' "${matches[0]}"
}
